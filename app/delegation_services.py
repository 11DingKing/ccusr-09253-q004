"""导师委托与确认的应用服务。

写路径（新增委托、撤销、批量确认）统一通过 ``BEGIN IMMEDIATE`` 开启 SQLite
写事务，在同一个事务内完成"读取授权快照 -> 领域校验 -> 落库"，因此并发的
撤销与确认只能串行发生，不会出现读到旧授权后写入的窗口。

确认一旦合法落库，其依据的授权版本、委托链与责任人就随事件永久保存；之后
撤销委托或委托过期只会改变 *之后* 的校验结果，不影响既有确认。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from . import delegation_repository as repo
from .core.clock import clock
from .delegations import (
    DelegationError,
    Grant,
    describe_grant,
    resolve_authorization,
    validate_new_grant,
    validate_window,
)
from .models import Delegation, DelegationVersion, MentorConfirmation
from .repository import get_plan


class DelegationNotFoundError(Exception):
    pass


class ResponsibleMentorNotFoundError(Exception):
    pass


class GrantConflictError(Exception):
    pass


class PermissionDeniedError(Exception):
    pass


class ConfirmationRejectedError(Exception):
    """批量确认中存在不合法条目；整批已被拒绝，无任何落库。"""

    def __init__(self, message: str, failures: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.failures = failures


@contextmanager
def immediate_tx(db: Session) -> Iterator[Session]:
    """在独立连接上开启 BEGIN IMMEDIATE 并返回同一事务内的 ORM 会话。"""
    # 结束请求级会话可能持有的共享锁，避免写事务升级 EXCLUSIVE 时自阻塞。
    db.commit()
    engine = db.bind
    conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    conn.execute(text("BEGIN IMMEDIATE"))
    tx = Session(bind=conn, join_transaction_mode="create_savepoint")
    try:
        yield tx
        tx.commit()
        conn.execute(text("COMMIT"))
    except BaseException:
        tx.rollback()
        if conn.in_transaction():
            conn.execute(text("ROLLBACK"))
        raise
    finally:
        tx.close()
        conn.close()


def _latest_auth_timestamp(tx: Session) -> datetime | None:
    """库内已落库的最新授权/确认时间（重启后据此恢复单调性）。"""
    candidates = [
        tx.execute(select(func.max(Delegation.created_at))).scalar_one_or_none(),
        tx.execute(select(func.max(Delegation.revoked_at))).scalar_one_or_none(),
        tx.execute(select(func.max(MentorConfirmation.authorized_at))).scalar_one_or_none(),
        tx.execute(select(func.max(DelegationVersion.created_at))).scalar_one_or_none(),
    ]
    stamped = [repo.as_utc(c) for c in candidates if c is not None]
    return max(stamped) if stamped else None


def _monotonic_now(tx: Session) -> datetime:
    """事务时间戳：不早于墙钟，且严格晚于库内任何既有授权时间。"""
    wall = clock.now()
    latest = _latest_auth_timestamp(tx)
    if latest is not None and latest >= wall:
        return latest + timedelta(microseconds=1)
    return wall


def _require_plan_tx(tx: Session, plan_version: str):
    plan = get_plan(tx, plan_version)
    if plan is None:
        raise DelegationNotFoundError(
            f"plan version '{plan_version}' is not registered"
        )
    return plan


def assign_mentor(
    db: Session,
    *,
    plan_version: str,
    student_id: str,
    mentor_id: str,
) -> dict[str, Any]:
    with immediate_tx(db) as tx:
        _require_plan_tx(tx, plan_version)
        now = _monotonic_now(tx)
        repo.upsert_assignment(
            tx,
            plan_version=plan_version,
            student_id=student_id,
            mentor_id=mentor_id,
            now=now,
        )
    return {
        "plan_version": plan_version,
        "student_id": student_id,
        "mentor_id": mentor_id,
        "updated_at_utc": now.isoformat().replace("+00:00", "Z"),
    }


def _grant_out(row, *, now: datetime) -> dict[str, Any]:
    return describe_grant(repo.to_grant(row), now=now)


def create_delegation(
    db: Session,
    *,
    plan_version: str,
    grant_id: str,
    student_id: str,
    delegator_id: str,
    grantee_id: str,
    starts_at: datetime,
    ends_at: datetime,
    actor_id: str,
    reason: str,
) -> dict[str, Any]:
    validate_window(starts_at, ends_at)
    if actor_id != delegator_id:
        raise PermissionDeniedError("只有委托人本人可以登记委托")

    with immediate_tx(db) as tx:
        now = _monotonic_now(tx)
        start = starts_at.astimezone(now.tzinfo)
        end = ends_at.astimezone(now.tzinfo)
        _require_plan_tx(tx, plan_version)
        if repo.get_grant(tx, grant_id) is not None:
            raise GrantConflictError(f"委托 {grant_id} 已存在")

        assignment = repo.get_assignment(tx, plan_version, student_id)
        if assignment is None:
            raise ResponsibleMentorNotFoundError(
                f"学生 {student_id} 尚未登记责任导师，无法建立委托"
            )

        rows = repo.list_grants(tx, plan_version=plan_version, student_id=student_id)
        grants = [repo.to_grant(r) for r in rows]
        new_grant = Grant(
            grant_id=grant_id,
            plan_version=plan_version,
            student_id=student_id,
            delegator_id=delegator_id,
            grantee_id=grantee_id,
            starts_at=start,
            ends_at=end,
        )
        validate_new_grant(
            new_grant,
            grants,
            responsible_id=assignment.mentor_id,
            as_of=now,
        )
        repo.insert_grant(
            tx,
            grant_id=grant_id,
            plan_version=plan_version,
            student_id=student_id,
            delegator_id=delegator_id,
            grantee_id=grantee_id,
            starts_at=start,
            ends_at=end,
            actor_id=actor_id,
            reason=reason,
            now=now,
        )
        row = repo.get_grant(tx, grant_id)
        assert row is not None
        result = _grant_out(row, now=now)
        result["versions"] = _versions_out(repo.list_versions(tx, grant_id))
    return result


def revoke_delegation(
    db: Session,
    *,
    grant_id: str,
    actor_id: str,
    reason: str,
) -> dict[str, Any]:
    with immediate_tx(db) as tx:
        now = _monotonic_now(tx)
        row = repo.get_grant(tx, grant_id)
        if row is None:
            raise DelegationNotFoundError(f"委托 {grant_id} 不存在")

        assignment = repo.get_assignment(tx, row.plan_version, row.student_id)
        responsible = assignment.mentor_id if assignment is not None else None
        if actor_id not in {row.delegator_id, responsible}:
            raise PermissionDeniedError(
                "只有委托人或责任导师可以撤销该委托"
            )

        current_grant = repo.to_grant(row)
        changed = False
        if current_grant.status == "active" and now < current_grant.ends_at:
            repo.mark_grant_revoked(
                tx, row, actor_id=actor_id, reason=reason, now=now
            )
            changed = True
        tx.flush()
        result = _grant_out(row, now=now)
        result["changed"] = changed
        result["versions"] = _versions_out(repo.list_versions(tx, grant_id))
    return result


def get_delegation(db: Session, grant_id: str) -> dict[str, Any]:
    now = clock.now()
    row = repo.get_grant(db, grant_id)
    if row is None:
        raise DelegationNotFoundError(f"委托 {grant_id} 不存在")
    result = _grant_out(row, now=now)
    result["versions"] = _versions_out(repo.list_versions(db, grant_id))
    return result


def list_delegations(
    db: Session,
    *,
    plan_version: str,
    student_id: str | None = None,
    mentor_id: str | None = None,
) -> dict[str, Any]:
    now = clock.now()
    _require_plan_tx(db, plan_version)
    rows = repo.list_grants(
        db,
        plan_version=plan_version,
        student_id=student_id,
        mentor_id=mentor_id,
    )
    items = [_grant_out(r, now=now) for r in rows]
    return {"plan_version": plan_version, "delegations": items}


def check_authorization(
    db: Session,
    *,
    plan_version: str,
    student_id: str,
    actor_mentor_id: str,
    at: datetime | None = None,
) -> dict[str, Any]:
    """权限查询：某老师在指定时刻（默认当前）能否确认该学生。"""
    moment = (at or clock.now()).astimezone(clock.now().tzinfo)
    _require_plan_tx(db, plan_version)
    assignment = repo.get_assignment(db, plan_version, student_id)
    responsible = assignment.mentor_id if assignment is not None else ""
    grants = [
        repo.to_grant(r)
        for r in repo.list_grants(
            db, plan_version=plan_version, student_id=student_id
        )
    ]
    auth = resolve_authorization(
        actor_id=actor_mentor_id,
        responsible_id=responsible,
        plan_version=plan_version,
        student_id=student_id,
        grants=grants,
        as_of=moment,
    )
    result = auth.to_explanation()
    result["checked_at_utc"] = moment.isoformat().replace("+00:00", "Z")
    if not responsible:
        result["reason"] = "学生尚未登记责任导师"
    return result


def _versions_out(versions) -> list[dict[str, Any]]:
    return [
        {
            "grant_id": v.grant_id,
            "version": v.version,
            "action": v.action,
            "actor_id": v.actor_id,
            "reason": v.reason,
            "created_at_utc": repo.as_utc(v.created_at)
            .isoformat()
            .replace("+00:00", "Z"),
        }
        for v in versions
    ]


def _confirm_event_id(checkin_event_id: str) -> str:
    return f"confirm:{checkin_event_id}"


def confirm_checkins(
    db: Session,
    *,
    plan_version: str,
    actor_mentor_id: str,
    checkin_event_ids: list[str],
    at: datetime | None = None,
) -> dict[str, Any]:
    if not checkin_event_ids:
        raise DelegationError("批量确认至少包含一条签到")
    if len(set(checkin_event_ids)) != len(checkin_event_ids):
        raise DelegationError("同一批次中不允许出现重复签到")

    results: list[dict[str, Any]] = []

    with immediate_tx(db) as tx:
        _require_plan_tx(tx, plan_version)
        # 显式 at 仅用于窗口边界测试；实时确认取持久化单调时间戳。
        now = (
            at.astimezone(timezone.utc)
            if at is not None
            else _monotonic_now(tx)
        )

        checkins: dict[str, dict[str, Any]] = {}
        for checkin_event_id in checkin_event_ids:
            loaded = _load_checkin_event(tx, plan_version, checkin_event_id)
            if loaded is not None:
                checkins[checkin_event_id] = loaded

        assignments = {
            a.student_id: a.mentor_id
            for a in repo.list_assignments(tx, plan_version)
        }

        # 预载相关学生的全部授权，在内存中做纯领域解析。
        grants = [
            repo.to_grant(r) for r in repo.list_grants(tx, plan_version=plan_version)
        ]
        existing_confirmations = {
            c.checkin_event_id: c
            for c in repo.list_confirmations(
                tx,
                plan_version=plan_version,
                checkin_event_ids=checkin_event_ids,
            )
        }

        failures: list[dict[str, Any]] = []

        # 第一阶段：整批逐条校验，任何一条不合法都拒绝整批。
        resolved: list[tuple[str, str, Any]] = []
        for checkin_event_id in checkin_event_ids:
            checkin = checkins.get(checkin_event_id)
            if checkin is None:
                failures.append(
                    {
                        "checkin_event_id": checkin_event_id,
                        "code": "checkin_not_found",
                    }
                )
                continue
            if checkin["activity_type"] != "internship":
                failures.append(
                    {
                        "checkin_event_id": checkin_event_id,
                        "code": "confirmation_not_required",
                        "detail": "仅实习类签到需要导师确认",
                    }
                )
                continue
            if checkin_event_id in existing_confirmations:
                failures.append(
                    {
                        "checkin_event_id": checkin_event_id,
                        "code": "already_confirmed",
                    }
                )
                continue
            student_id = checkin["student_id"]
            responsible = assignments.get(student_id, "")
            if not responsible:
                failures.append(
                    {
                        "checkin_event_id": checkin_event_id,
                        "code": "responsible_mentor_missing",
                    }
                )
                continue
            auth = resolve_authorization(
                actor_id=actor_mentor_id,
                responsible_id=responsible,
                plan_version=plan_version,
                student_id=student_id,
                grants=grants,
                as_of=now,
            )
            if not auth.authorized:
                failures.append(
                    {
                        "checkin_event_id": checkin_event_id,
                        "code": "not_authorized",
                        "detail": auth.reason,
                    }
                )
                continue
            resolved.append((checkin_event_id, student_id, auth))

        if failures:
            # 不写入任何内容，事务随上下文回滚。
            raise ConfirmationRejectedError(
                "批量确认中存在不合法条目，整批已拒绝", failures
            )

        # 第二阶段：全部合法 -> 审计记录与 mentor_confirm 事件一起落库。
        for checkin_event_id, student_id, auth in resolved:
            confirm_event_id = _confirm_event_id(checkin_event_id)
            chain_payload = [
                {
                    "grant_id": step.grant_id,
                    "delegator_id": step.delegator_id,
                    "grantee_id": step.grantee_id,
                    "grant_version": step.version,
                }
                for step in auth.path
            ]
            payload = {
                "checkin_event_id": checkin_event_id,
                "actor_mentor_id": actor_mentor_id,
                "responsible_mentor_id": auth.responsible_id,
                "grant_id": auth.grant.grant_id if auth.grant else None,
                "grant_version": auth.grant.version if auth.grant else None,
                "chain_length": auth.chain_length,
                "delegation_path": chain_payload,
                "authorized_at": now.isoformat().replace("+00:00", "Z"),
            }
            repo.insert_confirmation(
                tx,
                {
                    "plan_version": plan_version,
                    "student_id": student_id,
                    "checkin_event_id": checkin_event_id,
                    "actor_mentor_id": actor_mentor_id,
                    "responsible_mentor_id": auth.responsible_id,
                    "grant_id": auth.grant.grant_id if auth.grant else None,
                    "grant_version": auth.grant.version if auth.grant else None,
                    "chain_length": auth.chain_length,
                    "delegation_path": chain_payload,
                    "authorized_at": now,
                    "confirm_event_id": confirm_event_id,
                },
            )
            repo.insert_confirm_event(
                tx,
                event_id=confirm_event_id,
                plan_version=plan_version,
                student_id=student_id,
                payload=payload,
            )
            results.append(
                {
                    "checkin_event_id": checkin_event_id,
                    "student_id": student_id,
                    "confirm_event_id": confirm_event_id,
                    **payload,
                }
            )

    return {
        "plan_version": plan_version,
        "actor_mentor_id": actor_mentor_id,
        "confirmed_at_utc": now.isoformat().replace("+00:00", "Z"),
        "count": len(results),
        "confirmations": results,
    }


def _load_checkin_event(tx: Session, plan_version: str, event_id: str):
    from .models import Event as EventModel

    stmt = (
        select(EventModel)
        .where(EventModel.plan_version == plan_version)
        .where(EventModel.event_id == event_id)
        .where(EventModel.event_type == "checkin")
    )
    row = tx.execute(stmt).scalar_one_or_none()
    if row is None:
        return None
    return {
        "student_id": row.student_id,
        "activity_type": row.payload.get("activity_type", "regular"),
    }


def explain_confirmation(
    db: Session,
    *,
    plan_version: str,
    checkin_event_id: str,
    at: datetime | None = None,
) -> dict[str, Any]:
    """确认解释：回放记录的实际操作人/责任人/授权版本，并重新校验当前状态。"""
    now = (at or clock.now()).astimezone(clock.now().tzinfo)
    _require_plan_tx(db, plan_version)
    record = repo.get_confirmation(db, plan_version, checkin_event_id)
    if record is None:
        raise DelegationNotFoundError(
            f"签到 {checkin_event_id} 没有确认记录"
        )

    grants = [
        repo.to_grant(r)
        for r in repo.list_grants(
            db,
            plan_version=plan_version,
            student_id=record.student_id,
        )
    ]
    # 以记录的确认时刻重放授权（撤销不溯及既往）。
    then = resolve_authorization(
        actor_id=record.actor_mentor_id,
        responsible_id=record.responsible_mentor_id,
        plan_version=plan_version,
        student_id=record.student_id,
        grants=grants,
        as_of=repo.as_utc(record.authorized_at),
    )
    # 以当前时刻重新校验，说明撤销/过期后的现状。
    current = resolve_authorization(
        actor_id=record.actor_mentor_id,
        responsible_id=record.responsible_mentor_id,
        plan_version=plan_version,
        student_id=record.student_id,
        grants=grants,
        as_of=now,
    )

    versions: list[dict[str, Any]] = []
    if record.grant_id is not None:
        versions = _versions_out(repo.list_versions(db, record.grant_id))

    # 记录中固化的授权版本必须能在不可变版本历史中找到。
    recorded_version_present = (
        record.grant_id is None
        or any(v["version"] == record.grant_version for v in versions)
    )
    # 历史重放：授权在确认时刻成立，且路径终点的委托与记录一致
    # （撤销/过期都不溯及既往；版本号以固化的审计记录为准）。
    was_authorized = (
        then.authorized
        and (
            record.grant_id is None
            or (then.grant is not None and then.grant.grant_id == record.grant_id)
        )
        and recorded_version_present
    )

    return {
        "plan_version": plan_version,
        "student_id": record.student_id,
        "checkin_event_id": record.checkin_event_id,
        "confirm_event_id": record.confirm_event_id,
        "actor_mentor_id": record.actor_mentor_id,
        "responsible_mentor_id": record.responsible_mentor_id,
        "grant_id": record.grant_id,
        "grant_version": record.grant_version,
        "chain_length": record.chain_length,
        "delegation_path": record.delegation_path,
        "authorized_at_utc": repo.as_utc(record.authorized_at)
        .isoformat()
        .replace("+00:00", "Z"),
        "was_authorized_at_confirmation": was_authorized,
        "recorded_grant_version_present": recorded_version_present,
        "historical_reason": then.reason,
        "currently_authorized": current.authorized,
        "current_reason": current.reason,
        "checked_at_utc": now.isoformat().replace("+00:00", "Z"),
        "grant_versions": versions,
    }
