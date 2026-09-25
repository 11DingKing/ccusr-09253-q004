"""委托链纯领域逻辑测试：链路解析、防环、窗口边界与可达区间。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.delegation import (
    DelegationRuleError,
    Grant,
    ensure_acyclic,
    ensure_no_overlap,
    reachable_intervals,
    resolve_authority,
    validate_window,
)


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


W_START = "2026-09-01T00:00:00+00:00"
W_MID = "2026-09-15T00:00:00+00:00"
W_END = "2026-10-01T00:00:00+00:00"


def _grant(
    gid: str,
    grantor: str,
    grantee: str,
    *,
    starts_at: str = W_START,
    ends_at: str = W_END,
    state: str = "active",
    version: int = 1,
) -> Grant:
    return Grant(
        grant_id=gid,
        plan_version="P1",
        grantor_id=grantor,
        grantee_id=grantee,
        student_id="S1",
        starts_at=_dt(starts_at),
        ends_at=_dt(ends_at),
        state=state,
        version=version,
    )


def test_direct_authority_always_allowed_for_responsible_mentor():
    auth = resolve_authority(
        [],
        responsible_mentor_id="M1",
        operator_id="M1",
        student_id="S1",
        moment=_dt(W_MID),
    )
    assert auth.authorized
    assert auth.kind == "direct"
    assert auth.chain == ()


def test_chain_delegation_resolves_to_terminal_grantee():
    grants = [
        _grant("G1", "M1", "M2"),
        _grant("G2", "M2", "M3"),
        _grant("G3", "M3", "M4", starts_at="2026-09-10T00:00:00+00:00",
               ends_at="2026-09-20T00:00:00+00:00"),
    ]
    auth = resolve_authority(
        grants,
        responsible_mentor_id="M1",
        operator_id="M4",
        student_id="S1",
        moment=_dt("2026-09-15T12:00:00+00:00"),
    )
    assert auth.authorized
    assert auth.kind == "delegated"
    assert [g.grant_id for g in auth.chain] == ["G1", "G2", "G3"]
    assert auth.responsible_mentor_id == "M1"


def test_unrelated_teacher_is_denied():
    grants = [_grant("G1", "M1", "M2")]
    auth = resolve_authority(
        grants,
        responsible_mentor_id="M1",
        operator_id="M9",
        student_id="S1",
        moment=_dt(W_MID),
    )
    assert not auth.authorized
    assert auth.kind == "none"
    assert auth.denial_reason


def test_revoked_and_expired_edges_are_not_traversed():
    grants = [
        _grant("G1", "M1", "M2"),
        _grant("G2", "M2", "M3", state="revoked"),
    ]
    auth = resolve_authority(
        grants,
        responsible_mentor_id="M1",
        operator_id="M3",
        student_id="S1",
        moment=_dt(W_MID),
    )
    assert not auth.authorized

    grants = [
        _grant("G1", "M1", "M2"),
        _grant("G2", "M2", "M3", starts_at="2026-09-10T00:00:00+00:00",
               ends_at="2026-09-14T00:00:00+00:00"),
    ]
    auth = resolve_authority(
        grants,
        responsible_mentor_id="M1",
        operator_id="M3",
        student_id="S1",
        moment=_dt("2026-09-15T00:00:00+00:00"),
    )
    assert not auth.authorized


def test_window_boundaries_are_start_inclusive_end_exclusive():
    grant = _grant(
        "G1", "M1", "M2",
        starts_at="2026-09-01T00:00:00+00:00",
        ends_at="2026-09-02T00:00:00+00:00",
    )
    at_start = resolve_authority(
        [grant], responsible_mentor_id="M1", operator_id="M2",
        student_id="S1", moment=_dt("2026-09-01T00:00:00+00:00"),
    )
    assert at_start.authorized
    at_end = resolve_authority(
        [grant], responsible_mentor_id="M1", operator_id="M2",
        student_id="S1", moment=_dt("2026-09-02T00:00:00+00:00"),
    )
    assert not at_end.authorized


def test_grant_for_other_student_is_ignored():
    grant = _grant("G1", "M1", "M2")
    auth = resolve_authority(
        [grant], responsible_mentor_id="M1", operator_id="M2",
        student_id="S-OTHER", moment=_dt(W_MID),
    )
    assert not auth.authorized


def test_validate_window_rejects_empty_or_naive():
    with pytest.raises(DelegationRuleError):
        validate_window(_dt(W_END), _dt(W_START))
    with pytest.raises(ValueError):
        validate_window(datetime(2026, 9, 1), datetime(2026, 9, 2))


def test_overlapping_active_grant_for_same_grantor_student_rejected():
    existing = [_grant("G1", "M1", "M2",
                       starts_at="2026-09-01T00:00:00+00:00",
                       ends_at="2026-09-10T00:00:00+00:00")]
    with pytest.raises(DelegationRuleError):
        ensure_no_overlap(
            existing, grantor_id="M1", student_id="S1",
            starts_at=_dt("2026-09-09T00:00:00+00:00"),
            ends_at=_dt("2026-09-12T00:00:00+00:00"),
        )
    # 首尾相接不重叠
    ensure_no_overlap(
        existing, grantor_id="M1", student_id="S1",
        starts_at=_dt("2026-09-10T00:00:00+00:00"),
        ends_at=_dt("2026-09-12T00:00:00+00:00"),
    )
    # 已撤销的不阻止
    existing[0] = _grant("G1", "M1", "M2", state="revoked",
                         starts_at="2026-09-01T00:00:00+00:00",
                         ends_at="2026-09-10T00:00:00+00:00")
    ensure_no_overlap(
        existing, grantor_id="M1", student_id="S1",
        starts_at=_dt("2026-09-09T00:00:00+00:00"),
        ends_at=_dt("2026-09-12T00:00:00+00:00"),
    )


def test_cycle_is_detected_even_when_only_partial_overlap():
    # M1 -> M2 (整月), M2 -> M3 (9/10~9/20)。若 M3 -> M1 只在 9/11~9/12，
    # 该子区间内 M3 已可达 M1，仍构成环，必须拒绝。
    existing = [
        _grant("G1", "M1", "M2"),
        _grant("G2", "M2", "M3", starts_at="2026-09-10T00:00:00+00:00",
               ends_at="2026-09-20T00:00:00+00:00"),
    ]
    with pytest.raises(DelegationRuleError):
        ensure_acyclic(
            existing, grantor_id="M3", grantee_id="M1", student_id="S1",
            starts_at=_dt("2026-09-11T00:00:00+00:00"),
            ends_at=_dt("2026-09-12T00:00:00+00:00"),
        )


def test_no_cycle_when_windows_do_not_overlap():
    # M2 -> M3 在 9/10~9/20；M3 -> M1 在 9/20~9/25（端点相接），不构成环。
    existing = [
        _grant("G1", "M1", "M2"),
        _grant("G2", "M2", "M3", starts_at="2026-09-10T00:00:00+00:00",
               ends_at="2026-09-20T00:00:00+00:00"),
    ]
    ensure_acyclic(
        existing, grantor_id="M3", grantee_id="M1", student_id="S1",
        starts_at=_dt("2026-09-20T00:00:00+00:00"),
        ends_at=_dt("2026-09-25T00:00:00+00:00"),
    )


def test_reachable_intervals_clip_to_horizon_and_chains():
    edges = [
        ("M1", "M2", (_dt("2026-09-01T00:00:00+00:00"),
                      _dt("2026-10-01T00:00:00+00:00"))),
        ("M2", "M3", (_dt("2026-09-10T00:00:00+00:00"),
                      _dt("2026-09-20T00:00:00+00:00"))),
    ]
    intervals = reachable_intervals(
        edges, root="M1", target="M3",
        horizon=(_dt("2026-09-01T00:00:00+00:00"),
                 _dt("2026-10-01T00:00:00+00:00")),
    )
    assert intervals == [
        (_dt("2026-09-10T00:00:00+00:00"),
         _dt("2026-09-20T00:00:00+00:00"))
    ]
    # 裁剪到更小的视界
    intervals = reachable_intervals(
        edges, root="M1", target="M3",
        horizon=(_dt("2026-09-15T00:00:00+00:00"),
                 _dt("2026-09-25T00:00:00+00:00")),
    )
    assert intervals == [
        (_dt("2026-09-15T00:00:00+00:00"),
         _dt("2026-09-20T00:00:00+00:00"))
    ]


def test_self_delegation_is_a_trivial_cycle():
    with pytest.raises(DelegationRuleError):
        ensure_acyclic(
            [], grantor_id="M1", grantee_id="M1", student_id="S1",
            starts_at=_dt(W_START), ends_at=_dt(W_END),
        )
