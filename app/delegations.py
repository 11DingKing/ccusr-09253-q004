"""导师确认委托的纯领域逻辑。

委托是有期限、不可循环的单向授权：导师（delegator）在某个时间窗口内把
指定学生的实习确认权委托给另一位老师（grantee）。受托老师可以在自己持有
授权的窗口内继续向下委托，从而形成有限长度的委托链。

本模块不接触数据库与 HTTP：所有判定都基于调用方给出的不可变授权快照和
``as_of`` 时刻，因此撤销后的重放、重启后的重新校验都能得到确定结论。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Sequence


class DelegationError(ValueError):
    """委托或确认违反业务约束。"""


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise DelegationError("时间戳必须包含时区")
    return value.astimezone(timezone.utc)


def _window_active(
    starts_at: datetime,
    ends_at: datetime,
    revoked_at: datetime | None,
    as_of: datetime,
) -> bool:
    """授权在某一历史时刻是否有效（撤销不溯及既往）。"""
    moment = as_utc(as_of)
    if moment < as_utc(starts_at) or moment >= as_utc(ends_at):
        return False
    if revoked_at is not None and moment >= as_utc(revoked_at):
        return False
    return True


@dataclass(frozen=True)
class Grant:
    """一版委托授权的不可变快照。"""

    grant_id: str
    plan_version: str
    student_id: str
    delegator_id: str
    grantee_id: str
    starts_at: datetime
    ends_at: datetime
    status: str = "active"
    version: int = 1
    revoked_at: datetime | None = None

    def active_at(self, as_of: datetime) -> bool:
        return self.status == "active" and _window_active(
            self.starts_at, self.ends_at, self.revoked_at, as_of
        )

    def valid_at(self, as_of: datetime) -> bool:
        """授权在某一历史时刻是否成立（只看窗口与撤销时点）。

        撤销不溯及既往：一条当前已撤销的授权，在撤销时刻之前仍然成立。
        """
        return _window_active(
            self.starts_at, self.ends_at, self.revoked_at, as_of
        )


@dataclass(frozen=True)
class ChainStep:
    grant_id: str
    delegator_id: str
    grantee_id: str
    version: int
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True)
class Authorization:
    """一次确认所依据的授权解析结果。"""

    authorized: bool
    actor_id: str
    responsible_id: str
    student_id: str
    grant: Grant | None
    path: tuple[ChainStep, ...] = field(default_factory=tuple)
    reason: str = ""

    @property
    def chain_length(self) -> int:
        return len(self.path)

    def to_explanation(self) -> dict[str, object]:
        if self.grant is None:
            return {
                "authorized": self.authorized,
                "reason": self.reason,
                "actor_mentor_id": self.actor_id,
                "responsible_mentor_id": self.responsible_id,
                "student_id": self.student_id,
                "chain_length": 0,
                "chain": [],
            }
        return {
            "authorized": self.authorized,
            "reason": self.reason,
            "actor_mentor_id": self.actor_id,
            "responsible_mentor_id": self.responsible_id,
            "student_id": self.student_id,
            "grant_id": self.grant.grant_id,
            "grant_version": self.grant.version,
            "chain_length": len(self.path),
            "chain": [
                {
                    "grant_id": step.grant_id,
                    "delegator_id": step.delegator_id,
                    "grantee_id": step.grantee_id,
                    "grant_version": step.version,
                    "starts_at_utc": as_utc(step.starts_at)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "ends_at_utc": as_utc(step.ends_at)
                    .isoformat()
                    .replace("+00:00", "Z"),
                }
                for step in self.path
            ],
        }


def validate_window(starts_at: datetime, ends_at: datetime) -> None:
    start = as_utc(starts_at)
    end = as_utc(ends_at)
    if end <= start:
        raise DelegationError("委托结束时间必须晚于开始时间")


def validate_new_grant(
    grant: Grant,
    grants: Iterable[Grant],
    *,
    responsible_id: str | None,
    as_of: datetime,
) -> None:
    """创建委托前的静态校验：自我委托、授权来源、窗口重叠、成环。"""
    moment = as_utc(as_of)
    if grant.delegator_id == grant.grantee_id:
        raise DelegationError("不能把学生委托给自己")
    if moment >= as_utc(grant.ends_at):
        raise DelegationError("委托窗口在创建时已经过期")

    others = [
        g
        for g in grants
        if g.plan_version == grant.plan_version
        and g.student_id == grant.student_id
        and g.grant_id != grant.grant_id
    ]

    # 只有责任导师本人、或在新委托开始时持有该学生有效授权的老师才能转授
    # （允许提前登记：上游委托与本委托都尚未生效，但窗口起点处链路成立）。
    if grant.delegator_id != responsible_id:
        upstream = resolve_authorization(
            actor_id=grant.delegator_id,
            responsible_id=responsible_id or "",
            plan_version=grant.plan_version,
            student_id=grant.student_id,
            grants=others,
            as_of=as_utc(grant.starts_at),
        )
        if not upstream.authorized:
            raise DelegationError("委托人对该学生没有可转授的确认权")

    # 同一 (delegator, grantee) 在重叠窗口内不允许重复授权，避免并行链分叉。
    for g in others:
        if (
            g.status == "active"
            and g.delegator_id == grant.delegator_id
            and g.grantee_id == grant.grantee_id
            and _windows_overlap(
                grant.starts_at, grant.ends_at, g.starts_at, g.ends_at
            )
        ):
            raise DelegationError(
                f"委托 {g.grant_id} 与新委托窗口重叠，存在并行授权"
            )

    # 不可循环：新边 delegator -> grantee 不得让授权关系图成环
    # （只考虑与新窗口时间上重叠的有效边）。
    if _would_create_cycle(grant, others):
        raise DelegationError("委托链不允许出现循环")


def _windows_overlap(
    start_a: datetime,
    end_a: datetime,
    start_b: datetime,
    end_b: datetime,
) -> bool:
    return as_utc(start_a) < as_utc(end_b) and as_utc(start_b) < as_utc(end_a)


def _would_create_cycle(new_edge: Grant, grants: Sequence[Grant]) -> bool:
    adjacency: dict[str, list[str]] = {}
    for g in grants:
        if g.status != "active":
            continue
        if not _windows_overlap(
            new_edge.starts_at, new_edge.ends_at, g.starts_at, g.ends_at
        ):
            continue
        adjacency.setdefault(g.delegator_id, []).append(g.grantee_id)
    adjacency.setdefault(new_edge.delegator_id, []).append(new_edge.grantee_id)

    # 从新边的终点出发，若能沿有效边回到起点，则成环。
    stack = [new_edge.grantee_id]
    seen: set[str] = set()
    while stack:
        node = stack.pop()
        if node == new_edge.delegator_id:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(adjacency.get(node, []))
    return False


def resolve_authorization(
    *,
    actor_id: str,
    responsible_id: str,
    plan_version: str,
    student_id: str,
    grants: Sequence[Grant],
    as_of: datetime,
) -> Authorization:
    """解析 actor 能否在 as_of 时刻确认该学生的实习签到。

    返回的路径从责任导师开始逐级向下，最后一步的 grantee 必须是实际操作人。
    链上每一环都要在该时刻处于有效窗口内且未被撤销。
    """
    moment = as_utc(as_of)
    scoped = [
        g
        for g in grants
        if g.plan_version == plan_version and g.student_id == student_id
    ]

    def denied(reason: str) -> Authorization:
        return Authorization(
            authorized=False,
            actor_id=actor_id,
            responsible_id=responsible_id,
            student_id=student_id,
            grant=None,
            reason=reason,
        )

    if not responsible_id:
        return denied("学生尚未登记责任导师")
    if actor_id == responsible_id:
        return Authorization(
            authorized=True,
            actor_id=actor_id,
            responsible_id=responsible_id,
            student_id=student_id,
            grant=None,
            reason="责任导师本人确认",
        )

    # BFS 寻找责任导师 -> 实际操作人的有效委托路径。
    outgoing: dict[str, list[Grant]] = {}
    for g in scoped:
        if g.valid_at(moment):
            outgoing.setdefault(g.delegator_id, []).append(g)

    queue: list[tuple[str, tuple[ChainStep, ...]]] = [(responsible_id, ())]
    visited: set[str] = {responsible_id}
    while queue:
        current, path = queue.pop(0)
        for edge in outgoing.get(current, []):
            step = ChainStep(
                grant_id=edge.grant_id,
                delegator_id=edge.delegator_id,
                grantee_id=edge.grantee_id,
                version=edge.version,
                starts_at=edge.starts_at,
                ends_at=edge.ends_at,
            )
            next_path = path + (step,)
            if edge.grantee_id == actor_id:
                return Authorization(
                    authorized=True,
                    actor_id=actor_id,
                    responsible_id=responsible_id,
                    student_id=student_id,
                    grant=edge,
                    path=next_path,
                    reason=(
                        f"经 {len(next_path)} 级委托授权"
                        if next_path
                        else "直接委托授权"
                    ),
                )
            if edge.grantee_id not in visited:
                visited.add(edge.grantee_id)
                queue.append((edge.grantee_id, next_path))

    return denied("实际操作人不在该学生的有效委托链上")


def describe_grant(grant: Grant, *, now: datetime) -> dict[str, object]:
    moment = as_utc(now)
    if grant.status == "revoked":
        effective = False
        state = "revoked"
    elif moment < as_utc(grant.starts_at):
        effective = False
        state = "scheduled"
    elif moment >= as_utc(grant.ends_at):
        effective = False
        state = "expired"
    else:
        effective = True
        state = "active"
    return {
        "grant_id": grant.grant_id,
        "plan_version": grant.plan_version,
        "student_id": grant.student_id,
        "delegator_id": grant.delegator_id,
        "grantee_id": grant.grantee_id,
        "version": grant.version,
        "status": state,
        "starts_at_utc": as_utc(grant.starts_at).isoformat().replace("+00:00", "Z"),
        "ends_at_utc": as_utc(grant.ends_at).isoformat().replace("+00:00", "Z"),
        "revoked_at_utc": (
            as_utc(grant.revoked_at).isoformat().replace("+00:00", "Z")
            if grant.revoked_at is not None
            else None
        ),
        "effective_now": effective,
    }
