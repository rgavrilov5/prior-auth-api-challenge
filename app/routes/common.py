from typing import Annotated
from uuid import UUID

from fastapi import Header, Query

WriteKey = Annotated[
    UUID, Header(alias="Idempotency-Key", description="UUID; reuse for retries only")
]
PageSize = Annotated[int, Query(ge=1, le=100)]
PageOffset = Annotated[int, Query(ge=0, le=10000)]
