from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel

from app.routes.common import PageOffset, PageSize, Service, WriteKey, require_role
from app.schemas import DecisionInput, InternalRequest

router = APIRouter(prefix="/reviewer", tags=["Reviewers"], dependencies=[require_role("reviewer")])


class QueueItem(BaseModel):
    id: UUID


@router.get("/queue", response_model=list[QueueItem])
def queue(service: Service, limit: PageSize = 50, offset: PageOffset = 0):
    return service.queue(limit, offset)


@router.get("/requests", response_model=list[InternalRequest])
def list_requests(service: Service, limit: PageSize = 50, offset: PageOffset = 0):
    return service.list_requests(limit, offset)


@router.get("/requests/{request_id}", response_model=InternalRequest)
def get_request(request_id: UUID, service: Service):
    return service.get(request_id)


@router.post("/requests/{request_id}/claim", response_model=InternalRequest)
def claim(request_id: UUID, key: WriteKey, service: Service):
    """Claim one queued request. Concurrent claims have one winner."""
    return service.claim(key, request_id)


@router.post("/requests/{request_id}/decision", response_model=InternalRequest)
def decide(request_id: UUID, body: DecisionInput, key: WriteKey, service: Service):
    """Commit every procedure outcome at once. Decisions are final."""
    return service.decide(key, request_id, body)
