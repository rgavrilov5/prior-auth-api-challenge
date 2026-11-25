from uuid import UUID

from fastapi import APIRouter

from app.routes.common import PageOffset, PageSize, Service, WriteKey, require_role
from app.schemas import OfficeRequest, Submission

router = APIRouter(
    prefix="/office", tags=["Provider offices"], dependencies=[require_role("office")]
)


@router.post("/requests", response_model=OfficeRequest, status_code=201)
def submit(body: Submission, key: WriteKey, service: Service):
    """Submit for a linked, covered patient. Retries return the same resource."""
    return service.submit(key, body)


@router.get("/requests", response_model=list[OfficeRequest])
def list_requests(service: Service, limit: PageSize = 50, offset: PageOffset = 0):
    return service.list_requests(limit, offset)


@router.get("/requests/{request_id}", response_model=OfficeRequest)
def get_request(request_id: UUID, service: Service):
    return service.get(request_id)
