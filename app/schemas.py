"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class PlanIn(BaseModel):
    plan_version: str = Field(..., min_length=1, max_length=128)
    iana_timezone: str = Field(..., min_length=1, max_length=64)
    required_seconds: int = Field(0, ge=0)


class PlanOut(BaseModel):
    plan_version: str
    iana_timezone: str
    required_seconds: int


class CheckinPayload(BaseModel):
    activity_id: str = ""
    activity_type: str = "regular"
    check_in_at: datetime
    check_out_at: datetime

    @model_validator(mode="after")
    def _check_order(self) -> "CheckinPayload":
        if self.check_out_at <= self.check_in_at:
            raise ValueError("check_out_at must be after check_in_at")
        return self

    @field_validator("check_in_at", "check_out_at")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (RFC 3339)")
        return v


class MentorConfirmPayload(BaseModel):
    checkin_event_id: str


class LeaveCorrectionPayload(BaseModel):
    adjustment_seconds: int
    reason: str = ""


class EventIn(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=128)
    event_type: Literal["checkin", "mentor_confirm", "leave_correction"]
    student_id: str = Field(..., min_length=1, max_length=128)
    payload: dict[str, Any]


class EventBatchIn(BaseModel):
    events: list[EventIn]


class EventOut(BaseModel):
    event_id: str
    plan_version: str
    event_type: str
    student_id: str
    payload: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class ImportResult(BaseModel):
    accepted: int
    duplicates: list[str]
    rejected: list[dict[str, Any]]


class DailyTotal(BaseModel):
    academic_day: str
    seconds: int


class CheckinExplanation(BaseModel):
    event_id: str
    activity_id: str
    activity_type: str
    status: str
    counts: bool
    check_in_at_utc: str
    check_out_at_utc: str
    raw_seconds: int
    academic_days: list[dict[str, Any]]


class AdjustmentOut(BaseModel):
    event_id: str
    seconds: int
    reason: str


class StudentProgressOut(BaseModel):
    student_id: str
    confirmed_seconds: int
    pending_seconds: int
    adjustment_seconds: int
    total_seconds: int
    lesson_units: int
    pending_lesson_units: int
    meets_requirement: bool
    daily: list[DailyTotal]
    checkins: list[CheckinExplanation]
    adjustments: list[AdjustmentOut]


class SnapshotOut(BaseModel):
    plan_version: str
    freeze_id: str | None
    timezone: str
    required_seconds: int
    generated_at: str
    event_cutoff_id: str | None
    students: list[dict[str, Any]]


class FreezeIn(BaseModel):
    pass


class DiffOut(BaseModel):
    plan_version: str
    old_freeze_id: str | None
    new_freeze_id: str | None
    old_generated_at: str
    new_generated_at: str
    old_event_cutoff_id: str | None
    new_event_cutoff_id: str | None
    student_changes: list[dict[str, Any]]
    students_affected: int


# ---------------------------------------------------------------------------
# 导师委托链
# ---------------------------------------------------------------------------


class MentorAssignmentIn(BaseModel):
    student_id: str = Field(..., min_length=1, max_length=128)
    mentor_id: str = Field(..., min_length=1, max_length=128)


class MentorAssignmentOut(BaseModel):
    plan_version: str
    student_id: str
    mentor_id: str


class DelegationIn(BaseModel):
    grant_id: str = Field(..., min_length=1, max_length=128)
    grantor_id: str = Field(..., min_length=1, max_length=128)
    grantee_id: str = Field(..., min_length=1, max_length=128)
    student_id: str = Field(..., min_length=1, max_length=128)
    starts_at: datetime
    ends_at: datetime
    reason: str = Field("", max_length=512)

    @field_validator("starts_at", "ends_at")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (RFC 3339)")
        return v


class DelegationOut(BaseModel):
    grant_id: str
    plan_version: str
    grantor_id: str
    grantee_id: str
    student_id: str
    starts_at: str
    ends_at: str
    state: str
    version: int
    reason: str
    created_at: str
    revoked_at: str | None
    revoke_reason: str | None


class RevocationIn(BaseModel):
    revoked_by: str = Field(..., min_length=1, max_length=128)
    reason: str = Field(..., min_length=1, max_length=512)
    revoked_at: datetime | None = None

    @field_validator("revoked_at")
    @classmethod
    def _ensure_aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (RFC 3339)")
        return v


class PermissionQueryOut(BaseModel):
    authorized: bool
    responsible_mentor_id: str
    operator_id: str
    student_id: str
    checked_at: str
    authority: str
    denial_reason: str | None
    chain: list[dict[str, Any]]
    matching_grants: list[DelegationOut]


class ConfirmIn(BaseModel):
    confirmation_id: str = Field(..., min_length=1, max_length=128)
    checkin_event_id: str = Field(..., min_length=1, max_length=128)
    operator_id: str = Field(..., min_length=1, max_length=128)
    confirmed_at: datetime | None = None

    @field_validator("confirmed_at")
    @classmethod
    def _ensure_aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (RFC 3339)")
        return v


class BatchConfirmIn(BaseModel):
    confirms: list[ConfirmIn] = Field(..., min_length=1)


class ConfirmationOut(BaseModel):
    confirmation_id: str
    plan_version: str
    checkin_event_id: str
    student_id: str
    operator_id: str
    responsible_mentor_id: str
    authority: str
    grant_id: str | None
    grant_version: int | None
    delegation_chain: list[dict[str, Any]]
    confirmed_at: str
    created_at: str
    recorded_grant_states: list[str | None]
    current_grant_states: list[str | None]
    still_legally_valid: bool


class BatchConfirmOut(BaseModel):
    confirmed: list[ConfirmationOut]
    count: int


class ReverifyOut(BaseModel):
    confirmation_id: str
    valid: bool
    checks: list[dict[str, Any]]
    explanation: ConfirmationOut
