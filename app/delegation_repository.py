"""委托授权、确认事件与导师归属的持久化访问。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .core.delegation import Grant
from .models import (
    Confirmation,
    DelegationGrant,
    DelegationGrantVersion,
    MentorAssignment,
)


def upsert_mentor_assignment(
    db: Session, *, plan_version: str, student_id: str, mentor_id: str
) -> None:
    stmt = sqlite_insert(MentorAssignment).values(
        plan_version=plan_version,
        student_id=student_id,
        mentor_id=mentor_id,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["plan_version", "student_id"],
        set_={"mentor_id": mentor_id},
    )
    db.execute(stmt)


def get_mentor_assignment(
    db: Session, *, plan_version: str, student_id: str
) -> MentorAssignment | None:
    return db.get(MentorAssignment, (plan_version, student_id))


def _to_core_grant(row: DelegationGrant) -> Grant:
    return Grant(
        grant_id=row.grant_id,
        plan_version=row.plan_version,
        grantor_id=row.grantor_id,
        grantee_id=row.grantee_id,
        student_id=row.student_id,
        starts_at=row.starts_at,
        ends_at=row.ends_at,
        state=row.state,
        version=row.version,
        reason=row.reason,
        revoked_at=row.revoked_at,
        revoke_reason=row.revoke_reason,
    )


def get_grant(db: Session, grant_id: str) -> DelegationGrant | None:
    return db.get(DelegationGrant, grant_id)


def get_grant_core(db: Session, grant_id: str) -> Grant | None:
    row = get_grant(db, grant_id)
    return _to_core_grant(row) if row is not None else None


def list_all_grants_for_student(
    db: Session, *, plan_version: str, student_id: str
) -> list[Grant]:
    return [_to_core_grant(r) for r in list_grant_rows_for_student(
        db, plan_version=plan_version, student_id=student_id
    )]


def list_grant_rows_for_student(
    db: Session, *, plan_version: str, student_id: str
) -> list[DelegationGrant]:
    stmt = (
        select(DelegationGrant)
        .where(DelegationGrant.plan_version == plan_version)
        .where(DelegationGrant.student_id == student_id)
        .order_by(DelegationGrant.starts_at, DelegationGrant.grant_id)
    )
    return list(db.execute(stmt).scalars().all())


def list_grants(
    db: Session,
    *,
    plan_version: str,
    mentor_id: str | None = None,
    as_grantor: bool = True,
    as_grantee: bool = True,
) -> list[DelegationGrant]:
    stmt = select(DelegationGrant).where(
        DelegationGrant.plan_version == plan_version
    )
    if mentor_id is not None:
        clauses = []
        if as_grantor:
            clauses.append(DelegationGrant.grantor_id == mentor_id)
        if as_grantee:
            clauses.append(DelegationGrant.grantee_id == mentor_id)
        if not clauses:
            return []
        stmt = stmt.where(or_(*clauses))
    stmt = stmt.order_by(DelegationGrant.starts_at, DelegationGrant.grant_id)
    return list(db.execute(stmt).scalars().all())


def insert_grant(db: Session, grant: Grant, *, created_at: datetime) -> None:
    db.add(
        DelegationGrant(
            grant_id=grant.grant_id,
            plan_version=grant.plan_version,
            grantor_id=grant.grantor_id,
            grantee_id=grant.grantee_id,
            student_id=grant.student_id,
            starts_at=grant.starts_at,
            ends_at=grant.ends_at,
            state=grant.state,
            version=grant.version,
            reason=grant.reason,
            created_at=created_at,
        )
    )


def insert_grant_version(db: Session, row: DelegationGrant) -> None:
    db.add(
        DelegationGrantVersion(
            grant_id=row.grant_id,
            version=row.version,
            plan_version=row.plan_version,
            grantor_id=row.grantor_id,
            grantee_id=row.grantee_id,
            student_id=row.student_id,
            starts_at=row.starts_at,
            ends_at=row.ends_at,
            state=row.state,
            reason=row.reason,
            created_at=row.created_at,
            revoked_at=row.revoked_at,
            revoke_reason=row.revoke_reason,
        )
    )


def get_grant_version(
    db: Session, *, grant_id: str, version: int
) -> DelegationGrantVersion | None:
    return db.get(DelegationGrantVersion, (grant_id, version))


def list_grant_versions(
    db: Session, *, grant_id: str
) -> list[DelegationGrantVersion]:
    stmt = (
        select(DelegationGrantVersion)
        .where(DelegationGrantVersion.grant_id == grant_id)
        .order_by(DelegationGrantVersion.version)
    )
    return list(db.execute(stmt).scalars().all())


def insert_confirmation(
    db: Session,
    *,
    confirmation_id: str,
    plan_version: str,
    checkin_event_id: str,
    student_id: str,
    operator_id: str,
    responsible_mentor_id: str,
    authority: str,
    grant_id: str | None,
    grant_version: int | None,
    delegation_chain: list[dict[str, Any]],
    confirmed_at: datetime,
) -> str | None:
    """幂等插入；确认 ID 冲突返回 None，checkin 已被确认抛 UniqueViolation。"""
    stmt = sqlite_insert(Confirmation).values(
        confirmation_id=confirmation_id,
        plan_version=plan_version,
        checkin_event_id=checkin_event_id,
        student_id=student_id,
        operator_id=operator_id,
        responsible_mentor_id=responsible_mentor_id,
        authority=authority,
        grant_id=grant_id,
        grant_version=grant_version,
        delegation_chain=delegation_chain,
        confirmed_at=confirmed_at,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["confirmation_id"]
    ).returning(Confirmation.confirmation_id)
    return db.execute(stmt).scalar_one_or_none()


def get_confirmation(
    db: Session, *, confirmation_id: str
) -> Confirmation | None:
    return db.get(Confirmation, confirmation_id)


def get_confirmation_for_checkin(
    db: Session, *, plan_version: str, checkin_event_id: str
) -> Confirmation | None:
    stmt = (
        select(Confirmation)
        .where(Confirmation.plan_version == plan_version)
        .where(Confirmation.checkin_event_id == checkin_event_id)
    )
    return db.execute(stmt).scalar_one_or_none()


def list_confirmations_for_student(
    db: Session, *, plan_version: str, student_id: str
) -> list[Confirmation]:
    stmt = (
        select(Confirmation)
        .where(Confirmation.plan_version == plan_version)
        .where(Confirmation.student_id == student_id)
        .order_by(Confirmation.confirmed_at, Confirmation.confirmation_id)
    )
    return list(db.execute(stmt).scalars().all())
