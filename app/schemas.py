from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Code = Annotated[str, StringConstraints(pattern=r"^(?:[0-9]{5}|[A-Z][0-9]{4}|[0-9]{4}[A-Z])$")]
Diagnosis = Annotated[
    str, StringConstraints(pattern=r"^[A-TV-Z][0-9][A-Z0-9](?:\.[A-Z0-9]{1,4})?$")
]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProcedureInput(Input):
    code: Code
    units: Annotated[int, Field(strict=True, ge=1, le=30)]


class Submission(Input):
    member_id: Annotated[str, Field(min_length=1, max_length=100)]
    diagnosis: Diagnosis
    service_date: date
    procedures: Annotated[list[ProcedureInput], Field(min_length=1, max_length=100)]
    clinical_note: Annotated[str | None, Field(max_length=20000)] = None

    @model_validator(mode="after")
    def unique_codes(self):
        codes = [item.code for item in self.procedures]
        if len(codes) != len(set(codes)):
            raise ValueError("Combine units for duplicate procedure codes")
        return self


class ProcedureDecision(Input):
    procedure_id: UUID
    decision: Literal["approved", "denied"]
    reason: Reason | None = None

    @model_validator(mode="after")
    def denial_reason(self):
        if self.decision == "denied" and self.reason is None:
            raise ValueError("A denial requires a nonblank reason")
        return self


class DecisionInput(Input):
    procedures: Annotated[list[ProcedureDecision], Field(min_length=1, max_length=100)]


class Reassignment(Input):
    reviewer_id: Annotated[str, Field(min_length=1, max_length=100)]


class MemberProcedure(BaseModel):
    id: UUID
    code: str
    units: int
    decision: Literal["pending", "approved", "denied"]


class InternalProcedure(MemberProcedure):
    reason: str | None
    decided_by: str | None
    decided_at: datetime | None


class MemberRequest(BaseModel):
    id: UUID
    member_id: str
    diagnosis: str
    service_date: date
    status: Literal["queued", "in_review", "decided"]
    created_at: datetime
    procedures: list[MemberProcedure]


class OfficeRequest(MemberRequest):
    office_id: str
    clinical_note: str | None
    decided_at: datetime | None


class InternalRequest(OfficeRequest):
    reviewer_id: str | None
    procedures: list[InternalProcedure]


class AuditEvent(BaseModel):
    id: int
    request_id: UUID
    actor_id: str
    action: str
    occurred_at: datetime
    trace_id: UUID
    details: dict


class AccessEvent(BaseModel):
    trace_id: UUID
    actor_id: str | None
    method: str
    route: str
    status: int
    error_codes: list[str]
    occurred_at: datetime


class ErrorItem(BaseModel):
    field: str
    code: str
    message: str


class ErrorResponse(BaseModel):
    request_id: UUID
    errors: list[ErrorItem]
