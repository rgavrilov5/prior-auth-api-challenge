from uuid import UUID

from fastapi import APIRouter

from app.routes.common import PageOffset, PageSize, Service, require_role
from app.schemas import MemberRequest

router = APIRouter(prefix="/member", tags=["Members"], dependencies=[require_role("member")])


@router.get("/requests", response_model=list[MemberRequest])
def list_requests(service: Service, limit: PageSize = 50, offset: PageOffset = 0):
    return service.list_requests(limit, offset)


@router.get("/requests/{request_id}", response_model=MemberRequest)
def get_request(request_id: UUID, service: Service):
    """Member contract intentionally has no note, reason, reviewer, or audit fields."""
    return service.get(request_id)
