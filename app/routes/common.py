from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, Query

from app.auth import Identity
from app.db import DB
from app.service import PriorAuthorizationService


def get_service(db: DB, actor: Identity) -> PriorAuthorizationService:
    return PriorAuthorizationService(db, actor)


Service = Annotated[PriorAuthorizationService, Depends(get_service)]


def require_role(role: str):
    def check(actor: Identity) -> None:
        actor.require_role(role)

    return Depends(check)


WriteKey = Annotated[
    UUID, Header(alias="Idempotency-Key", description="UUID; reuse for retries only")
]
PageSize = Annotated[int, Query(ge=1, le=100)]
PageOffset = Annotated[int, Query(ge=0, le=10000)]
