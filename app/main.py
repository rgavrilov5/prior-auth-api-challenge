from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from app.auth import signing_key
from app.db import make_pool
from app.http import configure_http
from app.routes import admin, member, office, reviewer
from app.schemas import ErrorResponse


@asynccontextmanager
async def lifespan(app: FastAPI):
    signing_key()
    pool = make_pool()
    pool.open(wait=True)
    app.state.pool = pool
    try:
        yield
    finally:
        pool.close()


app = FastAPI(
    title="Prior Authorization API",
    version="0.1.0",
    description=(
        "Obtain a token from the identity service, then use Bearer authorization. "
        "Every write requires a UUID Idempotency-Key. Replays return the same resource's "
        "current state. See README for synthetic identities and an end-to-end example."
    ),
    lifespan=lifespan,
    responses={status: {"model": ErrorResponse} for status in (401, 403, 404, 409, 422, 500, 503)},
)
for router in (office.router, member.router, reviewer.router, admin.router):
    app.include_router(router)


configure_http(app)


@app.get("/health", tags=["Health"])
def health(request: Request):
    with request.app.state.pool.connection() as db:
        db.execute("SELECT 1")
    return {"status": "ok"}
