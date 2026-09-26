"""委托与确认 API 的集成测试。

覆盖：链式委托确认、越权/过期拒绝、撤销不溯及既往、批量原子性、
跨时区（含 DST）窗口边界、撤销与确认的并发串行化，以及"重启"后
用全新引擎/会话重新校验授权。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.clock import clock
from app.db import get_db
from app.main import app
from app.models import MentorConfirmation
from tests.conftest import SHANGHAI_PLAN, TestSessionLocal, test_engine

PV = SHANGHAI_PLAN["plan_version"]
M0 = "mentor-root"
M1 = "mentor-a"
M2 = "mentor-b"
M3 = "mentor-c"

NOW = datetime(2024, 3, 15, 1, 0, tzinfo=timezone.utc)
WIN_START = NOW - timedelta(hours=1)
WIN_END = NOW + timedelta(hours=8)


@pytest.fixture(autouse=True)
def _frozen_clock() -> Iterator[None]:
    clock.freeze(NOW)
    yield
    clock.reset()


def _plan(client: TestClient) -> None:
    resp = client.post("/api/plans", json=SHANGHAI_PLAN)
    assert resp.status_code == 201, resp.text


def _assign(client: TestClient, student: str = "S1", mentor: str = M0) -> None:
    resp = client.put(f"/api/plans/{PV}/students/{student}/mentor", json={"mentor_id": mentor})
    assert resp.status_code == 200, resp.text


def _internship_checkin(
    client: TestClient,
    eid: str,
    student: str = "S1",
    *,
    pv: str = PV,
    start: str = "2024-03-15T08:00:00+08:00",
    end: str = "2024-03-15T12:00:00+08:00",
    activity_type: str = "internship",
) -> None:
    resp = client.post(
        f"/api/plans/{pv}/events",
        json={
            "events": [
                {
                    "event_id": eid,
                    "event_type": "checkin",
                    "student_id": student,
                    "payload": {
                        "activity_id": "A1",
                        "activity_type": activity_type,
                        "check_in_at": start,
                        "check_out_at": end,
                    },
                }
            ]
        },
    )
    assert resp.status_code == 201, resp.text


def _grant(
    client: TestClient,
    gid: str,
    delegator: str,
    grantee: str,
    *,
    student: str = "S1",
    start: datetime = WIN_START,
    end: datetime = WIN_END,
    actor: str | None = None,
):
    resp = client.post(
        f"/api/plans/{PV}/delegations",
        json={
            "grant_id": gid,
            "student_id": student,
            "delegator_id": delegator,
            "grantee_id": grantee,
            "starts_at": start.isoformat(),
            "ends_at": end.isoformat(),
            "actor_id": actor or delegator,
            "reason": "出差",
        },
    )
    assert resp.status_code in (201, 409, 422), resp.text
    return resp


def _confirm(
    client: TestClient,
    actor: str,
    eids: list[str],
    *,
    at: datetime | None = None,
):
    body = {"actor_mentor_id": actor, "checkin_event_ids": eids}
    if at is not None:
        body["at"] = at.isoformat()
    return client.post(f"/api/plans/{PV}/confirmations", json=body)


def _fresh_client() -> TestClient:
    """每个请求使用独立数据库会话（并发测试需要）。"""

    def override() -> Iterator[Session]:
        session = TestSessionLocal()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override
    return TestClient(app)


@pytest.fixture
def fresh_client() -> Iterator[TestClient]:
    client = _fresh_client()
    try:
        yield client
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------- chain


def test_delegated_chain_confirmation_records_provenance(client):
    _plan(client)
    _assign(client)
    _internship_checkin(client, "E-1")
    assert _grant(client, "g1", M0, M1).status_code == 201
    assert _grant(client, "g2", M1, M2).status_code == 201
    assert _grant(client, "g3", M2, M3).status_code == 201

    resp = _confirm(client, M3, ["E-1"])
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["count"] == 1
    record = body["confirmations"][0]
    assert record["actor_mentor_id"] == M3
    assert record["responsible_mentor_id"] == M0
    assert record["grant_id"] == "g3"
    assert record["grant_version"] == 1
    assert record["chain_length"] == 3
    assert [step["grant_id"] for step in record["delegation_path"]] == [
        "g1",
        "g2",
        "g3",
    ]

    # Replay now counts the internship hours and exposes the evidence.
    progress = client.get(f"/api/plans/{PV}/students/S1/progress").json()
    assert progress["confirmed_seconds"] == 4 * 3600
    evidence = progress["checkins"][0]["confirmation"]
    assert evidence["actor_mentor_id"] == M3
    assert evidence["responsible_mentor_id"] == M0
    assert evidence["grant_version"] == 1


def test_responsible_mentor_can_confirm_directly(client):
    _plan(client)
    _assign(client)
    _internship_checkin(client, "E-1")
    resp = _confirm(client, M0, ["E-1"])
    assert resp.status_code == 201, resp.text
    record = resp.json()["confirmations"][0]
    assert record["grant_id"] is None
    assert record["grant_version"] is None
    assert record["chain_length"] == 0


def test_unauthorized_teacher_is_rejected(client):
    _plan(client)
    _assign(client)
    _internship_checkin(client, "E-1")
    _grant(client, "g1", M0, M1)
    resp = _confirm(client, "mentor-random", ["E-1"])
    assert resp.status_code == 403
    assert resp.json()["detail"]["failures"][0]["code"] == "not_authorized"


def test_circular_delegation_chain_rejected(client):
    _plan(client)
    _assign(client)
    assert _grant(client, "g1", M0, M1).status_code == 201
    assert _grant(client, "g2", M1, M2).status_code == 201
    resp = _grant(client, "g3", M2, M0)  # closes back to the root
    assert resp.status_code == 422
    assert "循环" in resp.text


def test_delegation_requires_actual_authority(client):
    _plan(client)
    _assign(client)
    # M2 has no authority whatsoever; cannot delegate onwards.
    resp = _grant(client, "g-bogus", M2, M3)
    assert resp.status_code == 422
    assert "转授" in resp.text


def test_actor_other_than_delegator_cannot_register(client):
    _plan(client)
    _assign(client)
    resp = client.post(
        f"/api/plans/{PV}/delegations",
        json={
            "grant_id": "g1",
            "student_id": "S1",
            "delegator_id": M0,
            "grantee_id": M1,
            "starts_at": WIN_START.isoformat(),
            "ends_at": WIN_END.isoformat(),
            "actor_id": "someone-else",
            "reason": "x",
        },
    )
    assert resp.status_code == 403


# ---------------------------------------------------- expiry / windows


def test_confirmation_outside_window_is_rejected(client):
    _plan(client)
    _assign(client)
    _internship_checkin(client, "E-1")
    _grant(client, "g1", M0, M1)

    before = WIN_START - timedelta(minutes=1)
    after = WIN_END  # ends_at is an exclusive boundary
    assert _confirm(client, M1, ["E-1"], at=before).status_code == 403
    assert _confirm(client, M1, ["E-1"], at=after).status_code == 403

    ok = _confirm(client, M1, ["E-1"], at=WIN_START)
    assert ok.status_code == 201, ok.text


def test_shanghai_window_evaluated_in_utc_across_timezone(client):
    _plan(client)
    _assign(client)
    _internship_checkin(client, "E-1")
    start = datetime.fromisoformat("2024-03-15T09:00:00+08:00")
    end = datetime.fromisoformat("2024-03-15T18:00:00+08:00")
    _grant(client, "g1", M0, M1, start=start, end=end)

    # 09:00 +08 == 01:00 UTC: window opens exactly on the boundary.
    assert _confirm(client, M1, ["E-1"], at=datetime(2024, 3, 15, 0, 59, tzinfo=timezone.utc)).status_code == 403
    assert _confirm(client, M1, ["E-1"], at=datetime(2024, 3, 15, 1, 0, tzinfo=timezone.utc)).status_code == 201


def test_new_york_dst_spring_forward_window(client):
    # DST 窗口在 3 月 10 日：把时钟冻结到委托创建当时。
    clock.freeze(datetime(2024, 3, 10, 5, 30, tzinfo=timezone.utc))
    plan = dict(SHANGHAI_PLAN)
    plan["plan_version"] = "P-NY-DST"
    resp = client.post("/api/plans", json=plan)
    assert resp.status_code == 201
    client.put("/api/plans/P-NY-DST/students/S1/mentor", json={"mentor_id": M0})
    _internship_checkin(
        client,
        "E-NY",
        pv="P-NY-DST",
        start="2024-03-10T01:00:00-05:00",
        end="2024-03-10T03:30:00-05:00",
    )
    # Local 00:00->04:00 on the DST day is only 3 wall-clock hours in UTC:
    # 05:00Z -> 08:00Z (the 02:00 local hour does not exist).
    start = datetime.fromisoformat("2024-03-10T00:00:00-05:00")
    end = datetime.fromisoformat("2024-03-10T04:00:00-04:00")
    r = client.post(
        "/api/plans/P-NY-DST/delegations",
        json={
            "grant_id": "g1",
            "student_id": "S1",
            "delegator_id": M0,
            "grantee_id": M1,
            "starts_at": start.isoformat(),
            "ends_at": end.isoformat(),
            "actor_id": M0,
            "reason": "DST trip",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["starts_at_utc"] == "2024-03-10T05:00:00Z"
    assert r.json()["ends_at_utc"] == "2024-03-10T08:00:00Z"

    body = {"actor_mentor_id": M1, "checkin_event_ids": ["E-NY"]}
    body["at"] = "2024-03-10T07:59:59Z"
    assert client.post("/api/plans/P-NY-DST/confirmations", json=body).status_code == 201


# ------------------------------------------------------------- revoke


def test_revocation_blocks_future_but_keeps_past_confirmation(client):
    _plan(client)
    _assign(client)
    _internship_checkin(client, "E-1")
    _grant(client, "g1", M0, M1)
    assert _confirm(client, M1, ["E-1"]).status_code == 201

    revoke = client.post(
        "/api/delegations/g1/revoke",
        json={"actor_id": M0, "reason": "导师提前回来"},
    )
    assert revoke.status_code == 200
    body = revoke.json()
    assert body["status"] == "revoked"
    assert body["version"] == 2
    actions = [(v["version"], v["action"]) for v in body["versions"]]
    assert actions == [(1, "create"), (2, "revoke")]

    # Wall clock moves past the revocation instant.
    clock.freeze(NOW + timedelta(minutes=1))

    # A second check-in cannot be confirmed once authority is revoked.
    _internship_checkin(
        client,
        "E-2",
        start="2024-03-15T13:00:00+08:00",
        end="2024-03-15T14:00:00+08:00",
    )
    refused = _confirm(client, M1, ["E-2"])
    assert refused.status_code == 403
    assert refused.json()["detail"]["failures"][0]["code"] == "not_authorized"

    # The earlier confirmation stays valid: replay still counts it.
    progress = client.get(f"/api/plans/{PV}/students/S1/progress").json()
    assert progress["confirmed_seconds"] == 4 * 3600

    explain = client.get(f"/api/plans/{PV}/confirmations/E-1/explain").json()
    assert explain["was_authorized_at_confirmation"] is True
    assert explain["currently_authorized"] is False
    assert explain["grant_version"] == 1
    assert [v["action"] for v in explain["grant_versions"]] == ["create", "revoke"]


def test_revoking_middle_link_revokes_whole_downstream_chain(client):
    _plan(client)
    _assign(client)
    _internship_checkin(client, "E-1")
    _grant(client, "g1", M0, M1)
    _grant(client, "g2", M1, M2)
    _grant(client, "g3", M2, M3)

    client.post(
        "/api/delegations/g2/revoke",
        json={"actor_id": M1, "reason": "cut middle"},
    )
    # M3 loses authority even though g3 itself was never touched.
    assert _confirm(client, M3, ["E-1"]).status_code == 403
    # M1, directly delegated by the root, can still confirm.
    assert _confirm(client, M1, ["E-1"]).status_code == 201


def test_non_related_teacher_cannot_revoke(client):
    _plan(client)
    _assign(client)
    _grant(client, "g1", M0, M1)
    resp = client.post(
        "/api/delegations/g1/revoke",
        json={"actor_id": "outsider", "reason": "x"},
    )
    assert resp.status_code == 403


# --------------------------------------------------------- permission


def test_permission_query_reflects_window_and_revocation(client):
    _plan(client)
    _assign(client)
    _grant(client, "g1", M0, M1)

    q = client.get(
        f"/api/plans/{PV}/students/S1/authorization",
        params={"actor_mentor_id": M1},
    )
    assert q.json()["authorized"] is True

    q_before = client.get(
        f"/api/plans/{PV}/students/S1/authorization",
        params={"actor_mentor_id": M1, "at": (WIN_START - timedelta(minutes=1)).isoformat()},
    )
    assert q_before.json()["authorized"] is False

    client.post("/api/delegations/g1/revoke", json={"actor_id": M0, "reason": "x"})
    clock.freeze(NOW + timedelta(minutes=1))
    q_after = client.get(
        f"/api/plans/{PV}/students/S1/authorization",
        params={"actor_mentor_id": M1},
    )
    assert q_after.json()["authorized"] is False
    # But at a historical point (before revocation) it remains authorized.
    q_hist = client.get(
        f"/api/plans/{PV}/students/S1/authorization",
        params={"actor_mentor_id": M1, "at": NOW.isoformat()},
    )
    assert q_hist.json()["authorized"] is True


# --------------------------------------------------------- atomic batch


def test_batch_confirmation_is_all_or_nothing(client):
    _plan(client)
    _assign(client)
    _internship_checkin(client, "E-1")
    _internship_checkin(
        client,
        "E-2",
        start="2024-03-15T13:00:00+08:00",
        end="2024-03-15T15:00:00+08:00",
    )
    _grant(client, "g1", M0, M1)

    # E-1 is confirmable by M1, E-2 belongs to a student M1 cannot act for
    # (no delegation exists for S2 at all).
    client.post(
        f"/api/plans/{PV}/events",
        json={
            "events": [
                {
                    "event_id": "E-S2",
                    "event_type": "checkin",
                    "student_id": "S2",
                    "payload": {
                        "activity_id": "A1",
                        "activity_type": "internship",
                        "check_in_at": "2024-03-15T08:00:00+08:00",
                        "check_out_at": "2024-03-15T10:00:00+08:00",
                    },
                }
            ]
        },
    )
    resp = _confirm(client, M1, ["E-1", "E-S2"])
    assert resp.status_code == 403
    failures = {f["checkin_event_id"]: f["code"] for f in resp.json()["detail"]["failures"]}
    assert failures == {"E-S2": "responsible_mentor_missing"}

    # Nothing landed: both S1 check-ins are still pending and E-1 can be
    # confirmed alone afterwards.
    progress = client.get(f"/api/plans/{PV}/students/S1/progress").json()
    assert progress["pending_seconds"] == 6 * 3600
    assert _confirm(client, M1, ["E-1"]).status_code == 201


def test_batch_with_mixed_validity_reports_every_failure(client):
    _plan(client)
    _assign(client)
    _internship_checkin(client, "E-1")
    _grant(client, "g1", M0, M1)
    _confirm(client, M1, ["E-1"])
    resp = _confirm(client, M1, ["E-1", "missing-event"])
    assert resp.status_code == 403
    codes = {f["checkin_event_id"]: f["code"] for f in resp.json()["detail"]["failures"]}
    assert codes == {"E-1": "already_confirmed", "missing-event": "checkin_not_found"}


def test_batch_rejects_duplicate_ids_upfront(client):
    _plan(client)
    _assign(client)
    _internship_checkin(client, "E-1")
    resp = _confirm(client, M0, ["E-1", "E-1"])
    assert resp.status_code == 422


def test_non_internship_checkin_does_not_need_confirmation(client):
    _plan(client)
    _assign(client)
    _internship_checkin(client, "E-1", activity_type="regular")
    resp = _confirm(client, M0, ["E-1"])
    assert resp.status_code == 403
    assert resp.json()["detail"]["failures"][0]["code"] == "confirmation_not_required"


# --------------------------------------------------------- concurrency


def test_concurrent_confirm_and_revoke_are_serialized(fresh_client):
    _plan(fresh_client)
    rounds = 7
    for r in range(rounds):
        sid = f"S{r}"
        cid = f"CE-{r}"
        gid = f"cg-{r}"
        fresh_client.put(
            f"/api/plans/{PV}/students/{sid}/mentor", json={"mentor_id": M0}
        )
        _internship_checkin(fresh_client, cid, sid)
        assert _grant(fresh_client, gid, M0, M1, student=sid).status_code == 201

    outcomes: list[dict] = []
    barrier = threading.Barrier(rounds * 2)

    def do_confirm(cid: str, sid: str) -> None:
        c = _fresh_client()
        barrier.wait()
        r = _confirm(c, M1, [cid])
        outcomes.append({"round": cid, "kind": "confirm", "status": r.status_code})

    def do_revoke(gid: str) -> None:
        c = _fresh_client()
        barrier.wait()
        r = c.post(
            f"/api/delegations/{gid}/revoke",
            json={"actor_id": M0, "reason": "concurrent"},
        )
        outcomes.append({"round": gid, "kind": "revoke", "status": r.status_code})

    threads: list[threading.Thread] = []
    for r in range(rounds):
        threads.append(
            threading.Thread(target=do_confirm, args=(f"CE-{r}", f"S{r}"))
        )
        threads.append(threading.Thread(target=do_revoke, args=(f"cg-{r}",)))
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=40)
        assert not t.is_alive(), "并发事务出现死锁"

    assert len(outcomes) == rounds * 2
    by_round: dict[int, dict[str, int]] = {}
    for o in outcomes:
        idx = int(o["round"].split("-")[1])
        by_round.setdefault(idx, {})[o["kind"]] = o["status"]

    confirm_statuses = {s["confirm"] for s in by_round.values()}
    # Barrier-synchronized rounds reliably exhibit both serial orderings.
    assert confirm_statuses == {201, 403}, by_round

    with Session(test_engine) as session:
        for r, statuses in by_round.items():
            assert statuses["revoke"] == 200, statuses
            assert statuses["confirm"] in (201, 403), statuses
            stored = (
                session.query(MentorConfirmation)
                .filter(
                    MentorConfirmation.plan_version == PV,
                    MentorConfirmation.checkin_event_id == f"CE-{r}",
                )
                .one_or_none()
            )
            if statuses["confirm"] == 201:
                # Confirm won the race: the record exists, and the later
                # revocation must not have invalidated it.
                assert stored is not None
                assert stored.grant_version == 1
            else:
                # Revoke won: no confirmation landed.
                assert stored is None


# ------------------------------------------------------------- restart


def test_authorization_revalidates_after_process_restart(client, tmp_path):
    db_path = tmp_path / "restart.db"
    restart_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )

    # Boot the service once, seed data, delegate, confirm.
    from app import delegation_services as svc

    SessionOne = sessionmaker(bind=restart_engine)
    from app.models import Base

    Base.metadata.create_all(restart_engine)
    with SessionOne() as s1:
        from app import services

        services.ensure_plan(
            s1,
            plan_version=PV,
            iana_timezone=SHANGHAI_PLAN["iana_timezone"],
            required_seconds=10800,
        )
        svc.assign_mentor(s1, plan_version=PV, student_id="S1", mentor_id=M0)
        svc.create_delegation(
            s1,
            plan_version=PV,
            grant_id="g1",
            student_id="S1",
            delegator_id=M0,
            grantee_id=M1,
            starts_at=WIN_START,
            ends_at=WIN_END,
            actor_id=M0,
            reason="trip",
        )
        # Import an internship check-in through the event repository.
        from app import repository

        repository.insert_events(
            s1,
            plan_version=PV,
            events=[
                {
                    "event_id": "E-1",
                    "event_type": "checkin",
                    "student_id": "S1",
                    "payload": {
                        "activity_id": "A1",
                        "activity_type": "internship",
                        "check_in_at": "2024-03-15T08:00:00+08:00",
                        "check_out_at": "2024-03-15T12:00:00+08:00",
                    },
                }
            ],
        )
        svc.confirm_checkins(
            s1, plan_version=PV, actor_mentor_id=M1, checkin_event_ids=["E-1"]
        )

    # Simulate a full process restart: dispose the engine, open a brand-new
    # engine/session against the same database file and revalidate.
    restart_engine.dispose()
    rebooted = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    SessionTwo = sessionmaker(bind=rebooted)
    with SessionTwo() as s2:
        explanation = svc.explain_confirmation(
            s2, plan_version=PV, checkin_event_id="E-1"
        )
        assert explanation["was_authorized_at_confirmation"] is True
        assert explanation["currently_authorized"] is True
        assert explanation["actor_mentor_id"] == M1
        assert explanation["responsible_mentor_id"] == M0
        assert explanation["grant_version"] == 1

        # Revocation after restart still cannot erase the old confirmation.
        clock.freeze(NOW + timedelta(minutes=1))
        svc.revoke_delegation(
            s2, grant_id="g1", actor_id=M0, reason="back in office"
        )
        explanation2 = svc.explain_confirmation(
            s2, plan_version=PV, checkin_event_id="E-1"
        )
        assert explanation2["was_authorized_at_confirmation"] is True
        assert explanation2["currently_authorized"] is False
        permission = svc.check_authorization(
            s2, plan_version=PV, student_id="S1", actor_mentor_id=M1
        )
        assert permission["authorized"] is False
    rebooted.dispose()
