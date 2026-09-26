"""导师委托与确认的 HTTP 接口。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from . import delegation_services as services
from .db import get_db
from .schemas import (
    ConfirmBatchIn,
    DelegationIn,
    MentorAssignmentIn,
    RevokeIn,
)

router = APIRouter(prefix="/api")


def _http_for(exc: Exception) -> HTTPException:
    mapping = {
        services.DelegationNotFoundError: 404,
        services.ResponsibleMentorNotFoundError: 409,
        services.GrantConflictError: 409,
        services.PermissionDeniedError: 403,
        services.DelegationError: 422,
    }
    for error_type, code in mapping.items():
        if isinstance(exc, error_type):
            return HTTPException(status_code=code, detail=str(exc))
    raise exc


@router.put(
    "/plans/{plan_version}/students/{student_id}/mentor",
    status_code=status.HTTP_200_OK,
)
def assign_mentor(
    plan_version: str,
    student_id: str,
    body: MentorAssignmentIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.assign_mentor(
            db,
            plan_version=plan_version,
            student_id=student_id,
            mentor_id=body.mentor_id,
        )
    except services.DelegationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/plans/{plan_version}/delegations",
    status_code=status.HTTP_201_CREATED,
)
def create_delegation(
    plan_version: str, body: DelegationIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.create_delegation(
            db,
            plan_version=plan_version,
            grant_id=body.grant_id,
            student_id=body.student_id,
            delegator_id=body.delegator_id,
            grantee_id=body.grantee_id,
            starts_at=body.starts_at,
            ends_at=body.ends_at,
            actor_id=body.actor_id,
            reason=body.reason,
        )
    except Exception as exc:  # noqa: BLE001 - 统一映射领域错误
        raise _http_for(exc) from exc


@router.post("/delegations/{grant_id}/revoke")
def revoke_delegation(
    grant_id: str, body: RevokeIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.revoke_delegation(
            db,
            grant_id=grant_id,
            actor_id=body.actor_id,
            reason=body.reason,
        )
    except Exception as exc:  # noqa: BLE001
        raise _http_for(exc) from exc


@router.get("/delegations/{grant_id}")
def get_delegation(grant_id: str, db: Session = Depends(get_db)) -> Any:
    try:
        return services.get_delegation(db, grant_id)
    except services.DelegationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/plans/{plan_version}/delegations")
def list_delegations(
    plan_version: str,
    student_id: str | None = Query(None),
    mentor_id: str | None = Query(None),
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.list_delegations(
            db,
            plan_version=plan_version,
            student_id=student_id,
            mentor_id=mentor_id,
        )
    except services.DelegationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/students/{student_id}/authorization",
)
def check_authorization(
    plan_version: str,
    student_id: str,
    actor_mentor_id: str = Query(...),
    at: datetime | None = Query(None),
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.check_authorization(
            db,
            plan_version=plan_version,
            student_id=student_id,
            actor_mentor_id=actor_mentor_id,
            at=at,
        )
    except services.DelegationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/plans/{plan_version}/confirmations",
    status_code=status.HTTP_201_CREATED,
)
def confirm_checkins(
    plan_version: str, body: ConfirmBatchIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.confirm_checkins(
            db,
            plan_version=plan_version,
            actor_mentor_id=body.actor_mentor_id,
            checkin_event_ids=body.checkin_event_ids,
            at=body.at,
        )
    except services.ConfirmationRejectedError as exc:
        raise HTTPException(
            status_code=403,
            detail={"message": str(exc), "failures": exc.failures},
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise _http_for(exc) from exc


@router.get(
    "/plans/{plan_version}/confirmations/{checkin_event_id}/explain",
)
def explain_confirmation(
    plan_version: str,
    checkin_event_id: str,
    at: datetime | None = Query(None),
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.explain_confirmation(
            db,
            plan_version=plan_version,
            checkin_event_id=checkin_event_id,
            at=at,
        )
    except services.DelegationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
