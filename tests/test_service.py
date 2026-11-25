from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg
import pytest

from app.domain import Actor, RequestError
from app.schemas import DecisionInput, Submission
from app.service import PriorAuthorizationService
from tests.conftest import decision, submission


@pytest.mark.parametrize(
    ("subject", "role", "method", "arguments"),
    [
        ("member-1", "member", "submit", (uuid4(), Submission(**submission()))),
        ("office-maine", "office", "claim", (uuid4(), uuid4())),
        (
            "admin-1",
            "admin",
            "decide",
            (
                uuid4(),
                uuid4(),
                DecisionInput(procedures=[{"procedure_id": uuid4(), "decision": "approved"}]),
            ),
        ),
        ("reviewer-1", "reviewer", "reassign", (uuid4(), uuid4(), "reviewer-2")),
        ("office-maine", "office", "queue", ()),
        ("member-1", "member", "audit", (uuid4(),)),
        ("reviewer-1", "reviewer", "support", (uuid4(),)),
    ],
)
def test_internal_calls_enforce_roles_before_touching_data(api, subject, role, method, arguments):
    with api.connect() as db:
        service = PriorAuthorizationService(db, Actor(subject, role, None))
        with pytest.raises(RequestError) as failure:
            getattr(service, method)(*arguments)
        assert failure.value.kind == "forbidden"
        assert failure.value.errors[0]["code"] == "forbidden"
        assert db.execute("SELECT count(*) AS n FROM write_receipts").fetchone()["n"] == 0


def test_internal_decision_requires_assignee_and_uses_injected_clock(api):
    request = api.claim(api.submit())
    request_id = UUID(request["id"])
    body = DecisionInput(**decision(request))
    decided_at = datetime(2026, 11, 27, 12, tzinfo=UTC)
    with api.connect() as db:
        other_reviewer = PriorAuthorizationService(db, Actor("reviewer-2", "reviewer", None))
        with pytest.raises(RequestError) as failure:
            other_reviewer.decide(uuid4(), request_id, body)
        assert failure.value.kind == "not_found"
        db.execute("SELECT set_config('app.actor_id', 'reviewer-1', true)")
        db.execute("SELECT set_config('app.trace_id', %s, true)", (str(uuid4()),))
        service = PriorAuthorizationService(
            db, Actor("reviewer-1", "reviewer", None), clock=lambda: decided_at
        )
        decided = service.decide(uuid4(), request_id, body)
        assert decided["decided_at"] == decided_at
        assert all(item["decided_by"] == "reviewer-1" for item in decided["procedures"])
    audit = api.call("GET", f"/admin/requests/{request_id}/audit", user="admin-1").json()
    assert [event["action"] for event in audit] == ["submitted", "claimed", "decided"]


@pytest.mark.parametrize("error_type", [ValueError, psycopg.errors.UndefinedColumn])
def test_programming_errors_are_not_reported_as_retryable_database_failures(
    api, monkeypatch, error_type
):
    from app import http

    logged = []
    monkeypatch.setattr(http.logger, "error", lambda message, *args: logged.append(message))

    def broken_queue(self, *args):
        raise error_type("SYNTHETIC PRIVATE DATA MUST NOT BE LOGGED")

    monkeypatch.setattr(PriorAuthorizationService, "queue", broken_queue)
    response = api.call("GET", "/reviewer/queue", user="reviewer-1")
    assert response.status_code == 500
    assert response.json()["errors"][0]["code"] == "internal_error"
    assert "SYNTHETIC PRIVATE" not in response.text + " ".join(logged)
    assert any("broken_queue" in message and error_type.__name__ in message for message in logged)
    trace = response.headers["x-request-id"]
    support = api.call("GET", f"/admin/support/{trace}", user="admin-1").json()
    assert support["status"] == 500
    assert support["error_codes"] == ["internal_error"]


def test_unmatched_routes_use_the_documented_error_contract(api):
    response = api.client.get("/not-an-endpoint")
    assert response.status_code == 404
    assert response.json()["request_id"] == response.headers["x-request-id"]
    assert response.json()["errors"][0]["code"] == "http_error"
