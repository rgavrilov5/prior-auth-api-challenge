from uuid import UUID

from fastapi import APIRouter

from app import service
from app.auth import Identity, allow
from app.db import DB
from app.routes.common import PageOffset, PageSize
from app.schemas import MemberRequest

router = APIRouter(prefix="/member", tags=["Members"])


@router.get("/requests", response_model=list[MemberRequest])
def list_requests(actor: Identity, db: DB, limit: PageSize = 50, offset: PageOffset = 0):
    allow(actor, "member")
    return service.list_requests(db, actor, limit, offset)


@router.get("/requests/{request_id}", response_model=MemberRequest)
def get_request(request_id: UUID, actor: Identity, db: DB):
    """Member contract intentionally has no note, reason, reviewer, or audit fields."""
    allow(actor, "member")
    return service.fetch(db, actor, request_id)
