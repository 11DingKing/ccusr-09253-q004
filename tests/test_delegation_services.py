"""委托链 API 与应用服务测试。

覆盖：链式委托确认、撤销不溯及既往、越权/过期拒绝、批量全成全败、
权限查询与确认解释、跨时区窗口边界，以及重启后的授权版本复核。
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from datetime import datetime, timezone

import pytest

from app import delegation_services as svc
from app import services as core_svc
from app.db import make_engine
from app.models import Base, DelegationGrant, DelegationGrantVersion
from app.repository import upsert_plan
from tests.conftest import TestSessionLocal

PLAN = "P-DEL"
STUDENT = "S1"
MENTOR = "M1"
SUBSTITUTE = "M2"
THIRD = "M3"


# --------------------------------------------------------------------------
# service-level helpers
# --------------------------------------------------------------------------


def _seed_plan(db, plan_version: str = PLAN, tz: str = "Asia/Shanghai"):
    upsert_plan(
        db,
        plan_version=plan_version,
        iana_timezone=tz,
        required_seconds=3600,
    )
    db.commit()


def _assign(db, student_id: str = STUDENT, mentor_id: str = MENTOR,
            plan_version: str = PLAN):
    return svc.assign_responsible_mentor(
        db, plan_version=plan_version, student_id=student_id, mentor_id=mentor_id
    )


def _grant(db, gid, grantor, grantee, start, end, *, student_id=STUDENT,
           plan_version=PLAN, reason=""):
    return svc.create_delegation(
        db,
        grant_id=gid,
        plan_version=plan_version,
        grantor_id=grantor,
        grantee_id=grantee,
        student_id=student_id,
        starts_at=datetime.fromisoformat(start),
        ends_at=datetime.fromisoformat(end),
        reason=reason,
    )


def _checkin(db, eid, *, student_id=STUDENT, plan_version=PLAN,
             check_in="2026-09-15T08:00:00+08:00",
             check_out="2026-09-15T12:00:00+08:00"):
    core_svc.import_events(
        db,
        plan_version=plan_version,
        events=[
            {
                "event_id": eid,
                "event_type": "checkin",
                "student_id": student_id,
                "payload": {
                    "activity_id": "A1",
                    "activity_type": "internship",
                    "check_in_at": check_in,
                    "check_out_at": check_out,
                },
            }
        ],
    )


@pytest.fixture
def db_env(db):
    _seed_plan(db)
    _assign(db)
    return db


# --------------------------------------------------------------------------
# 链式委托
# --------------------------------------------------------------------------


def test_multi_hop_chain_confirmation_records_actor_owner_and_versions(db_env):
    db = db_env
    _grant(db, "G1", MENTOR, SUBSTITUTE,
           "2026-09-01T00:00:00+08:00", "2026-10-01T00:00:00+08:00")
    _grant(db, "G2", SUBSTITUTE, THIRD,
           "2026-09-10T00:00:00+08:00", "2026-09-20T00:00:00+08:00")
    _checkin(db, "E-1")

    out = svc.confirm_checkin(
        db,
        plan_version=PLAN,
        confirmation_id="C-1",
        checkin_event_id="E-1",
        operator_id=THIRD,
        confirmed_at=datetime.fromisoformat("2026-09-15T13:00:00+08:00"),
    )
    assert out["authority"] == "delegated"
    assert out["operator_id"] == THIRD            # 实际操作人
    assert out["responsible_mentor_id"] == MENTOR  # 原责任人
    assert out["grant_id"] == "G2"
    assert out["grant_version"] == 1               # 授权版本
    assert [hop["grant_id"] for hop in out["delegation_chain"]] == ["G1", "G2"]
    assert [hop["grant_version"] for hop in out["delegation_chain"]] == [1, 1]
    assert out["still_legally_valid"] is True

    # 学时重放反映确认结果
    progress = core_svc.student_progress(db, PLAN, STUDENT)
    assert progress["confirmed_seconds"] == 4 * 3600
    assert progress["pending_seconds"] == 0


def test_chain_delegation_is_rejected_when_upstream_window_does_not_cover(db_env):
    db = db_env
    _grant(db, "G1", MENTOR, SUBSTITUTE,
           "2026-09-01T00:00:00+08:00", "2026-09-20T00:00:00+08:00")
    # M2 只能在自己持有授权的窗口内继续委托
    with pytest.raises(svc.AuthorizationError):
        _grant(db, "G2", SUBSTITUTE, THIRD,
               "2026-09-19T00:00:00+08:00", "2026-09-25T00:00:00+08:00")


def test_freeze_snapshot_reflects_delegated_confirmation(db_env):
    db = db_env
    _grant(db, "G1", MENTOR, SUBSTITUTE,
           "2026-09-01T00:00:00+08:00", "2026-10-01T00:00:00+08:00")
    _checkin(db, "E-1")
    svc.confirm_checkin(
        db, plan_version=PLAN, confirmation_id="C-1", checkin_event_id="E-1",
        operator_id=SUBSTITUTE,
        confirmed_at=datetime.fromisoformat("2026-09-15T13:00:00+08:00"),
    )
    snap, created = core_svc.freeze_semester(db, plan_version=PLAN, freeze_id="F-1")
    assert created is True
    student = snap.students[0]
    assert student["confirmed_seconds"] == 4 * 3600
    assert student["pending_seconds"] == 0


def test_direct_confirmation_by_responsible_mentor(db_env):
    db = db_env
    _checkin(db, "E-1")
    out = svc.confirm_checkin(
        db, plan_version=PLAN, confirmation_id="C-1", checkin_event_id="E-1",
        operator_id=MENTOR,
        confirmed_at=datetime.fromisoformat("2026-09-15T13:00:00+08:00"),
    )
    assert out["authority"] == "direct"
    assert out["grant_id"] is None
    assert out["delegation_chain"] == []


# --------------------------------------------------------------------------
# 越权与过期
# --------------------------------------------------------------------------


def test_unauthorized_teacher_confirmation_is_rejected_and_not_persisted(db_env):
    db = db_env
    _grant(db, "G1", MENTOR, SUBSTITUTE,
           "2026-09-01T00:00:00+08:00", "2026-10-01T00:00:00+08:00")
    _checkin(db, "E-1")
    with pytest.raises(svc.AuthorizationError):
        svc.confirm_checkin(
            db, plan_version=PLAN, confirmation_id="C-X", checkin_event_id="E-1",
            operator_id="M9",
            confirmed_at=datetime.fromisoformat("2026-09-15T13:00:00+08:00"),
        )
    # 事件表也不应留下确认
    progress = core_svc.student_progress(db, PLAN, STUDENT)
    assert progress["pending_seconds"] == 4 * 3600


def test_expired_delegation_confirmation_is_rejected(db_env):
    db = db_env
    _grant(db, "G1", MENTOR, SUBSTITUTE,
           "2026-09-01T00:00:00+08:00", "2026-09-20T00:00:00+08:00")
    _checkin(db, "E-1")
    with pytest.raises(svc.AuthorizationError):
        svc.confirm_checkin(
            db, plan_version=PLAN, confirmation_id="C-X", checkin_event_id="E-1",
            operator_id=SUBSTITUTE,
            confirmed_at=datetime.fromisoformat("2026-09-20T00:00:00+08:00"),
        )


def test_future_delegation_is_not_yet_effective(db_env):
    db = db_env
    _grant(db, "G1", MENTOR, SUBSTITUTE,
           "2026-10-01T00:00:00+08:00", "2026-11-01T00:00:00+08:00")
    _checkin(db, "E-1")
    with pytest.raises(svc.AuthorizationError):
        svc.confirm_checkin(
            db, plan_version=PLAN, confirmation_id="C-X", checkin_event_id="E-1",
            operator_id=SUBSTITUTE,
            confirmed_at=datetime.fromisoformat("2026-09-15T13:00:00+08:00"),
        )


def test_double_confirmation_of_same_checkin_conflicts(db_env):
    db = db_env
    _checkin(db, "E-1")
    svc.confirm_checkin(
        db, plan_version=PLAN, confirmation_id="C-1", checkin_event_id="E-1",
        operator_id=MENTOR,
        confirmed_at=datetime.fromisoformat("2026-09-15T13:00:00+08:00"),
    )
    with pytest.raises(svc.DelegationConflictError):
        svc.confirm_checkin(
            db, plan_version=PLAN, confirmation_id="C-2", checkin_event_id="E-1",
            operator_id=MENTOR,
            confirmed_at=datetime.fromisoformat("2026-09-15T13:05:00+08:00"),
        )


def test_confirmation_id_is_idempotent_even_after_revocation(db_env):
    db = db_env
    _grant(db, "G1", MENTOR, SUBSTITUTE,
           "2026-09-01T00:00:00+08:00", "2026-10-01T00:00:00+08:00")
    _checkin(db, "E-1")
    kwargs = dict(
        plan_version=PLAN, confirmation_id="C-1", checkin_event_id="E-1",
        operator_id=SUBSTITUTE,
        confirmed_at=datetime.fromisoformat("2026-09-15T13:00:00+08:00"),
    )
    out1 = svc.confirm_checkin(db, **kwargs)
    svc.revoke_delegation(
        db, grant_id="G1", revoked_by=MENTOR, reason="back at desk"
    )
    # 重放旧请求不应因当前授权已撤销而失败
    out2 = svc.confirm_checkin(db, **kwargs)
    assert out1["grant_version"] == out2["grant_version"] == 1


# --------------------------------------------------------------------------
# 撤销：不溯及既往 + 并发
# --------------------------------------------------------------------------


def test_revocation_does_not_invalidate_legal_confirmation_but_blocks_new(db_env):
    db = db_env
    _grant(db, "G1", MENTOR, SUBSTITUTE,
           "2026-09-01T00:00:00+08:00", "2026-10-01T00:00:00+08:00")
    _checkin(db, "E-1")
    svc.confirm_checkin(
        db, plan_version=PLAN, confirmation_id="C-1", checkin_event_id="E-1",
        operator_id=SUBSTITUTE,
        confirmed_at=datetime.fromisoformat("2026-09-15T13:00:00+08:00"),
    )
    revoked = svc.revoke_delegation(
        db, grant_id="G1", revoked_by=MENTOR, reason="trip cut short"
    )
    assert revoked["state"] == "revoked"
    assert revoked["version"] == 2

    explained = svc.explain_confirmation(db, "C-1")
    assert explained["recorded_grant_states"] == ["active"]
    assert explained["current_grant_states"] == ["revoked"]
    assert explained["still_legally_valid"] is True
    assert explained["grant_version"] == 1

    # 撤销后新的代确认必须被拒绝
    _checkin(db, "E-2",
             check_in="2026-09-16T08:00:00+08:00",
             check_out="2026-09-16T10:00:00+08:00")
    with pytest.raises(svc.AuthorizationError):
        svc.confirm_checkin(
            db, plan_version=PLAN, confirmation_id="C-2", checkin_event_id="E-2",
            operator_id=SUBSTITUTE,
            confirmed_at=datetime.fromisoformat("2026-09-16T11:00:00+08:00"),
        )
    # 但责任导师本人仍可确认
    out = svc.confirm_checkin(
        db, plan_version=PLAN, confirmation_id="C-3", checkin_event_id="E-2",
        operator_id=MENTOR,
        confirmed_at=datetime.fromisoformat("2026-09-16T11:05:00+08:00"),
    )
    assert out["authority"] == "direct"


def test_revoking_upstream_grant_cascades_to_downstream_chain(db_env):
    db = db_env
    _grant(db, "G1", MENTOR, SUBSTITUTE,
           "2026-09-01T00:00:00+08:00", "2026-10-01T00:00:00+08:00")
    _grant(db, "G2", SUBSTITUTE, THIRD,
           "2026-09-10T00:00:00+08:00", "2026-09-20T00:00:00+08:00")

    svc.revoke_delegation(db, grant_id="G1", revoked_by=MENTOR, reason="upstream")

    g2 = db.get(DelegationGrant, "G2")
    assert g2.state == "revoked"
    assert g2.version == 2
    assert g2.revoke_reason  # 级联原因已记录
    # 版本历史保留 v1 active 与 v2 revoked
    assert db.get(DelegationGrantVersion, ("G2", 1)).state == "active"
    assert db.get(DelegationGrantVersion, ("G2", 2)).state == "revoked"

    # M3 立即失去权限
    perm = svc.check_permission(
        db, plan_version=PLAN, student_id=STUDENT, operator_id=THIRD,
        at=datetime.fromisoformat("2026-09-15T12:00:00+08:00"),
    )
    assert perm["authorized"] is False

    # 重新建立 M1->M2 不会让旧的 M2->M3 复活
    _grant(db, "G1B", MENTOR, SUBSTITUTE,
           "2026-09-22T00:00:00+08:00", "2026-10-01T00:00:00+08:00")
    perm = svc.check_permission(
        db, plan_version=PLAN, student_id=STUDENT, operator_id=THIRD,
        at=datetime.fromisoformat("2026-09-23T12:00:00+08:00"),
    )
    assert perm["authorized"] is False
    perm = svc.check_permission(
        db, plan_version=PLAN, student_id=STUDENT, operator_id=SUBSTITUTE,
        at=datetime.fromisoformat("2026-09-23T12:00:00+08:00"),
    )
    assert perm["authorized"] is True


def test_cascade_revoke_does_not_touch_confirmations_already_made(db_env):
    db = db_env
    _grant(db, "G1", MENTOR, SUBSTITUTE,
           "2026-09-01T00:00:00+08:00", "2026-10-01T00:00:00+08:00")
    _grant(db, "G2", SUBSTITUTE, THIRD,
           "2026-09-10T00:00:00+08:00", "2026-09-20T00:00:00+08:00")
    _checkin(db, "E-1")
    svc.confirm_checkin(
        db, plan_version=PLAN, confirmation_id="C-1", checkin_event_id="E-1",
        operator_id=THIRD,
        confirmed_at=datetime.fromisoformat("2026-09-15T13:00:00+08:00"),
    )
    svc.revoke_delegation(db, grant_id="G1", revoked_by=MENTOR, reason="upstream")

    explained = svc.explain_confirmation(db, "C-1")
    assert explained["still_legally_valid"] is True
    assert explained["recorded_grant_states"] == ["active", "active"]
    assert explained["current_grant_states"] == ["revoked", "revoked"]
    assert svc.reverify_confirmation(db, "C-1")["valid"] is True


def test_revoke_requires_grantor_or_responsible_mentor(db_env):
    db = db_env
    _grant(db, "G1", MENTOR, SUBSTITUTE,
           "2026-09-01T00:00:00+08:00", "2026-10-01T00:00:00+08:00")
    with pytest.raises(svc.AuthorizationError):
        svc.revoke_delegation(db, grant_id="G1", revoked_by=THIRD, reason="x")
    # grantee 自身不能撤销
    with pytest.raises(svc.AuthorizationError):
        svc.revoke_delegation(
            db, grant_id="G1", revoked_by=SUBSTITUTE, reason="x"
        )
    # grantor 可以
    svc.revoke_delegation(db, grant_id="G1", revoked_by=MENTOR, reason="ok")


def test_concurrent_revoke_wins_then_pending_confirm_is_denied(db_env):
    _grant(db_env, "G1", MENTOR, SUBSTITUTE,
           "2026-09-01T00:00:00+08:00", "2026-10-01T00:00:00+08:00")
    _checkin(db_env, "E-1")

    revoker_lock = threading.Event()
    release = threading.Event()
    outcome: dict[str, object] = {}

    def revoker():
        session = TestSessionLocal()
        try:
            row = session.get(DelegationGrant, "G1")
            row.state = "revoked"
            row.version = 2
            row.revoked_at = datetime.now(timezone.utc)
            row.revoke_reason = "concurrent"
            session.flush()  # 已持有 BEGIN IMMEDIATE 预留锁
            revoker_lock.set()
            release.wait(10)
            session.commit()
        finally:
            session.close()

    def confirmer():
        revoker_lock.wait(5)
        time.sleep(0.2)
        session = TestSessionLocal()
        try:
            svc.confirm_checkin(
                session, plan_version=PLAN, confirmation_id="C-RACE",
                checkin_event_id="E-1", operator_id=SUBSTITUTE,
                confirmed_at=datetime.fromisoformat("2026-09-15T13:00:00+08:00"),
            )
            outcome["result"] = "confirmed"
        except svc.AuthorizationError as exc:
            outcome["result"] = "denied"
            outcome["detail"] = str(exc)
        finally:
            session.close()

    t1 = threading.Thread(target=revoker)
    t2 = threading.Thread(target=confirmer)
    t1.start()
    t2.start()
    time.sleep(0.8)  # 确认方此时应阻塞在 BEGIN IMMEDIATE
    release.set()
    t1.join(10)
    t2.join(10)

    assert outcome["result"] == "denied"
    fresh = TestSessionLocal()
    try:
        assert fresh.get(DelegationGrant, "G1").state == "revoked"
        assert svc.explain_confirmation.__name__  # attr exists
        from app.delegation_repository import get_confirmation

        assert get_confirmation(fresh, confirmation_id="C-RACE") is None
    finally:
        fresh.close()


def test_concurrent_confirm_wins_then_revoke_still_keeps_record(db_env):
    _grant(db_env, "G1", MENTOR, SUBSTITUTE,
           "2026-09-01T00:00:00+08:00", "2026-10-01T00:00:00+08:00")
    _checkin(db_env, "E-1")

    confirmer_lock = threading.Event()
    release = threading.Event()
    errors: list[Exception] = []

    def confirmer():
        session = TestSessionLocal()
        try:
            session.get(DelegationGrant, "G1")  # 先拿预留锁
            confirmer_lock.set()
            release.wait(10)
            svc.confirm_checkin(
                session, plan_version=PLAN, confirmation_id="C-FIRST",
                checkin_event_id="E-1", operator_id=SUBSTITUTE,
                confirmed_at=datetime.fromisoformat("2026-09-15T13:00:00+08:00"),
            )
        except Exception as exc:  # pragma: no cover - 失败时展示
            errors.append(exc)
        finally:
            session.close()

    def revoker():
        confirmer_lock.wait(5)
        time.sleep(0.2)
        session = TestSessionLocal()
        try:
            svc.revoke_delegation(
                session, grant_id="G1", revoked_by=MENTOR, reason="late"
            )
        except Exception as exc:  # pragma: no cover
            errors.append(exc)
        finally:
            session.close()

    t1 = threading.Thread(target=confirmer)
    t2 = threading.Thread(target=revoker)
    t1.start()
    t2.start()
    time.sleep(0.8)
    release.set()
    t1.join(10)
    t2.join(10)
    assert not errors

    fresh = TestSessionLocal()
    try:
        out = svc.explain_confirmation(fresh, "C-FIRST")
        assert out["grant_version"] == 1
        assert out["current_grant_states"] == ["revoked"]
        assert out["still_legally_valid"] is True
        assert fresh.get(DelegationGrant, "G1").version == 2
    finally:
        fresh.close()


# --------------------------------------------------------------------------
# 批量：全成全败
# --------------------------------------------------------------------------


def test_concurrent_confirmations_of_same_checkin_exactly_one_wins(db_env):
    db = db_env
    _grant(db, "G1", MENTOR, SUBSTITUTE,
           "2026-09-01T00:00:00+08:00", "2026-10-01T00:00:00+08:00")
    _checkin(db, "E-1")
    db.close()

    results: list[str] = []

    def attempt(cid: str):
        session = TestSessionLocal()
        try:
            svc.confirm_checkin(
                session, plan_version=PLAN, confirmation_id=cid,
                checkin_event_id="E-1", operator_id=SUBSTITUTE,
                confirmed_at=datetime.fromisoformat("2026-09-15T13:00:00+08:00"),
            )
            results.append(f"{cid}:ok")
        except svc.DelegationConflictError:
            results.append(f"{cid}:conflict")
        except svc.DelegationError as exc:
            results.append(f"{cid}:other:{type(exc).__name__}")
        finally:
            session.close()

    threads = [
        threading.Thread(target=attempt, args=(cid,))
        for cid in ("C-A", "C-B", "C-C", "C-D")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)

    ok = sorted(r for r in results if r.endswith(":ok"))
    assert len(ok) == 1, results
    assert len(results) == 4

    fresh = TestSessionLocal()
    try:
        from app.delegation_repository import list_confirmations_for_student

        rows = list_confirmations_for_student(
            fresh, plan_version=PLAN, student_id=STUDENT
        )
        assert len(rows) == 1
    finally:
        fresh.close()


def test_batch_confirm_is_all_or_nothing(db_env):
    db = db_env
    _grant(db, "G1", MENTOR, SUBSTITUTE,
           "2026-09-01T00:00:00+08:00", "2026-10-01T00:00:00+08:00")
    _checkin(db, "E-1")
    _checkin(db, "E-2",
             check_in="2026-09-16T08:00:00+08:00",
             check_out="2026-09-16T10:00:00+08:00")
    _checkin(db, "E-3",
             check_in="2026-09-17T08:00:00+08:00",
             check_out="2026-09-17T09:00:00+08:00")

    at = datetime.fromisoformat("2026-09-17T12:00:00+08:00")
    # 第二条越权 -> 整批拒绝
    with pytest.raises(svc.AuthorizationError):
        svc.confirm_batch(
            db, plan_version=PLAN,
            confirms=[
                {"confirmation_id": "B-1", "checkin_event_id": "E-1",
                 "operator_id": SUBSTITUTE, "confirmed_at": at},
                {"confirmation_id": "B-2", "checkin_event_id": "E-2",
                 "operator_id": "M9", "confirmed_at": at},
                {"confirmation_id": "B-3", "checkin_event_id": "E-3",
                 "operator_id": MENTOR, "confirmed_at": at},
            ],
        )
    for cid in ("B-1", "B-2", "B-3"):
        with pytest.raises(svc.DelegationNotFoundError):
            svc.explain_confirmation(db, cid)
    progress = core_svc.student_progress(db, PLAN, STUDENT)
    assert progress["confirmed_seconds"] == 0
    assert progress["pending_seconds"] == 7 * 3600

    # 合法批次全部成功
    outs = svc.confirm_batch(
        db, plan_version=PLAN,
        confirms=[
            {"confirmation_id": "B-4", "checkin_event_id": "E-1",
             "operator_id": SUBSTITUTE, "confirmed_at": at},
            {"confirmation_id": "B-5", "checkin_event_id": "E-2",
             "operator_id": SUBSTITUTE, "confirmed_at": at},
            {"confirmation_id": "B-6", "checkin_event_id": "E-3",
             "operator_id": MENTOR, "confirmed_at": at},
        ],
    )
    assert len(outs) == 3
    assert {o["operator_id"] for o in outs} == {SUBSTITUTE, MENTOR}
    progress = core_svc.student_progress(db, PLAN, STUDENT)
    assert progress["pending_seconds"] == 0


def test_batch_rejects_duplicate_entries_without_persisting_any(db_env):
    db = db_env
    _checkin(db, "E-1")
    with pytest.raises(svc.ValidationError):
        svc.confirm_batch(
            db, plan_version=PLAN,
            confirms=[
                {"confirmation_id": "B-1", "checkin_event_id": "E-1",
                 "operator_id": MENTOR,
                 "confirmed_at": datetime.fromisoformat("2026-09-15T13:00:00+08:00")},
                {"confirmation_id": "B-1", "checkin_event_id": "E-1",
                 "operator_id": MENTOR,
                 "confirmed_at": datetime.fromisoformat("2026-09-15T13:01:00+08:00")},
            ],
        )
    with pytest.raises(svc.DelegationNotFoundError):
        svc.explain_confirmation(db, "B-1")


# --------------------------------------------------------------------------
# 权限查询
# --------------------------------------------------------------------------


def test_permission_query_traces_chain_and_denial(db_env):
    db = db_env
    _grant(db, "G1", MENTOR, SUBSTITUTE,
           "2026-09-01T00:00:00+08:00", "2026-10-01T00:00:00+08:00")
    _grant(db, "G2", SUBSTITUTE, THIRD,
           "2026-09-10T00:00:00+08:00", "2026-09-20T00:00:00+08:00")

    allowed = svc.check_permission(
        db, plan_version=PLAN, student_id=STUDENT, operator_id=THIRD,
        at=datetime.fromisoformat("2026-09-15T12:00:00+08:00"),
    )
    assert allowed["authorized"] is True
    assert allowed["authority"] == "delegated"
    assert [g["grant_id"] for g in allowed["matching_grants"]] == ["G1", "G2"]

    denied = svc.check_permission(
        db, plan_version=PLAN, student_id=STUDENT, operator_id="M9",
        at=datetime.fromisoformat("2026-09-15T12:00:00+08:00"),
    )
    assert denied["authorized"] is False
    assert denied["denial_reason"]


# --------------------------------------------------------------------------
# 跨时区边界
# --------------------------------------------------------------------------


def test_window_boundary_is_compared_in_utc_across_timezones(db_env):
    db = db_env
    # 窗口在上海时间 9/16 00:00 截止，即 UTC 9/15 16:00。
    _grant(db, "G1", MENTOR, SUBSTITUTE,
           "2026-09-15T00:00:00+08:00", "2026-09-16T00:00:00+08:00")
    _checkin(db, "E-1")

    one_second_before = svc.check_permission(
        db, plan_version=PLAN, student_id=STUDENT, operator_id=SUBSTITUTE,
        at=datetime.fromisoformat("2026-09-15T15:59:59+00:00"),
    )
    assert one_second_before["authorized"] is True
    at_end = svc.check_permission(
        db, plan_version=PLAN, student_id=STUDENT, operator_id=SUBSTITUTE,
        at=datetime.fromisoformat("2026-09-15T16:00:00+00:00"),
    )
    assert at_end["authorized"] is False


def test_window_declared_in_new_york_survives_storage_roundtrip(db):
    _seed_plan(db, tz="America/New_York")
    _assign(db)
    # 纽约 11/3 夏令时回退当天的窗口；以 -04:00/-05:00 明确表达。
    _grant(db, "G-NY", MENTOR, SUBSTITUTE,
           "2024-11-03T00:00:00-04:00", "2024-11-04T00:00:00-05:00")
    row = db.get(DelegationGrant, "G-NY")
    # 落库为 UTC 朴素时间：EDT 00:00 = 04:00Z
    assert row.starts_at == datetime(2024, 11, 3, 4, 0, tzinfo=timezone.utc)
    assert row.ends_at == datetime(2024, 11, 4, 5, 0, tzinfo=timezone.utc)

    # 上海侧 11/3 下午仍在纽约窗口内
    ok = svc.check_permission(
        db, plan_version=PLAN, student_id=STUDENT, operator_id=SUBSTITUTE,
        at=datetime.fromisoformat("2024-11-03T20:00:00+08:00"),
    )
    assert ok["authorized"] is True
    # 上海 11/4 13:30（UTC 05:30）已过期
    gone = svc.check_permission(
        db, plan_version=PLAN, student_id=STUDENT, operator_id=SUBSTITUTE,
        at=datetime.fromisoformat("2024-11-04T13:30:00+08:00"),
    )
    assert gone["authorized"] is False


def test_naive_datetime_payload_rejected_by_schema(client):
    r = client.post(f"/api/plans/{PLAN}/delegations", json={
        "grant_id": "G-BAD", "grantor_id": MENTOR, "grantee_id": SUBSTITUTE,
        "student_id": STUDENT,
        "starts_at": "2026-09-01T00:00:00",
        "ends_at": "2026-10-01T00:00:00",
    })
    assert r.status_code == 422


# --------------------------------------------------------------------------
# 重启后的授权校验
# --------------------------------------------------------------------------


def _build_persisted_scenario(path: str) -> None:
    eng = make_engine(f"sqlite:///{path}")
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=eng)
    Base.metadata.create_all(eng)
    db = factory()
    _seed_plan(db)
    _assign(db)
    _grant(db, "G1", MENTOR, SUBSTITUTE,
           "2026-09-01T00:00:00+08:00", "2026-10-01T00:00:00+08:00")
    _grant(db, "G2", SUBSTITUTE, THIRD,
           "2026-09-10T00:00:00+08:00", "2026-09-20T00:00:00+08:00")
    _checkin(db, "E-1")
    svc.confirm_checkin(
        db, plan_version=PLAN, confirmation_id="C-PERSIST",
        checkin_event_id="E-1", operator_id=THIRD,
        confirmed_at=datetime.fromisoformat("2026-09-15T13:00:00+08:00"),
    )
    db.close()
    eng.dispose()


def test_authorization_reverified_after_restart():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _build_persisted_scenario(path)

        # “重启”：全新引擎与会话工厂，不共享任何进程内状态
        eng2 = make_engine(f"sqlite:///{path}")
        from sqlalchemy.orm import sessionmaker

        factory2 = sessionmaker(bind=eng2)
        db = factory2()
        report = svc.reverify_confirmation(db, "C-PERSIST")
        assert report["valid"] is True
        hops = [c for c in report["checks"] if c["check"].startswith("hop")]
        assert any("G1" in c["check"] for c in hops)
        assert any("G2" in c["check"] for c in hops)
        assert all(c["passed"] for c in report["checks"])
        db.close()
        eng2.dispose()
    finally:
        os.remove(path)


def test_reverify_fails_if_version_history_tampered():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _build_persisted_scenario(path)
        eng2 = make_engine(f"sqlite:///{path}")
        from sqlalchemy.orm import sessionmaker

        factory2 = sessionmaker(bind=eng2)
        db = factory2()
        # 篡改：把固化版本 v1 的授权人改掉
        version_row = db.get(DelegationGrantVersion, ("G2", 1))
        version_row.grantee_id = "M-EVIL"
        db.commit()

        report = svc.reverify_confirmation(db, "C-PERSIST")
        assert report["valid"] is False
        failed = [c for c in report["checks"] if not c["passed"]]
        assert failed and any("G2" in c["check"] for c in failed)
        db.close()
        eng2.dispose()
    finally:
        os.remove(path)


def test_reverify_after_revoke_still_validates_historical_record(db_env):
    db = db_env
    _grant(db, "G1", MENTOR, SUBSTITUTE,
           "2026-09-01T00:00:00+08:00", "2026-10-01T00:00:00+08:00")
    _checkin(db, "E-1")
    svc.confirm_checkin(
        db, plan_version=PLAN, confirmation_id="C-1", checkin_event_id="E-1",
        operator_id=SUBSTITUTE,
        confirmed_at=datetime.fromisoformat("2026-09-15T13:00:00+08:00"),
    )
    svc.revoke_delegation(db, grant_id="G1", revoked_by=MENTOR, reason="back")

    report = svc.reverify_confirmation(db, "C-1")
    assert report["valid"] is True
    # v1 历史行仍为 active；另有 v2 撤销行
    assert db.get(DelegationGrantVersion, ("G1", 1)).state == "active"
    assert db.get(DelegationGrantVersion, ("G1", 2)).state == "revoked"
    assert report["explanation"]["current_grant_states"] == ["revoked"]
    assert report["explanation"]["still_legally_valid"] is True
