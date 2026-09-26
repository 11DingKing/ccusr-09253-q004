"""委托、授权版本与确认记录的仓储访问。

所有写操作都在服务层开启的 ``BEGIN IMMEDIATE`` 事务内执行，保证
"读授权快照 -> 校验 -> 落库" 期间不会被并发的撤销/确认穿插。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .delegations import Grant
from .models import (
    Delegation,
    DelegationVersion,
    MentorAssignment,
    MentorConfirmation,
)
from .models import Event as EventModel


def as_utc(value: datetime) -> datetime:
    # SQLite 不保留时区信息，所有时间均以 UTC 落库，读回时补齐。
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def to_grant(row: Delegation) -> Grant:
    return Grant(
        grant_id=row.grant_id,
        plan_version=row.plan_version,
        student_id=row.student_id,
        delegator_id=row.delegator_id,
        grantee_id=row.grantee_id,
        starts_at=as_utc(row.starts_at),
        ends_at=as_utc(row.ends_at),
        status=row.status,
        version=row.version,
        revoked_at=as_utc(row.revoked_at) if row.revoked_at is not None else None,
    )


def get_assignment(
    tx: Session, plan_version: str, student_id: str
) -> MentorAssignment | None:
    return tx.get(MentorAssignment, (plan_version, student_id))


def upsert_assignment(
    tx: Session,
    *,
    plan_version: str,
    student_id: str,
    mentor_id: str,
    now: datetime,
) -> None:
    existing = get_assignment(tx, plan_version, student_id)
    if existing is None:
        tx.add(
            MentorAssignment(
                plan_version=plan_version,
                student_id=student_id,
                mentor_id=mentor_id,
                updated_at=now,
            )
        )
    else:
        existing.mentor_id = mentor_id
        existing.updated_at = now


def list_assignments(tx: Session, plan_version: str) -> list[MentorAssignment]:
    stmt = select(MentorAssignment).where(
        MentorAssignment.plan_version == plan_version
    )
    return list(tx.execute(stmt).scalars().all())


def get_grant(tx: Session, grant_id: str) -> Delegation | None:
    return tx.get(Delegation, grant_id)


def list_grants(
    tx: Session,
    *,
    plan_version: str,
    student_id: str | None = None,
    mentor_id: str | None = None,
) -> list[Delegation]:
    stmt = select(Delegation).where(Delegation.plan_version == plan_version)
    if student_id is not None:
        stmt = stmt.where(Delegation.student_id == student_id)
    if mentor_id is not None:
        stmt = stmt.where(
            (Delegation.delegator_id == mentor_id)
            | (Delegation.grantee_id == mentor_id)
        )
    stmt = stmt.order_by(Delegation.starts_at, Delegation.grant_id)
    return list(tx.execute(stmt).scalars().all())


def list_versions(
    tx: Session, grant_id: str
) -> list[DelegationVersion]:
    stmt = (
        select(DelegationVersion)
        .where(DelegationVersion.grant_id == grant_id)
        .order_by(DelegationVersion.version)
    )
    return list(tx.execute(stmt).scalars().all())


def insert_grant(
    tx: Session,
    *,
    grant_id: str,
    plan_version: str,
    student_id: str,
    delegator_id: str,
    grantee_id: str,
    starts_at: datetime,
    ends_at: datetime,
    actor_id: str,
    reason: str,
    now: datetime,
) -> None:
    tx.add(
        Delegation(
            grant_id=grant_id,
            plan_version=plan_version,
            student_id=student_id,
            delegator_id=delegator_id,
            grantee_id=grantee_id,
            starts_at=starts_at,
            ends_at=ends_at,
            status="active",
            version=1,
            created_by=actor_id,
            reason=reason,
            created_at=now,
        )
    )
    tx.add(
        DelegationVersion(
            grant_id=grant_id,
            version=1,
            action="create",
            plan_version=plan_version,
            student_id=student_id,
            delegator_id=delegator_id,
            grantee_id=grantee_id,
            starts_at=starts_at,
            ends_at=ends_at,
            actor_id=actor_id,
            reason=reason,
            created_at=now,
        )
    )


def mark_grant_revoked(
    tx: Session,
    row: Delegation,
    *,
    actor_id: str,
    reason: str,
    now: datetime,
) -> None:
    next_version = row.version + 1
    row.status = "revoked"
    row.version = next_version
    row.revoked_at = now
    row.revoked_by = actor_id
    row.revoke_reason = reason
    tx.add(
        DelegationVersion(
            grant_id=row.grant_id,
            version=next_version,
            action="revoke",
            plan_version=row.plan_version,
            student_id=row.student_id,
            delegator_id=row.delegator_id,
            grantee_id=row.grantee_id,
            starts_at=row.starts_at,
            ends_at=row.ends_at,
            actor_id=actor_id,
            reason=reason,
            created_at=now,
        )
    )


def get_confirmation(
    tx: Session, plan_version: str, checkin_event_id: str
) -> MentorConfirmation | None:
    stmt = (
        select(MentorConfirmation)
        .where(MentorConfirmation.plan_version == plan_version)
        .where(MentorConfirmation.checkin_event_id == checkin_event_id)
    )
    return tx.execute(stmt).scalar_one_or_none()


def list_confirmations(
    tx: Session,
    *,
    plan_version: str,
    student_id: str | None = None,
    checkin_event_ids: Iterable[str] | None = None,
) -> list[MentorConfirmation]:
    stmt = select(MentorConfirmation).where(
        MentorConfirmation.plan_version == plan_version
    )
    if student_id is not None:
        stmt = stmt.where(MentorConfirmation.student_id == student_id)
    if checkin_event_ids is not None:
        ids = list(checkin_event_ids)
        if ids:
            stmt = stmt.where(MentorConfirmation.checkin_event_id.in_(ids))
        else:
            return []
    stmt = stmt.order_by(MentorConfirmation.id)
    return list(tx.execute(stmt).scalars().all())


def insert_confirmation(tx: Session, data: dict[str, Any]) -> None:
    tx.add(MentorConfirmation(**data))


def insert_confirm_event(
    tx: Session,
    *,
    event_id: str,
    plan_version: str,
    student_id: str,
    payload: dict[str, Any],
) -> None:
    tx.add(
        EventModel(
            event_id=event_id,
            plan_version=plan_version,
            student_id=student_id,
            event_type="mentor_confirm",
            payload=payload,
        )
    )
