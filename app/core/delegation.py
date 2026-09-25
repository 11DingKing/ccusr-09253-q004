"""导师确认委托链：授权解析、防环与解释（纯领域逻辑，不依赖数据库）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence


def to_utc(value: datetime) -> datetime:
    """全部时间比较统一在 UTC 下进行，调用方必须传入带时区时间。"""
    if value.tzinfo is None:
        raise ValueError("时间必须包含时区信息")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class Grant:
    """委托授权的一个不可变版本快照。"""

    grant_id: str
    plan_version: str
    grantor_id: str
    grantee_id: str
    student_id: str
    starts_at: datetime
    ends_at: datetime
    state: str  # "active" | "revoked"
    version: int
    reason: str = ""
    revoked_at: datetime | None = None
    revoke_reason: str | None = None

    def covers_window(self, starts_at: datetime, ends_at: datetime) -> bool:
        start = to_utc(starts_at)
        end = to_utc(ends_at)
        return to_utc(self.starts_at) < end and start < to_utc(self.ends_at)

    def effective_edge(
        self, mentor_id: str, student_id: str, moment: datetime
    ) -> bool:
        """该授权在 moment 是否构成 mentor_id 向下一位老师的有效边。"""
        instant = to_utc(moment)
        return (
            self.state == "active"
            and self.grantor_id == mentor_id
            and self.student_id == student_id
            and to_utc(self.starts_at) <= instant < to_utc(self.ends_at)
        )


class DelegationRuleError(ValueError):
    """违反委托链业务约束（重叠、成环等）。"""


@dataclass(frozen=True)
class Authority:
    authorized: bool
    responsible_mentor_id: str
    operator_id: str
    student_id: str
    checked_at: datetime
    kind: str  # "direct" | "delegated" | "none"
    chain: tuple[Grant, ...] = ()
    denial_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "authorized": self.authorized,
            "responsible_mentor_id": self.responsible_mentor_id,
            "operator_id": self.operator_id,
            "student_id": self.student_id,
            "checked_at": _iso(self.checked_at),
            "authority": self.kind,
            "denial_reason": self.denial_reason,
            "chain": [grant_chain_link(g, seq) for seq, g in enumerate(self.chain, 1)],
        }


def _iso(value: datetime) -> str:
    return to_utc(value).isoformat().replace("+00:00", "Z")


def grant_chain_link(grant: Grant, seq: int) -> dict[str, Any]:
    """确认事件中固化、解释 API 中复现的链路节点格式。"""
    return {
        "seq": seq,
        "grant_id": grant.grant_id,
        "grant_version": grant.version,
        "grantor_id": grant.grantor_id,
        "grantee_id": grant.grantee_id,
        "student_id": grant.student_id,
        "starts_at": _iso(grant.starts_at),
        "ends_at": _iso(grant.ends_at),
        "state": grant.state,
        "reason": grant.reason,
    }


def validate_window(starts_at: datetime, ends_at: datetime) -> None:
    start = to_utc(starts_at)
    end = to_utc(ends_at)
    if end <= start:
        raise DelegationRuleError("委托结束时间必须晚于开始时间")


def ensure_no_overlap(
    existing: Sequence[Grant],
    *,
    grantor_id: str,
    student_id: str,
    starts_at: datetime,
    ends_at: datetime,
) -> None:
    """同一导师对同一学员在重叠时段只能有一条生效中的委托。"""
    for grant in existing:
        if (
            grant.state == "active"
            and grant.grantor_id == grantor_id
            and grant.student_id == student_id
            and grant.covers_window(starts_at, ends_at)
        ):
            raise DelegationRuleError(
                f"与生效中的委托 {grant.grant_id}（{_iso(grant.starts_at)} ~ "
                f"{_iso(grant.ends_at)}）时段重叠，同一学员不可重复委托"
            )


def _intersect_intervals(
    left: list[tuple[datetime, datetime]],
    right: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    result: list[tuple[datetime, datetime]] = []
    i = j = 0
    while i < len(left) and j < len(right):
        lo = max(left[i][0], right[j][0])
        hi = min(left[i][1], right[j][1])
        if lo < hi:
            result.append((lo, hi))
        if left[i][1] < right[j][1]:
            i += 1
        else:
            j += 1
    return result


def _merge(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            if end > last_end:
                merged[-1] = (last_start, end)
        else:
            merged.append((start, end))
    return merged


def reachable_intervals(
    edges: Sequence[tuple[str, str, tuple[datetime, datetime]]],
    *,
    root: str,
    target: str,
    horizon: tuple[datetime, datetime],
) -> list[tuple[datetime, datetime]]:
    """求 horizon 内沿 active 委托边从 root 可到达 target 的时间段集合。

    边按闭合的授权窗口传播；只要存在任意一刻 target 可达，新授权就会成环。
    """
    reach: dict[str, list[tuple[datetime, datetime]]] = {root: [horizon]}
    for _ in range(len(edges) + 2):
        changed = False
        for source, dest, window in edges:
            current = reach.get(source)
            if not current:
                continue
            additions = _intersect_intervals(current, [window])
            if not additions:
                continue
            before = reach.get(dest, [])
            merged = _merge(before + additions)
            if merged != before:
                reach[dest] = merged
                changed = True
        if not changed:
            break
    return reach.get(target, [])


def ensure_acyclic(
    existing: Sequence[Grant],
    *,
    grantor_id: str,
    grantee_id: str,
    student_id: str,
    starts_at: datetime,
    ends_at: datetime,
) -> None:
    """新增 grantor→grantee 后，grantee 在新窗口内不得能沿现有委托回到 grantor。"""
    if grantor_id == grantee_id:
        raise DelegationRuleError("委托人不能将学员委托给自己")

    horizon = (to_utc(starts_at), to_utc(ends_at))
    edges: list[tuple[str, str, tuple[datetime, datetime]]] = []
    for grant in existing:
        if grant.state != "active" or grant.student_id != student_id:
            continue
        window = (
            max(to_utc(grant.starts_at), horizon[0]),
            min(to_utc(grant.ends_at), horizon[1]),
        )
        if window[0] < window[1]:
            edges.append((grant.grantor_id, grant.grantee_id, window))

    closing = reachable_intervals(
        edges, root=grantee_id, target=grantor_id, horizon=horizon
    )
    if closing:
        cycle_start, cycle_end = closing[0]
        raise DelegationRuleError(
            "该委托会在 "
            f"{_iso(cycle_start)} ~ {_iso(cycle_end)} 形成循环委托链，已拒绝"
        )


def resolve_authority(
    grants: Sequence[Grant],
    *,
    responsible_mentor_id: str,
    operator_id: str,
    student_id: str,
    moment: datetime,
) -> Authority:
    """判断 operator_id 此刻能否代 responsible_mentor_id 确认该学员的签到。

    导师本人永远拥有直接确认权；否则沿“在该时刻有效”的委托边单向行走，
    找到操作人即授权。撤销或过期的授权不构成边，因此不影响解析。
    """
    instant = to_utc(moment)
    direct = Authority(
        authorized=True,
        responsible_mentor_id=responsible_mentor_id,
        operator_id=operator_id,
        student_id=student_id,
        checked_at=instant,
        kind="direct",
    )
    if operator_id == responsible_mentor_id:
        return direct

    # 不变量保证同一 (grantor, student, moment) 至多一条有效边；
    # 万一历史数据异常，按 grant_id 取确定的一条，拒绝隐式选择。
    adjacency: dict[str, Grant] = {}
    for grant in grants:
        if not grant.effective_edge(
            grant.grantor_id, student_id, instant
        ):
            continue
        current = adjacency.get(grant.grantor_id)
        if current is None or grant.grant_id < current.grant_id:
            adjacency[grant.grantor_id] = grant

    chain: list[Grant] = []
    visited = {responsible_mentor_id}
    cursor = responsible_mentor_id
    denial = "操作人既不是责任导师，委托链中也不存在该老师"
    while True:
        edge = adjacency.get(cursor)
        if edge is None:
            break
        chain.append(edge)
        if edge.grantee_id == operator_id:
            return Authority(
                authorized=True,
                responsible_mentor_id=responsible_mentor_id,
                operator_id=operator_id,
                student_id=student_id,
                checked_at=instant,
                kind="delegated",
                chain=tuple(chain),
            )
        if edge.grantee_id in visited:
            denial = f"委托链在 {edge.grantee_id} 处出现环路"
            chain.pop()
            break
        visited.add(edge.grantee_id)
        cursor = edge.grantee_id

    return Authority(
        authorized=False,
        responsible_mentor_id=responsible_mentor_id,
        operator_id=operator_id,
        student_id=student_id,
        checked_at=instant,
        kind="none",
        denial_reason=denial,
    )
