"""导师委托、权限查询与授权确认 API。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from . import delegation_services as services
from .db import get_db
from .schemas import (
    BatchConfirmIn,
    BatchConfirmOut,
    ConfirmIn,
    ConfirmationOut,
    DelegationIn,
    DelegationOut,
    MentorAssignmentIn,
    MentorAssignmentOut,
    PermissionQueryOut,
    ReverifyOut,
    RevocationIn,
)

router = APIRouter(prefix="/api/plans/{plan_version}")


def _raise_domain_error(exc: Exception) -> HTTPException:
    if isinstance(exc, services.AuthorizationError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, services.ValidationError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, services.DelegationNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, services.DelegationConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@router.post(
    "/students/{student_id}/mentor",
    response_model=MentorAssignmentOut,
    status_code=status.HTTP_201_CREATED,
)
def assign_mentor(
    plan_version: str,
    student_id: str,
    body: MentorAssignmentIn,
    db: Session = Depends(get_db),
) -> Any:
    if body.student_id != student_id:
        raise HTTPException(status_code=422, detail="路径与请求体中的 student_id 不一致")
    try:
        return services.assign_responsible_mentor(
            db,
            plan_version=plan_version,
            student_id=student_id,
            mentor_id=body.mentor_id,
        )
    except services.DelegationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/delegations",
    response_model=DelegationOut,
    status_code=status.HTTP_201_CREATED,
)
def create_delegation(
    plan_version: str, body: DelegationIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.create_delegation(
            db,
            grant_id=body.grant_id,
            plan_version=plan_version,
            grantor_id=body.grantor_id,
            grantee_id=body.grantee_id,
            student_id=body.student_id,
            starts_at=body.starts_at,
            ends_at=body.ends_at,
            reason=body.reason,
        )
    except services.DelegationError as exc:
        raise _raise_domain_error(exc) from exc


@router.post("/delegations/{grant_id}/revoke", response_model=DelegationOut)
def revoke_delegation(
    plan_version: str,
    grant_id: str,
    body: RevocationIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.revoke_delegation(
            db,
            grant_id=grant_id,
            revoked_by=body.revoked_by,
            reason=body.reason,
            revoked_at=body.revoked_at,
        )
    except services.DelegationError as exc:
        raise _raise_domain_error(exc) from exc


@router.get("/delegations", response_model=list[DelegationOut])
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
    "/students/{student_id}/can-confirm",
    response_model=PermissionQueryOut,
)
def can_confirm(
    plan_version: str,
    student_id: str,
    operator_id: str = Query(...),
    at: datetime | None = Query(None),
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.check_permission(
            db,
            plan_version=plan_version,
            student_id=student_id,
            operator_id=operator_id,
            at=at,
        )
    except services.DelegationError as exc:
        raise _raise_domain_error(exc) from exc


@router.post(
    "/confirmations",
    response_model=ConfirmationOut,
    status_code=status.HTTP_201_CREATED,
)
def confirm_checkin(
    plan_version: str, body: ConfirmIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.confirm_checkin(
            db,
            plan_version=plan_version,
            confirmation_id=body.confirmation_id,
            checkin_event_id=body.checkin_event_id,
            operator_id=body.operator_id,
            confirmed_at=body.confirmed_at,
        )
    except services.DelegationError as exc:
        raise _raise_domain_error(exc) from exc


@router.post(
    "/confirmations/batch",
    response_model=BatchConfirmOut,
    status_code=status.HTTP_201_CREATED,
)
def confirm_batch(
    plan_version: str, body: BatchConfirmIn, db: Session = Depends(get_db)
) -> Any:
    try:
        result = services.confirm_batch(
            db,
            plan_version=plan_version,
            confirms=[c.model_dump() for c in body.confirms],
        )
    except services.DelegationError as exc:
        raise _raise_domain_error(exc) from exc
    return {"confirmed": result, "count": len(result)}


@router.get(
    "/confirmations/{confirmation_id}",
    response_model=ConfirmationOut,
)
def get_confirmation(
    plan_version: str, confirmation_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.explain_confirmation(
            db, confirmation_id, plan_version=plan_version
        )
    except services.DelegationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/confirmations/{confirmation_id}/explain",
    response_model=ConfirmationOut,
)
def explain_confirmation(
    plan_version: str, confirmation_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.explain_confirmation(
            db, confirmation_id, plan_version=plan_version
        )
    except services.DelegationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/confirmations/{confirmation_id}/reverify",
    response_model=ReverifyOut,
)
def reverify_confirmation(
    plan_version: str, confirmation_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.reverify_confirmation(
            db, confirmation_id, plan_version=plan_version
        )
    except services.DelegationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
