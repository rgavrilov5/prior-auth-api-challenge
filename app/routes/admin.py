from uuid import UUID

from fastapi import APIRouter

from app import service
from app.auth import Identity, allow
from app.db import DB
from app.errors import reject
from app.routes.common import PageOffset, PageSize, WriteKey
from app.schemas import AccessEvent, AuditEvent, InternalRequest, Reassignment

router = APIRouter(prefix="/admin", tags=["Operations admins"])


@router.get("/requests", response_model=list[InternalRequest])
def list_requests(actor: Identity, db: DB, limit: PageSize = 50, offset: PageOffset = 0):
    allow(actor, "admin")
    return service.list_requests(db, actor, limit, offset)


@router.get("/requests/{request_id}", response_model=InternalRequest)
def get_request(request_id: UUID, actor: Identity, db: DB):
    allow(actor, "admin")
    return service.fetch(db, actor, request_id)


@router.post("/requests/{request_id}/reassign", response_model=InternalRequest)
def reassign(request_id: UUID, body: Reassignment, key: WriteKey, actor: Identity, db: DB):
    allow(actor, "admin")
    return service.reassign(db, actor, key, request_id, body.reviewer_id)


@router.get("/requests/{request_id}/audit", response_model=list[AuditEvent])
def audit(request_id: UUID, actor: Identity, db: DB):
    allow(actor, "admin")
    service.fetch(db, actor, request_id)
    return db.execute(
        "SELECT * FROM audit_events WHERE request_id = %s ORDER BY id", (request_id,)
    ).fetchall()


@router.get("/support/{trace_id}", response_model=AccessEvent)
def support(trace_id: UUID, actor: Identity, db: DB):
    """Look up successful or failed calls using the returned X-Request-ID."""
    allow(actor, "admin")
    row = db.execute("SELECT * FROM access_events WHERE trace_id = %s", (trace_id,)).fetchone()
    if not row:
        reject(404, "trace_id", "not_found", "Call not found")
    return row
