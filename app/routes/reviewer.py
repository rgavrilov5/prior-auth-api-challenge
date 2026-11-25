from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel

from app import service
from app.auth import Identity, allow
from app.db import DB
from app.routes.common import PageOffset, PageSize, WriteKey
from app.schemas import DecisionInput, InternalRequest

router = APIRouter(prefix="/reviewer", tags=["Reviewers"])


class QueueItem(BaseModel):
    id: UUID


@router.get("/queue", response_model=list[QueueItem])
def queue(actor: Identity, db: DB, limit: PageSize = 50, offset: PageOffset = 0):
    allow(actor, "reviewer")
    return service.queue(db, actor, limit, offset)


@router.get("/requests", response_model=list[InternalRequest])
def list_requests(actor: Identity, db: DB, limit: PageSize = 50, offset: PageOffset = 0):
    allow(actor, "reviewer")
    return service.list_requests(db, actor, limit, offset)


@router.get("/requests/{request_id}", response_model=InternalRequest)
def get_request(request_id: UUID, actor: Identity, db: DB):
    allow(actor, "reviewer")
    return service.fetch(db, actor, request_id)


@router.post("/requests/{request_id}/claim", response_model=InternalRequest)
def claim(request_id: UUID, key: WriteKey, actor: Identity, db: DB):
    """Claim one queued request. Concurrent claims have one winner."""
    allow(actor, "reviewer")
    return service.claim(db, actor, key, request_id)


@router.post("/requests/{request_id}/decision", response_model=InternalRequest)
def decide(request_id: UUID, body: DecisionInput, key: WriteKey, actor: Identity, db: DB):
    """Commit every procedure outcome at once. Decisions are final."""
    allow(actor, "reviewer")
    return service.decide(db, actor, key, request_id, body)
