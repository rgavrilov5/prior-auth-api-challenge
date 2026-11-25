import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from app import service
from app.auth import AUDIENCE, ISSUER
from app.bootstrap import migrate, seed
from app.main import app

SECRET = "test-only-secret-at-least-thirty-two-characters"
ROLES = {
    "admin-1": "admin",
    "reviewer-1": "reviewer",
    "reviewer-2": "reviewer",
    "office-maine": "office",
    "office-hawaii": "office",
    "office-colleague": "office",
    "partner-maine": "office",
}


def token(user="office-maine", *, secret=SECRET, **overrides):
    current = datetime.now(UTC)
    claims = {
        "sub": user,
        "role": ROLES.get(user, "member"),
        "iat": current,
        "exp": current + timedelta(minutes=30),
        "iss": ISSUER,
        "aud": AUDIENCE,
        **overrides,
    }
    return jwt.encode(claims, secret, algorithm="HS256")


def submission(**overrides):
    return {
        "member_id": "member-1",
        "diagnosis": "M54.5",
        "service_date": "2026-11-26",
        "procedures": [{"code": "99213", "units": 1}, {"code": "J0120", "units": 2}],
        "clinical_note": "SYNTHETIC PRIVATE CLINICAL NOTE",
        **overrides,
    }


def decision(request, *, denied=False):
    return {
        "procedures": [
            {
                "procedure_id": item["id"],
                "decision": "denied" if denied else "approved",
                **({"reason": "SYNTHETIC INTERNAL REASON"} if denied else {}),
            }
            for item in request["procedures"]
        ]
    }


@pytest.fixture
def prior_auth_logs(caplog):
    from app.http import logger

    # This logger intentionally does not propagate to the root logger pytest normally captures.
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.DEBUG, logger="prior_auth"):
            yield caplog
    finally:
        logger.removeHandler(caplog.handler)


@dataclass
class API:
    client: TestClient
    database_url: str

    def connect(self):
        return psycopg.connect(self.database_url, row_factory=dict_row)

    def call(self, method, path, *, user="office-maine", key=None, **kwargs):
        headers = {"Authorization": f"Bearer {token(user)}"}
        if method.lower() in {"post", "put", "patch", "delete"}:
            headers["Idempotency-Key"] = str(key or uuid4())
        headers.update(kwargs.pop("headers", {}))
        return self.client.request(method, path, headers=headers, **kwargs)

    def submit(self, **kwargs):
        result = self.call("POST", "/office/requests", json=submission(), **kwargs)
        assert result.status_code == 201, result.text
        return result.json()

    def claim(self, request, *, user="reviewer-1", **kwargs):
        result = self.call("POST", f"/reviewer/requests/{request['id']}/claim", user=user, **kwargs)
        assert result.status_code == 200, result.text
        return result.json()


@pytest.fixture
def api(monkeypatch):
    base_url = os.environ.get("TEST_DATABASE_URL")
    if not base_url:
        pytest.fail("Set TEST_DATABASE_URL to a disposable PostgreSQL database")
    schema = f"test_{uuid4().hex}"
    with psycopg.connect(base_url, autocommit=True) as db:
        db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    database_url = make_conninfo(base_url, options=f"-csearch_path={schema}")
    try:
        with psycopg.connect(database_url) as db:
            migrate(db)
            seed(db)
        monkeypatch.setenv("DATABASE_URL", database_url)
        monkeypatch.setenv("JWT_SECRET", SECRET)
        monkeypatch.setattr(service, "now", lambda: datetime(2026, 11, 25, 12, tzinfo=UTC))
        with TestClient(app) as client:
            yield API(client, database_url)
    finally:
        with psycopg.connect(base_url, autocommit=True) as db:
            db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
