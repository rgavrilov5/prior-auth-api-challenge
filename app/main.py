import json
import logging
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb
from starlette.concurrency import run_in_threadpool

from app.auth import signing_key
from app.db import make_pool
from app.routes import admin, member, office, reviewer
from app.schemas import ErrorResponse

logger = logging.getLogger("prior_auth")
logger.setLevel(logging.INFO)
if not logger.handlers:
    logger.addHandler(logging.StreamHandler())
logger.propagate = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    signing_key()
    pool = make_pool()
    pool.open(wait=True)
    app.state.pool = pool
    yield
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
    responses={status: {"model": ErrorResponse} for status in (401, 403, 404, 409, 422, 503)},
)
for router in (office.router, member.router, reviewer.router, admin.router):
    app.include_router(router)


def error_response(request: Request, status: int, errors: list, headers=None):
    request.state.error_codes = [item["code"] for item in errors]
    return JSONResponse(
        status_code=status,
        content={"request_id": request.state.trace_id, "errors": errors},
        headers=headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    # Pydantic's default response includes raw input, which can contain clinical data.
    errors = [
        {
            "field": ".".join(str(part) for part in error["loc"]),
            "code": error["type"],
            "message": error["msg"],
        }
        for error in exc.errors()
    ]
    return error_response(request, 422, errors)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    errors = (
        exc.detail
        if isinstance(exc.detail, list)
        else [{"field": "request", "code": "http_error", "message": str(exc.detail)}]
    )
    return error_response(request, exc.status_code, errors, exc.headers)


def record_access(app, event):
    with app.state.pool.connection() as db:
        db.execute(
            """INSERT INTO access_events (trace_id, actor_id, method, route, status, error_codes)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (
                event["request_id"],
                event["actor_id"],
                event["method"],
                event["route"],
                event["status"],
                Jsonb(event["error_codes"]),
            ),
        )


@app.middleware("http")
async def trace_and_record(request: Request, call_next):
    request.state.trace_id = str(uuid4())
    request.state.error_codes = []
    try:
        response = await call_next(request)
    except Exception as exc:
        # Never log exception text: database/validation errors may include PHI.
        logger.error(
            "request_failed type=%s request_id=%s", type(exc).__name__, request.state.trace_id
        )
        response = error_response(
            request,
            503,
            [
                {
                    "field": "request",
                    "code": "unavailable",
                    "message": "Retry with the same write key",
                }
            ],
        )
    response.headers["X-Request-ID"] = request.state.trace_id
    response.headers["Cache-Control"] = "no-store"
    route = request.scope.get("route")
    event = {
        "request_id": request.state.trace_id,
        "actor_id": getattr(request.state, "actor_id", None),
        "method": request.method,
        "route": route.path if route else "unmatched",
        "status": response.status_code,
        "error_codes": request.state.error_codes,
    }
    if request.url.path not in {"/health", "/docs", "/openapi.json", "/redoc"}:
        try:
            await run_in_threadpool(record_access, request.app, event)
        except Exception:
            # A DB outage must still leave a PHI-free support trail in centralized logs.
            logger.error("support_store_unavailable request_id=%s", request.state.trace_id)
        logger.info(json.dumps(event))
    return response


@app.get("/health", tags=["Health"])
def health(request: Request):
    with request.app.state.pool.connection() as db:
        db.execute("SELECT 1")
    return {"status": "ok"}
