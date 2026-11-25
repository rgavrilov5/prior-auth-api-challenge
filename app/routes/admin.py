from uuid import UUID

from fastapi import APIRouter

from app.routes.common import PageOffset, PageSize, Service, WriteKey, require_role
from app.schemas import AccessEvent, AuditEvent, InternalRequest, Reassignment

router = APIRouter(
    prefix="/admin", tags=["Operations admins"], dependencies=[require_role("admin")]
)


@router.get("/requests", response_model=list[InternalRequest])
def list_requests(service: Service, limit: PageSize = 50, offset: PageOffset = 0):
    return service.list_requests(limit, offset)


@router.get("/requests/{request_id}", response_model=InternalRequest)
def get_request(request_id: UUID, service: Service):
    return service.get(request_id)


@router.post("/requests/{request_id}/reassign", response_model=InternalRequest)
def reassign(request_id: UUID, body: Reassignment, key: WriteKey, service: Service):
    return service.reassign(key, request_id, body.reviewer_id)


@router.get("/requests/{request_id}/audit", response_model=list[AuditEvent])
def audit(request_id: UUID, service: Service):
    return service.audit(request_id)


@router.get("/support/{trace_id}", response_model=AccessEvent)
def support(trace_id: UUID, service: Service):
    """Look up successful or failed calls using the returned X-Request-ID."""
    return service.support(trace_id)
