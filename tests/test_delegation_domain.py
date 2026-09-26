"""委托领域逻辑的单元测试：链式解析、撤销时点、过期、成环。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.delegations import (
    DelegationError,
    Grant,
    resolve_authorization,
    validate_new_grant,
)

PV = "P-1"
SID = "S1"
M0 = "mentor-root"
M1 = "mentor-a"
M2 = "mentor-b"
M3 = "mentor-c"

T0 = datetime(2024, 3, 15, 0, 0, tzinfo=timezone.utc)


def grant(
    gid: str,
    delegator: str,
    grantee: str,
    *,
    start=T0,
    end=T0 + timedelta(days=2),
    status: str = "active",
    revoked_at: datetime | None = None,
) -> Grant:
    return Grant(
        grant_id=gid,
        plan_version=PV,
        student_id=SID,
        delegator_id=delegator,
        grantee_id=grantee,
        starts_at=start,
        ends_at=end,
        status=status,
        revoked_at=revoked_at,
    )


def chain():
    return [
        grant("g1", M0, M1),
        grant("g2", M1, M2),
        grant("g3", M2, M3),
    ]


def test_responsible_mentor_self_authorizes_without_grant():
    auth = resolve_authorization(
        actor_id=M0,
        responsible_id=M0,
        plan_version=PV,
        student_id=SID,
        grants=[],
        as_of=T0 + timedelta(hours=1),
    )
    assert auth.authorized is True
    assert auth.grant is None
    assert auth.chain_length == 0


def test_chain_delegation_authorizes_leaf_only():
    grants = chain()
    at = T0 + timedelta(hours=5)
    for actor in (M1, M2, M3):
        auth = resolve_authorization(
            actor_id=actor,
            responsible_id=M0,
            plan_version=PV,
            student_id=SID,
            grants=grants,
            as_of=at,
        )
        assert auth.authorized is True, actor
    assert (
        resolve_authorization(
            actor_id="outsider",
            responsible_id=M0,
            plan_version=PV,
            student_id=SID,
            grants=grants,
            as_of=at,
        ).authorized
        is False
    )


def test_path_is_recorded_root_to_actor_with_versions():
    auth = resolve_authorization(
        actor_id=M3,
        responsible_id=M0,
        plan_version=PV,
        student_id=SID,
        grants=chain(),
        as_of=T0 + timedelta(hours=1),
    )
    assert auth.chain_length == 3
    assert [s.delegator_id for s in auth.path] == [M0, M1, M2]
    assert [s.grantee_id for s in auth.path] == [M1, M2, M3]
    assert [s.grant_id for s in auth.path] == ["g1", "g2", "g3"]
    assert auth.grant and auth.grant.grant_id == "g3"


def test_revoking_middle_link_breaks_downstream_but_keeps_upstream():
    grants = [
        grant("g1", M0, M1),
        grant("g2", M1, M2, status="revoked", revoked_at=T0 + timedelta(hours=3)),
        grant("g3", M2, M3),
    ]
    # 撤销时刻之后：M2/M3 失权，M1 仍可确认。
    after = T0 + timedelta(hours=4)
    assert (
        resolve_authorization(
            actor_id=M3,
            responsible_id=M0,
            plan_version=PV,
            student_id=SID,
            grants=grants,
            as_of=after,
        ).authorized
        is False
    )
    m1 = resolve_authorization(
        actor_id=M1,
        responsible_id=M0,
        plan_version=PV,
        student_id=SID,
        grants=grants,
        as_of=after,
    )
    assert m1.authorized is True and m1.chain_length == 1
    # 撤销时刻之前：整条链仍成立（撤销不溯及既往）。
    before = T0 + timedelta(hours=2)
    assert (
        resolve_authorization(
            actor_id=M3,
            responsible_id=M0,
            plan_version=PV,
            student_id=SID,
            grants=grants,
            as_of=before,
        ).authorized
        is True
    )


def test_window_boundaries_are_half_open():
    g = grant("g1", M0, M1, start=T0, end=T0 + timedelta(hours=1))
    assert (
        resolve_authorization(
            actor_id=M1,
            responsible_id=M0,
            plan_version=PV,
            student_id=SID,
            grants=[g],
            as_of=T0,
        ).authorized
        is True
    )
    assert (
        resolve_authorization(
            actor_id=M1,
            responsible_id=M0,
            plan_version=PV,
            student_id=SID,
            grants=[g],
            as_of=T0 - timedelta(seconds=1),
        ).authorized
        is False
    )
    # ends_at 是排他边界。
    assert (
        resolve_authorization(
            actor_id=M1,
            responsible_id=M0,
            plan_version=PV,
            student_id=SID,
            grants=[g],
            as_of=T0 + timedelta(hours=1),
        ).authorized
        is False
    )


def test_chain_requires_every_link_open_at_that_moment():
    grants = [
        grant("g1", M0, M1),
        grant("g2", M1, M2, start=T0 + timedelta(hours=2), end=T0 + timedelta(hours=4)),
        grant("g3", M2, M3),
    ]
    # 第二环尚未生效。
    assert (
        resolve_authorization(
            actor_id=M3,
            responsible_id=M0,
            plan_version=PV,
            student_id=SID,
            grants=grants,
            as_of=T0 + timedelta(hours=1),
        ).authorized
        is False
    )
    assert (
        resolve_authorization(
            actor_id=M3,
            responsible_id=M0,
            plan_version=PV,
            student_id=SID,
            grants=grants,
            as_of=T0 + timedelta(hours=3),
        ).authorized
        is True
    )
    # 第二环已过期，第三环虽在窗口内也不可达。
    assert (
        resolve_authorization(
            actor_id=M3,
            responsible_id=M0,
            plan_version=PV,
            student_id=SID,
            grants=grants,
            as_of=T0 + timedelta(hours=5),
        ).authorized
        is False
    )


def test_circular_delegation_is_rejected():
    existing = chain()
    closing = grant("g4", M3, M1)  # M3 -> M1 成环
    with pytest.raises(DelegationError, match="循环"):
        validate_new_grant(
            closing, existing, responsible_id=M0, as_of=T0 + timedelta(hours=1)
        )


def test_self_delegation_rejected_and_window_validated():
    with pytest.raises(DelegationError):
        validate_new_grant(
            grant("gx", M0, M0), [], responsible_id=M0, as_of=T0
        )
    with pytest.raises(DelegationError):
        validate_new_grant(
            grant("gx", M0, M1, start=T0 + timedelta(hours=2), end=T0),
            [],
            responsible_id=M0,
            as_of=T0,
        )


def test_delegator_must_hold_authority():
    # M3 试图把权力转授给陌生人，但 M3 当前并不在链上。
    existing = [grant("g1", M0, M1)]
    rogue = grant("gx", M3, "newbie")
    with pytest.raises(DelegationError, match="没有可转授"):
        validate_new_grant(
            rogue, existing, responsible_id=M0, as_of=T0 + timedelta(hours=1)
        )


def test_overlapping_parallel_grant_between_same_pair_rejected():
    existing = [grant("g1", M0, M1)]
    duplicate = grant(
        "g2",
        M0,
        M1,
        start=T0 + timedelta(hours=1),
        end=T0 + timedelta(hours=3),
    )
    with pytest.raises(DelegationError, match="并行授权"):
        validate_new_grant(
            duplicate, existing, responsible_id=M0, as_of=T0
        )


def test_expired_edge_does_not_count_as_cycle():
    from app.delegations import _would_create_cycle

    # 一条窗口不重叠的旧 M3->M1 边不应算作环的一部分。
    old = grant(
        "old",
        M3,
        M1,
        start=T0 - timedelta(days=4),
        end=T0 - timedelta(days=2),
    )
    fresh = [grant("g1", M0, M1), grant("g2", M1, M2), grant("g3", M2, M3)]
    closing = grant("g4", M3, M1)
    assert _would_create_cycle(closing, fresh) is True
    assert _would_create_cycle(closing, fresh + [old]) is True
    # 只有旧边（与新窗口不重叠）时不成环。
    assert _would_create_cycle(closing, [old]) is False
