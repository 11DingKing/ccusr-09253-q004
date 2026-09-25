"""导师委托与授权确认的应用服务。

所有写操作在单个数据库事务内完成（SQLite 下以 BEGIN IMMEDIATE 开始，
保证“确认”与“撤销”严格串行）；授权只在写入瞬间判定，并把授权快照
（实际操作人、原责任人、授权版本、链路）固化进确认事件，因此事后撤销
或过期都不会改变已合法确认的记录。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy.orm import Session

from . import delegation_repository as repo
from .core.delegation import (
    Authority,
    DelegationRuleError,
    Grant,
    ensure_acyclic,
    ensure_no_overlap,
    grant_chain_link,
    reachable_intervals,
    resolve_authority,
    to_utc,
    validate_window,
)
from .models import Event as EventModel
from .repository import get_plan
from .core.replay import EventType


class DelegationError(Exception):
    """委托域通用错误。"""


class DelegationNotFoundError(DelegationError):
    pass


class DelegationConflictError(DelegationError):
    pass


class AuthorizationError(DelegationError):
    pass


class ValidationError(DelegationError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _at_utc(value: datetime) -> datetime:
    """服务边界使用：拒绝朴素时间并统一转为 UTC。"""
    try:
        return to_utc(value)
    except ValueError as exc:
        raise ValidationError("时间必须带时区信息（RFC 3339）") from exc


def _require_plan(db: Session, plan_version: str):
    plan = get_plan(db, plan_version)
    if plan is None:
        raise DelegationNotFoundError(f"plan version '{plan_version}' 不存在")
    return plan


def assign_responsible_mentor(
    db: Session,
    *,
    plan_version: str,
    student_id: str,
    mentor_id: str,
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    repo.upsert_mentor_assignment(
        db, plan_version=plan_version, student_id=student_id, mentor_id=mentor_id
    )
    db.commit()
    return {
        "plan_version": plan_version,
        "student_id": student_id,
        "mentor_id": mentor_id,
    }


def _responsible_mentor(db: Session, plan_version: str, student_id: str) -> str:
    assignment = repo.get_mentor_assignment(db, plan_version=plan_version, student_id=student_id)
    if assignment is None:
        raise AuthorizationError(
            f"学员 {student_id} 尚未分配负责导师，无法进行委托或确认"
        )
    return assignment.mentor_id


def _student_edges(
    grants: Sequence[Grant],
    student_id: str,
    horizon: tuple[datetime, datetime],
) -> list[tuple[str, str, tuple[datetime, datetime]]]:
    edges: list[tuple[str, str, tuple[datetime, datetime]]] = []
    for grant in grants:
        if grant.state != "active" or grant.student_id != student_id:
            continue
        window = (
            max(to_utc(grant.starts_at), horizon[0]),
            min(to_utc(grant.ends_at), horizon[1]),
        )
        if window[0] < window[1]:
            edges.append((grant.grantor_id, grant.grantee_id, window))
    return edges


def _ensure_grantor_holds_authority_for_window(
    grants: Sequence[Grant],
    *,
    responsible: str,
    grantor_id: str,
    student_id: str,
    starts_at: datetime,
    ends_at: datetime,
) -> None:
    """委托人在整个新窗口内都必须对该学员持有确认授权（本人责任或上游委托覆盖）。"""
    horizon = (to_utc(starts_at), to_utc(ends_at))
    if grantor_id == responsible:
        return
    edges = _student_edges(grants, student_id, horizon)
    coverage = reachable_intervals(
        edges, root=responsible, target=grantor_id, horizon=horizon
    )
    if coverage != [horizon]:
        raise AuthorizationError(
            f"导师 {grantor_id} 在 {horizon[0]:%Y-%m-%dT%H:%M:%SZ} ~ "
            f"{horizon[1]:%Y-%m-%dT%H:%M:%SZ} 内并非全程拥有学员 {student_id} "
            "的确认授权，不能把自身授权范围之外的时间委托出去"
        )


def create_delegation(
    db: Session,
    *,
    grant_id: str,
    plan_version: str,
    grantor_id: str,
    grantee_id: str,
    student_id: str,
    starts_at: datetime,
    ends_at: datetime,
    reason: str = "",
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    starts_at = _at_utc(starts_at)
    ends_at = _at_utc(ends_at)
    if grantor_id == grantee_id:
        raise ValidationError("委托人不能将学员委托给自己")
    try:
        validate_window(starts_at, ends_at)
    except DelegationRuleError as exc:
        raise ValidationError(str(exc)) from exc

    responsible = _responsible_mentor(db, plan_version, student_id)
    existing = repo.list_all_grants_for_student(
        db, plan_version=plan_version, student_id=student_id
    )
    _ensure_grantor_holds_authority_for_window(
        existing,
        responsible=responsible,
        grantor_id=grantor_id,
        student_id=student_id,
        starts_at=starts_at,
        ends_at=ends_at,
    )
    try:
        ensure_no_overlap(
            existing,
            grantor_id=grantor_id,
            student_id=student_id,
            starts_at=starts_at,
            ends_at=ends_at,
        )
        ensure_acyclic(
            existing,
            grantor_id=grantor_id,
            grantee_id=grantee_id,
            student_id=student_id,
            starts_at=starts_at,
            ends_at=ends_at,
        )
    except DelegationRuleError as exc:
        raise DelegationConflictError(str(exc)) from exc

    if repo.get_grant(db, grant_id) is not None:
        raise DelegationConflictError(f"委托 {grant_id} 已存在")

    grant = Grant(
        grant_id=grant_id,
        plan_version=plan_version,
        grantor_id=grantor_id,
        grantee_id=grantee_id,
        student_id=student_id,
        starts_at=starts_at,
        ends_at=ends_at,
        state="active",
        version=1,
        reason=reason,
    )
    repo.insert_grant(db, grant, created_at=_now())
    db.flush()
    row = repo.get_grant(db, grant_id)
    assert row is not None
    repo.insert_grant_version(db, row)
    db.commit()
    return grant_dict(row)


def revoke_delegation(
    db: Session,
    *,
    grant_id: str,
    revoked_by: str,
    reason: str,
    revoked_at: datetime | None = None,
) -> dict[str, Any]:
    row = repo.get_grant(db, grant_id)
    if row is None:
        raise DelegationNotFoundError(f"委托 {grant_id} 不存在")
    if row.state == "revoked":
        raise DelegationConflictError(f"委托 {grant_id} 已被撤销，不能重复撤销")

    responsible = _responsible_mentor(db, row.plan_version, row.student_id)
    if revoked_by != row.grantor_id and revoked_by != responsible:
        raise AuthorizationError(
            f"只有委托人 {row.grantor_id} 或负责导师 {responsible} 可以撤销该委托"
        )

    instant = _at_utc(revoked_at or _now())
    row.state = "revoked"
    row.version += 1
    row.revoked_at = instant
    row.revoke_reason = reason
    repo.insert_grant_version(db, row)

    # 级联撤销：该边下游所有仍生效的子委托同时失效并记录新版本，
    # 避免未来新的上游委托让一条“僵尸”子委托静默复活。
    for descendant_id in _downstream_active_grant_ids(
        db,
        plan_version=row.plan_version,
        student_id=row.student_id,
        root_grant_id=grant_id,
    ):
        child = repo.get_grant(db, descendant_id)
        if child is None or child.state != "active":
            continue
        child.state = "revoked"
        child.version += 1
        child.revoked_at = instant
        child.revoke_reason = f"上游委托 {grant_id} 被撤销，子委托级联失效"
        repo.insert_grant_version(db, child)

    db.commit()
    return grant_dict(row)


def _downstream_active_grant_ids(
    db: Session,
    *,
    plan_version: str,
    student_id: str,
    root_grant_id: str,
) -> list[str]:
    rows = repo.list_grant_rows_for_student(
        db, plan_version=plan_version, student_id=student_id
    )
    root = next((r for r in rows if r.grant_id == root_grant_id), None)
    if root is None:
        return []

    active = [r for r in rows if r.state == "active"]
    collected: list[str] = []
    frontier = [root.grantee_id]
    visited_mentors = {root.grantor_id}
    while frontier:
        mentor = frontier.pop()
        if mentor in visited_mentors:
            continue
        visited_mentors.add(mentor)
        for edge in active:
            if edge.grantor_id != mentor:
                continue
            collected.append(edge.grant_id)
            frontier.append(edge.grantee_id)
    return collected


def grant_dict(row: Any) -> dict[str, Any]:
    return {
        "grant_id": row.grant_id,
        "plan_version": row.plan_version,
        "grantor_id": row.grantor_id,
        "grantee_id": row.grantee_id,
        "student_id": row.student_id,
        "starts_at": _iso(row.starts_at),
        "ends_at": _iso(row.ends_at),
        "state": row.state,
        "version": row.version,
        "reason": row.reason,
        "created_at": _iso(row.created_at),
        "revoked_at": _iso(row.revoked_at),
        "revoke_reason": row.revoke_reason,
    }


def list_delegations(
    db: Session,
    *,
    plan_version: str,
    student_id: str | None = None,
    mentor_id: str | None = None,
) -> list[dict[str, Any]]:
    _require_plan(db, plan_version)
    if student_id is not None:
        rows = repo.list_grant_rows_for_student(
            db, plan_version=plan_version, student_id=student_id
        )
    else:
        rows = repo.list_grants(db, plan_version=plan_version, mentor_id=mentor_id)
    return [grant_dict(r) for r in rows]


def check_permission(
    db: Session,
    *,
    plan_version: str,
    student_id: str,
    operator_id: str,
    at: datetime | None = None,
) -> dict[str, Any]:
    """权限查询：某老师此刻（或指定时刻）能否确认某学员的实习签到。"""
    _require_plan(db, plan_version)
    moment = _at_utc(at or _now())
    responsible = _responsible_mentor(db, plan_version, student_id)
    grants = repo.list_all_grants_for_student(
        db, plan_version=plan_version, student_id=student_id
    )
    authority = resolve_authority(
        grants,
        responsible_mentor_id=responsible,
        operator_id=operator_id,
        student_id=student_id,
        moment=moment,
    )
    result = authority.to_dict()
    result["matching_grants"] = [
        grant_dict(repo.get_grant(db, g.grant_id))
        for g in authority.chain
    ]
    return result


def _load_checkin(db: Session, plan_version: str, checkin_event_id: str):
    event = (
        db.query(EventModel)
        .filter(EventModel.plan_version == plan_version)
        .filter(EventModel.event_id == checkin_event_id)
        .one_or_none()
    )
    if event is None:
        raise DelegationNotFoundError(
            f"签到事件 {checkin_event_id}（plan {plan_version}）不存在"
        )
    if event.event_type != EventType.CHECKIN.value:
        raise ValidationError(f"事件 {checkin_event_id} 不是签到事件")
    if event.payload.get("activity_type", "regular") != "internship":
        raise ValidationError(
            f"签到 {checkin_event_id} 的活动类型为 "
            f"{event.payload.get('activity_type', 'regular')}，"
            "只有实习（internship）签到需要导师确认"
        )
    return event


def _chain_snapshot(authority: Authority) -> tuple[str | None, int | None, list[dict[str, Any]]]:
    if authority.kind != "delegated":
        return None, None, []
    last = authority.chain[-1]
    chain = [grant_chain_link(g, seq) for seq, g in enumerate(authority.chain, 1)]
    return last.grant_id, last.version, chain


def _persist_confirmation(
    db: Session,
    *,
    plan_version: str,
    checkin: EventModel,
    confirmation_id: str,
    authority: Authority,
    confirmed_at: datetime,
) -> None:
    grant_id, grant_version, chain = _chain_snapshot(authority)
    inserted = repo.insert_confirmation(
        db,
        confirmation_id=confirmation_id,
        plan_version=plan_version,
        checkin_event_id=checkin.event_id,
        student_id=checkin.student_id,
        operator_id=authority.operator_id,
        responsible_mentor_id=authority.responsible_mentor_id,
        authority=authority.kind,
        grant_id=grant_id,
        grant_version=grant_version,
        delegation_chain=chain,
        confirmed_at=confirmed_at,
    )
    if inserted is None:
        raise DelegationConflictError(f"确认 {confirmation_id} 已存在（幂等冲突）")

    # 同步追加一条 mentor_confirm 事件，使学时重放与冻结快照反映确认结果；
    # 授权证据固化在 confirmations 表与 payload 中。
    db.add(
        EventModel(
            event_id=confirmation_id,
            plan_version=plan_version,
            student_id=checkin.student_id,
            event_type=EventType.MENTOR_CONFIRM.value,
            payload={
                "checkin_event_id": checkin.event_id,
                "confirmation_id": confirmation_id,
                "operator_id": authority.operator_id,
                "responsible_mentor_id": authority.responsible_mentor_id,
                "authority": "delegated" if grant_id else "direct",
                "grant_id": grant_id,
                "grant_version": grant_version,
                "delegation_chain": chain,
                "confirmed_at": _iso(confirmed_at),
            },
        )
    )


@dataclass(frozen=True)
class ConfirmRequest:
    confirmation_id: str
    checkin_event_id: str
    operator_id: str
    confirmed_at: datetime | None = None


def _validate_one(
    db: Session,
    plan_version: str,
    req: ConfirmRequest,
    moment: datetime,
) -> tuple[EventModel, Authority]:
    checkin = _load_checkin(db, plan_version, req.checkin_event_id)
    responsible = _responsible_mentor(db, plan_version, checkin.student_id)
    grants = repo.list_all_grants_for_student(
        db, plan_version=plan_version, student_id=checkin.student_id
    )
    authority = resolve_authority(
        grants,
        responsible_mentor_id=responsible,
        operator_id=req.operator_id,
        student_id=checkin.student_id,
        moment=moment,
    )
    if not authority.authorized:
        raise AuthorizationError(
            f"确认被拒绝：{authority.denial_reason}（签到 {req.checkin_event_id}，"
            f"操作人 {req.operator_id}，时刻 {_iso(moment)}）"
        )
    existing = repo.get_confirmation_for_checkin(
        db, plan_version=plan_version, checkin_event_id=req.checkin_event_id
    )
    if existing is not None:
        raise DelegationConflictError(
            f"签到 {req.checkin_event_id} 已由确认 {existing.confirmation_id} 确认"
        )
    clashing_event = (
        db.query(EventModel.event_id, EventModel.event_type)
        .filter(EventModel.plan_version == plan_version)
        .filter(EventModel.event_id == req.confirmation_id)
        .one_or_none()
    )
    if clashing_event is not None:
        raise DelegationConflictError(
            f"确认 ID {req.confirmation_id} 已被 "
            f"{clashing_event[1]} 事件占用，标识符不可复用"
        )
    return checkin, authority


def _existing_idempotent(
    db: Session, plan_version: str, req: ConfirmRequest
) -> dict[str, Any] | None:
    """已落库的同 ID 确认按幂等重放处理（不再校验此刻授权）。

    跨确认 ID 复用（同 ID 指向不同签到）视为冲突。
    """
    row = repo.get_confirmation(db, confirmation_id=req.confirmation_id)
    if row is None:
        return None
    if row.plan_version != plan_version or row.checkin_event_id != req.checkin_event_id:
        raise DelegationConflictError(
            f"确认 {req.confirmation_id} 已用于其他签到，标识符不可复用"
        )
    return explain_confirmation(db, req.confirmation_id)


def confirm_checkin(
    db: Session,
    *,
    plan_version: str,
    confirmation_id: str,
    checkin_event_id: str,
    operator_id: str,
    confirmed_at: datetime | None = None,
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    moment = _at_utc(confirmed_at or _now())
    req = ConfirmRequest(confirmation_id, checkin_event_id, operator_id, moment)
    existing = _existing_idempotent(db, plan_version, req)
    if existing is not None:
        return existing
    checkin, authority = _validate_one(db, plan_version, req, moment)
    _persist_confirmation(
        db,
        plan_version=plan_version,
        checkin=checkin,
        confirmation_id=confirmation_id,
        authority=authority,
        confirmed_at=moment,
    )
    try:
        db.commit()
    except Exception as exc:  # 唯一约束等并发竞争
        db.rollback()
        raise DelegationConflictError(
            f"签到 {checkin_event_id} 在提交时发现并发冲突，确认未落库"
        ) from exc
    return explain_confirmation(db, confirmation_id)


def confirm_batch(
    db: Session,
    *,
    plan_version: str,
    confirms: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """批量确认：全部成功才提交，任一被拒整体回滚、不落库。"""
    _require_plan(db, plan_version)
    if not confirms:
        raise ValidationError("批量确认至少包含一条请求")

    seen_confirmation_ids: set[str] = set()
    seen_checkins: set[str] = set()
    validated: list[tuple[ConfirmRequest, datetime, EventModel, Authority]] = []
    idempotent: dict[str, dict[str, Any]] = {}
    for item in confirms:
        req = ConfirmRequest(
            confirmation_id=item["confirmation_id"],
            checkin_event_id=item["checkin_event_id"],
            operator_id=item["operator_id"],
            confirmed_at=None,
        )
        moment = _at_utc(item.get("confirmed_at") or _now())
        if req.confirmation_id in seen_confirmation_ids:
            raise ValidationError(f"批量请求内确认 ID 重复：{req.confirmation_id}")
        if req.checkin_event_id in seen_checkins:
            raise ValidationError(f"批量请求内同一签到被重复确认：{req.checkin_event_id}")
        seen_confirmation_ids.add(req.confirmation_id)
        seen_checkins.add(req.checkin_event_id)
        replayed = _existing_idempotent(db, plan_version, req)
        if replayed is not None:
            idempotent[req.confirmation_id] = replayed
            continue
        checkin, authority = _validate_one(db, plan_version, req, moment)
        validated.append((req, moment, checkin, authority))

    for req, moment, checkin, authority in validated:
        _persist_confirmation(
            db,
            plan_version=plan_version,
            checkin=checkin,
            confirmation_id=req.confirmation_id,
            authority=authority,
            confirmed_at=moment,
        )
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise DelegationConflictError("批量确认提交时发生并发冲突，整批已回滚") from exc

    return [
        idempotent[item["confirmation_id"]]
        if item["confirmation_id"] in idempotent
        else explain_confirmation(db, item["confirmation_id"])
        for item in confirms
    ]


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return to_utc(value).isoformat().replace("+00:00", "Z")


def _confirmation_dict(row: Any, *, current_states: dict[str, Any]) -> dict[str, Any]:
    return {
        "confirmation_id": row.confirmation_id,
        "plan_version": row.plan_version,
        "checkin_event_id": row.checkin_event_id,
        "student_id": row.student_id,
        "operator_id": row.operator_id,
        "responsible_mentor_id": row.responsible_mentor_id,
        "authority": row.authority,
        "grant_id": row.grant_id,
        "grant_version": row.grant_version,
        "delegation_chain": list(row.delegation_chain or []),
        "confirmed_at": _iso(row.confirmed_at),
        "created_at": _iso(row.created_at),
        "recorded_grant_states": [
            hop.get("state") for hop in (row.delegation_chain or [])
        ],
        "current_grant_states": [
            current_states.get(hop.get("grant_id"))
            for hop in (row.delegation_chain or [])
        ],
        "still_legally_valid": True,
    }


def explain_confirmation(
    db: Session, confirmation_id: str, *, plan_version: str | None = None
) -> dict[str, Any]:
    """确认解释：还原操作人、原责任人、授权版本与链路，并标注当前状态。

    授权事后被撤销或已过期时，current_grant_states 显示链路各跳的现状，
    但 still_legally_valid 恒为 True —— 撤销不溯及既往。
    传入 plan_version 时会校验确认归属，避免跨培养方案读取。
    """
    row = repo.get_confirmation(db, confirmation_id=confirmation_id)
    if row is None or (
        plan_version is not None and row.plan_version != plan_version
    ):
        raise DelegationNotFoundError(f"确认 {confirmation_id} 不存在")
    current_states: dict[str, Any] = {}
    for hop in row.delegation_chain or []:
        gid = hop.get("grant_id")
        if gid is None or gid in current_states:
            continue
        grant_row = repo.get_grant(db, gid)
        current_states[gid] = grant_row.state if grant_row else "missing"
    return _confirmation_dict(row, current_states=current_states)


def reverify_confirmation(
    db: Session, confirmation_id: str, *, plan_version: str | None = None
) -> dict[str, Any]:
    """重启后/审计时复核：固化的授权版本必须与版本历史逐字段一致，
    且该版本在确认时刻确实处于 active 且窗口覆盖确认时刻。

    复核只证明“当时合法、证据未被篡改”，不要求授权此刻仍然有效。
    """
    row = repo.get_confirmation(db, confirmation_id=confirmation_id)
    if row is None or (
        plan_version is not None and row.plan_version != plan_version
    ):
        raise DelegationNotFoundError(f"确认 {confirmation_id} 不存在")

    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "passed": ok, "detail": detail})

    if row.authority == "direct":
        add(
            "operator_is_responsible",
            row.operator_id == row.responsible_mentor_id,
            f"{row.operator_id} == {row.responsible_mentor_id}",
        )
        add("no_grant_required", row.grant_id is None, "direct authority")
    else:
        add(
            "chain_snapshot_present",
            bool(row.delegation_chain),
            f"{len(row.delegation_chain or [])} hops",
        )
        confirmed_at = to_utc(row.confirmed_at)
        expected_grantor = row.responsible_mentor_id
        for index, hop in enumerate(row.delegation_chain or [], start=1):
            prefix = f"hop{index}:{hop.get('grant_id')}v{hop.get('grant_version')}"
            version_row = repo.get_grant_version(
                db,
                grant_id=hop.get("grant_id"),
                version=hop.get("grant_version"),
            )
            add(f"{prefix}:persisted", version_row is not None,
                "version row found in immutable history")
            if version_row is None:
                continue
            add(f"{prefix}:recorded_active", version_row.state == "active",
                f"state at recorded version = {version_row.state}")
            in_window = (
                to_utc(version_row.starts_at) <= confirmed_at
                and confirmed_at < to_utc(version_row.ends_at)
            )
            add(f"{prefix}:window_covers_confirmation", in_window,
                f"{_iso(version_row.starts_at)} <= {_iso(confirmed_at)} "
                f"< {_iso(version_row.ends_at)}")
            add(
                f"{prefix}:chain_links_to_expected_grantor",
                version_row.grantor_id == expected_grantor
                and version_row.grantee_id == hop.get("grantee_id")
                and version_row.student_id == row.student_id,
                f"{version_row.grantor_id} -> {version_row.grantee_id}, "
                f"expected grantor {expected_grantor}",
            )
            expected_grantor = version_row.grantee_id
        add(
            "chain_ends_at_operator",
            expected_grantor == row.operator_id,
            f"chain ends at {expected_grantor}, operator {row.operator_id}",
        )
        last_link = (row.delegation_chain or [])[-1]
        add(
            "terminal_grant_reference_matches",
            row.grant_id == last_link.get("grant_id")
            and row.grant_version == last_link.get("grant_version"),
            f"confirmation references {row.grant_id}v{row.grant_version}",
        )

    valid = all(c["passed"] for c in checks)
    return {
        "confirmation_id": confirmation_id,
        "valid": valid,
        "checks": checks,
        "explanation": explain_confirmation(db, confirmation_id),
    }
