from uuid import UUID

from fastapi import APIRouter

from app import service
from app.auth import Identity, allow
from app.db import DB
from app.routes.common import PageOffset, PageSize, WriteKey
from app.schemas import OfficeRequest, Submission

router = APIRouter(prefix="/office", tags=["Provider offices"])


@router.post("/requests", response_model=OfficeRequest, status_code=201)
def submit(body: Submission, key: WriteKey, actor: Identity, db: DB):
    """Submit for a linked, covered patient. Retries return the same resource."""
    allow(actor, "office")
    return service.submit(db, actor, key, body)


@router.get("/requests", response_model=list[OfficeRequest])
def list_requests(actor: Identity, db: DB, limit: PageSize = 50, offset: PageOffset = 0):
    allow(actor, "office")
    return service.list_requests(db, actor, limit, offset)


@router.get("/requests/{request_id}", response_model=OfficeRequest)
def get_request(request_id: UUID, actor: Identity, db: DB):
    allow(actor, "office")
    return service.fetch(db, actor, request_id)
