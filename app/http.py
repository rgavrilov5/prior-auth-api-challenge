"""HTTP errors and PHI-free call tracing; workflow rules live in the service."""

import json
import logging
import traceback
from pathlib import Path
from uuid import uuid4

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb
from psycopg_pool import PoolTimeout
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from app.domain import RequestError

logger = logging.getLogger("prior_auth")
logger.setLevel(logging.INFO)
if not logger.handlers:
    logger.addHandler(logging.StreamHandler())
logger.propagate = False

ERROR_STATUS = {"forbidden": 403, "not_found": 404, "conflict": 409, "invalid": 422}


def configure_http(app: FastAPI) -> None:
    app.add_exception_handler(RequestError, workflow_error)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(HTTPException, http_error)
    app.middleware("http")(trace_and_record)


def error_response(request: Request, status: int, errors: list, headers=None) -> JSONResponse:
    request.state.error_codes = [item["code"] for item in errors]
    return JSONResponse(
        status_code=status,
        content={"request_id": request.state.trace_id, "errors": errors},
        headers=headers,
    )


async def workflow_error(request: Request, exc: RequestError) -> JSONResponse:
    return error_response(request, ERROR_STATUS[exc.kind], exc.errors)


async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Pydantic's default includes raw input, which can contain clinical data.
    errors = [
        {
            "field": ".".join(str(part) for part in error["loc"]),
            "code": error["type"],
            "message": error["msg"],
        }
        for error in exc.errors()
    ]
    return error_response(request, 422, errors)


async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
    errors = (
        exc.detail
        if isinstance(exc.detail, list)
        else [{"field": "request", "code": "http_error", "message": str(exc.detail)}]
    )
    return error_response(request, exc.status_code, errors, exc.headers)


def record_access(app: FastAPI, event: dict) -> None:
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


def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    # Frame locations are useful for diagnosis; messages and locals can contain PHI.
    locations = [
        f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
        for frame in traceback.extract_tb(exc.__traceback__)[-8:]
    ]
    logger.error(
        json.dumps(
            {
                "event": "request_failed",
                "request_id": request.state.trace_id,
                "type": type(exc).__name__,
                "locations": locations,
            }
        )
    )
    if isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError, PoolTimeout)):
        status, code = 503, "unavailable"
        message = "Retry with the same write key"
    else:
        status, code = 500, "internal_error"
        message = "Unexpected server error; contact support with this request ID"
    return error_response(request, status, [{"field": "request", "code": code, "message": message}])


async def trace_and_record(request: Request, call_next):
    request.state.trace_id = str(uuid4())
    request.state.error_codes = []
    try:
        response = await call_next(request)
    except Exception as exc:
        response = unexpected_error(request, exc)
    response.headers["X-Request-ID"] = request.state.trace_id
    response.headers["Cache-Control"] = "no-store"
    # Routine probes and documentation fetches should not bury real support calls.
    if request.url.path in {"/health", "/docs", "/openapi.json", "/redoc"}:
        return response
    route = request.scope.get("route")
    event = {
        "request_id": request.state.trace_id,
        "actor_id": getattr(request.state, "actor_id", None),
        "method": request.method,
        "route": route.path if route else "unmatched",
        "status": response.status_code,
        "error_codes": request.state.error_codes,
    }
    try:
        await run_in_threadpool(record_access, request.app, event)
    except Exception:
        # Preserve a support trail even when the database cannot accept the access record.
        logger.error("support_store_unavailable request_id=%s", request.state.trace_id)
    logger.info(json.dumps(event))
    return response
