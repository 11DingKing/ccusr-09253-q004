"""委托链 REST API 端到端测试。"""

from __future__ import annotations

from tests.conftest import SHANGHAI_PLAN

P = SHANGHAI_PLAN["plan_version"]
BASE = f"/api/plans/{P}"


def _prepare(client) -> None:
    assert client.post("/api/plans", json=SHANGHAI_PLAN).status_code == 201
    r = client.post(f"{BASE}/students/S1/mentor", json={"student_id": "S1", "mentor_id": "M1"})
    assert r.status_code == 201, r.text
    r = client.post(f"{BASE}/students/S2/mentor", json={"student_id": "S2", "mentor_id": "M1"})
    assert r.status_code == 201, r.text


def _checkin(client, eid, student="S1"):
    r = client.post(f"{BASE}/events", json={"events": [{
        "event_id": eid, "event_type": "checkin", "student_id": student,
        "payload": {"activity_id": "A1", "activity_type": "internship",
                    "check_in_at": "2026-09-15T08:00:00+08:00",
                    "check_out_at": "2026-09-15T12:00:00+08:00"}}]})
    assert r.status_code == 201, r.text


def _delegate(client, gid, grantor, grantee, student="S1",
              start="2026-09-01T00:00:00+08:00",
              end="2026-10-01T00:00:00+08:00", expected=201):
    r = client.post(f"{BASE}/delegations", json={
        "grant_id": gid, "grantor_id": grantor, "grantee_id": grantee,
        "student_id": student, "starts_at": start, "ends_at": end,
        "reason": "business trip"})
    assert r.status_code == expected, r.text
    return r


def test_create_revoke_list_and_query_permissions(client):
    _prepare(client)
    r = _delegate(client, "G1", "M1", "M2")
    body = r.json()
    assert body["state"] == "active"
    assert body["version"] == 1
    assert body["grantor_id"] == "M1"

    # 列表
    r = client.get(f"{BASE}/delegations", params={"student_id": "S1"})
    assert [g["grant_id"] for g in r.json()] == ["G1"]

    # 权限查询：链路
    r = client.get(f"{BASE}/students/S1/can-confirm",
                   params={"operator_id": "M2", "at": "2026-09-15T12:00:00+08:00"})
    assert r.json()["authorized"] is True
    assert r.json()["authority"] == "delegated"

    # 撤销
    r = client.post(f"{BASE}/delegations/G1/revoke",
                    json={"revoked_by": "M1", "reason": "returned"})
    assert r.status_code == 200
    assert r.json()["state"] == "revoked"
    assert r.json()["version"] == 2

    # 撤销后权限消失
    r = client.get(f"{BASE}/students/S1/can-confirm",
                   params={"operator_id": "M2", "at": "2026-09-15T12:00:00+08:00"})
    assert r.json()["authorized"] is False

    # 重复撤销 -> 409
    r = client.post(f"{BASE}/delegations/G1/revoke",
                    json={"revoked_by": "M1", "reason": "again"})
    assert r.status_code == 409


def test_circular_and_overlapping_delegations_are_409(client):
    _prepare(client)
    _delegate(client, "G1", "M1", "M2")
    _delegate(client, "G2", "M2", "M3",
              start="2026-09-10T00:00:00+08:00", end="2026-09-20T00:00:00+08:00")
    # 成环
    _delegate(client, "G3", "M3", "M2",
              start="2026-09-11T00:00:00+08:00", end="2026-09-19T00:00:00+08:00",
              expected=409)
    # 同人同学员重叠
    _delegate(client, "G4", "M1", "M4",
              start="2026-09-15T00:00:00+08:00", end="2026-09-25T00:00:00+08:00",
              expected=409)
    # 自委托
    _delegate(client, "G5", "M1", "M1", expected=422)
    # 不存在的 plan
    r = client.post("/api/plans/NOPE/delegations", json={
        "grant_id": "GX", "grantor_id": "M1", "grantee_id": "M2",
        "student_id": "S1", "starts_at": "2026-09-01T00:00:00+08:00",
        "ends_at": "2026-10-01T00:00:00+08:00"})
    assert r.status_code == 404


def test_delegation_without_mentor_assignment_is_403(client):
    assert client.post("/api/plans", json=SHANGHAI_PLAN).status_code == 201
    _delegate(client, "G1", "M1", "M2", student="S-UNASSIGNED", expected=403)


def test_confirm_endpoint_happy_path_and_denials(client):
    _prepare(client)
    _delegate(client, "G1", "M1", "M2")
    _checkin(client, "E-1")

    r = client.post(f"{BASE}/confirmations", json={
        "confirmation_id": "C-1", "checkin_event_id": "E-1",
        "operator_id": "M2", "confirmed_at": "2026-09-15T13:00:00+08:00"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["operator_id"] == "M2"
    assert body["responsible_mentor_id"] == "M1"
    assert body["grant_version"] == 1
    assert body["delegation_chain"][0]["grant_id"] == "G1"

    # 解释接口
    r = client.get(f"{BASE}/confirmations/C-1/explain")
    assert r.status_code == 200
    assert r.json()["still_legally_valid"] is True

    # 复核接口
    r = client.get(f"{BASE}/confirmations/C-1/reverify")
    assert r.status_code == 200
    assert r.json()["valid"] is True

    # 越权 -> 403
    r = client.post(f"{BASE}/confirmations", json={
        "confirmation_id": "C-X", "checkin_event_id": "E-1",
        "operator_id": "M9", "confirmed_at": "2026-09-15T13:01:00+08:00"})
    assert r.status_code == 403

    # 重复确认 -> 409
    r = client.post(f"{BASE}/confirmations", json={
        "confirmation_id": "C-2", "checkin_event_id": "E-1",
        "operator_id": "M1", "confirmed_at": "2026-09-15T13:02:00+08:00"})
    assert r.status_code == 409

    # 幂等重放 -> 200 语义上返回同一记录（POST 返回 201 亦可，内容一致）
    r = client.post(f"{BASE}/confirmations", json={
        "confirmation_id": "C-1", "checkin_event_id": "E-1",
        "operator_id": "M2", "confirmed_at": "2026-09-15T13:00:00+08:00"})
    assert r.status_code in (200, 201)
    assert r.json()["grant_version"] == 1

    # 确认不存在的签到 -> 404
    r = client.post(f"{BASE}/confirmations", json={
        "confirmation_id": "C-Y", "checkin_event_id": "E-NOPE",
        "operator_id": "M1", "confirmed_at": "2026-09-15T13:00:00+08:00"})
    assert r.status_code == 404


def test_revocation_after_confirmation_keeps_record_valid(client):
    _prepare(client)
    _delegate(client, "G1", "M1", "M2")
    _checkin(client, "E-1")
    r = client.post(f"{BASE}/confirmations", json={
        "confirmation_id": "C-1", "checkin_event_id": "E-1",
        "operator_id": "M2", "confirmed_at": "2026-09-15T13:00:00+08:00"})
    assert r.status_code == 201

    client.post(f"{BASE}/delegations/G1/revoke",
                json={"revoked_by": "M1", "reason": "back"})

    r = client.get(f"{BASE}/confirmations/C-1/explain")
    body = r.json()
    assert body["recorded_grant_states"] == ["active"]
    assert body["current_grant_states"] == ["revoked"]
    assert body["still_legally_valid"] is True

    r = client.get(f"{BASE}/confirmations/C-1/reverify")
    assert r.json()["valid"] is True


def test_batch_endpoint_is_atomic_over_http(client):
    _prepare(client)
    _delegate(client, "G1", "M1", "M2")
    _checkin(client, "E-1")
    _checkin(client, "E-2")
    _checkin(client, "E-3")

    # 含越权项 -> 403 且全部不落库
    r = client.post(f"{BASE}/confirmations/batch", json={"confirms": [
        {"confirmation_id": "B-1", "checkin_event_id": "E-1",
         "operator_id": "M2", "confirmed_at": "2026-09-15T18:00:00+08:00"},
        {"confirmation_id": "B-2", "checkin_event_id": "E-2",
         "operator_id": "M9", "confirmed_at": "2026-09-15T18:00:00+08:00"},
        {"confirmation_id": "B-3", "checkin_event_id": "E-3",
         "operator_id": "M1", "confirmed_at": "2026-09-15T18:00:00+08:00"}]})
    assert r.status_code == 403
    for cid in ("B-1", "B-2", "B-3"):
        assert client.get(f"{BASE}/confirmations/{cid}").status_code == 404

    # 全合法 -> 201，三条均落库
    r = client.post(f"{BASE}/confirmations/batch", json={"confirms": [
        {"confirmation_id": "B-4", "checkin_event_id": "E-1",
         "operator_id": "M2", "confirmed_at": "2026-09-15T18:00:00+08:00"},
        {"confirmation_id": "B-5", "checkin_event_id": "E-2",
         "operator_id": "M2", "confirmed_at": "2026-09-15T18:00:00+08:00"},
        {"confirmation_id": "B-6", "checkin_event_id": "E-3",
         "operator_id": "M1", "confirmed_at": "2026-09-15T18:00:00+08:00"}]})
    assert r.status_code == 201, r.text
    assert r.json()["count"] == 3


def test_scoped_delegation_does_not_leak_across_students(client):
    _prepare(client)
    _delegate(client, "G1", "M1", "M2", student="S1")
    _checkin(client, "E-S2", student="S2")
    r = client.post(f"{BASE}/confirmations", json={
        "confirmation_id": "C-1", "checkin_event_id": "E-S2",
        "operator_id": "M2", "confirmed_at": "2026-09-15T13:00:00+08:00"})
    assert r.status_code == 403


def test_grantee_cannot_revoke(client):
    _prepare(client)
    _delegate(client, "G1", "M1", "M2")
    r = client.post(f"{BASE}/delegations/G1/revoke",
                    json={"revoked_by": "M2", "reason": "i quit"})
    assert r.status_code == 403


def test_regular_non_internship_checkin_cannot_be_confirmed(client):
    _prepare(client)
    r = client.post(f"{BASE}/events", json={"events": [{
        "event_id": "E-R", "event_type": "checkin", "student_id": "S1",
        "payload": {"activity_id": "A1", "activity_type": "regular",
                    "check_in_at": "2026-09-15T08:00:00+08:00",
                    "check_out_at": "2026-09-15T10:00:00+08:00"}}]})
    assert r.status_code == 201
    r = client.post(f"{BASE}/confirmations", json={
        "confirmation_id": "C-R", "checkin_event_id": "E-R",
        "operator_id": "M1", "confirmed_at": "2026-09-15T13:00:00+08:00"})
    assert r.status_code == 422


def test_naive_datetime_query_parameter_is_422(client):
    _prepare(client)
    r = client.get(f"{BASE}/students/S1/can-confirm",
                   params={"operator_id": "M1", "at": "2026-09-15T12:00:00"})
    assert r.status_code == 422
