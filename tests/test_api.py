import json
import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app import service
from tests.conftest import decision, submission, token


def test_lifecycle_mixed_decisions_and_member_privacy(api):
    request = api.submit()
    request_id = request["id"]
    assert request["status"] == "queued"
    queue = api.call("GET", "/reviewer/queue", user="reviewer-1").json()
    assert queue == [{"id": request_id}]
    api.claim(request)
    body = decision(request)
    body["procedures"][1].update(decision="denied", reason="SYNTHETIC INTERNAL REASON")
    result = api.call(
        "POST", f"/reviewer/requests/{request_id}/decision", user="reviewer-1", json=body
    )
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "decided"
    assert {p["decision"] for p in result.json()["procedures"]} == {"approved", "denied"}
    for procedure in result.json()["procedures"]:
        assert procedure["decided_by"] == "reviewer-1"
        assert procedure["decided_at"]

    for path in (f"/member/requests/{request_id}", "/member/requests"):
        member = api.call("GET", path, user="member-1")
        assert member.status_code == 200
        assert "SYNTHETIC" not in member.text
        assert "clinical_note" not in member.text
        assert "reason" not in member.text
        assert "reviewer" not in member.text
        assert "decided_by" not in member.text
        assert "denied" in member.text
    office = api.call("GET", f"/office/requests/{request_id}")
    assert office.status_code == 200
    assert "SYNTHETIC PRIVATE CLINICAL NOTE" in office.text
    assert "SYNTHETIC INTERNAL REASON" not in office.text
    assert "reason" not in office.json()["procedures"][1]
    assert "decided_by" not in office.json()["procedures"][1]
    audit = api.call("GET", f"/admin/requests/{request_id}/audit", user="admin-1").json()
    assert [event["action"] for event in audit] == ["submitted", "claimed", "decided"]
    assert [event["actor_id"] for event in audit] == ["office-maine", "reviewer-1", "reviewer-1"]
    assert all(event["occurred_at"] and event["trace_id"] for event in audit)


def test_role_and_resource_authorization(api):
    request = api.submit()
    request_id = request["id"]
    for user in ("office-hawaii", "office-colleague", "partner-maine"):
        assert api.call("GET", f"/office/requests/{request_id}", user=user).status_code == 404
        assert api.call("GET", "/office/requests", user=user).json() == []
    assert api.call("GET", f"/member/requests/{request_id}", user="member-2").status_code == 404
    assert api.call("GET", "/member/requests", user="member-2").json() == []
    assert api.call("GET", "/admin/requests", user="member-1").status_code == 403
    assert (
        api.call("GET", f"/admin/requests/{request_id}/audit", user="member-1").status_code == 403
    )
    assert (
        api.call("POST", "/office/requests", user="member-1", json=submission()).status_code == 403
    )
    assert api.call("GET", f"/reviewer/requests/{request_id}", user="reviewer-1").status_code == 404
    api.claim(request)
    assert api.call("GET", f"/reviewer/requests/{request_id}", user="reviewer-2").status_code == 404
    assert (
        api.call(
            "POST",
            f"/reviewer/requests/{request_id}/decision",
            user="reviewer-2",
            json=decision(request),
        ).status_code
        == 404
    )
    assert api.call("GET", "/admin/requests", user="admin-1").json()[0]["id"] == request_id


def test_unlinked_patient_and_partner_identity(api):
    response = api.call("POST", "/office/requests", json=submission(member_id="member-3"))
    assert response.status_code == 403
    assert response.json()["errors"][0]["code"] == "patient_not_linked"
    partner = api.submit(user="partner-maine")
    assert partner["office_id"] == "partner-maine"


@pytest.mark.parametrize(
    "credential",
    [
        None,
        "not.a.token",
        token(secret="wrong-test-secret-thirty-two-characters"),
        token(exp=datetime.now(UTC) - timedelta(minutes=1)),
        token("missing-user", role="office"),
        token(role="admin"),
        token(role="auditor"),
        token(aud="another-api"),
        token(iss="another-issuer"),
    ],
)
def test_invalid_tokens(api, credential):
    headers = {} if credential is None else {"Authorization": f"Bearer {credential}"}
    result = api.client.get("/office/requests", headers=headers)
    assert result.status_code == 401
    assert result.headers["www-authenticate"] == "Bearer"
    assert result.json()["errors"][0]["code"] == "invalid_token"


@pytest.mark.parametrize(
    ("procedures", "field"),
    [
        ([], "body.procedures"),
        ([{"code": "99213", "units": 31}], "body.procedures.0.units"),
        ([{"code": "99213", "units": 0}], "body.procedures.0.units"),
        ([{"code": "99213", "units": True}], "body.procedures.0.units"),
        ([{"code": "bad", "units": 1}], "body.procedures.0.code"),
        ([{"code": "99213", "units": 1}, {"code": "99213", "units": 2}], "body"),
    ],
)
def test_submission_errors_identify_field_without_echoing_phi(api, procedures, field):
    result = api.call("POST", "/office/requests", json=submission(procedures=procedures))
    assert result.status_code == 422
    assert field in [error["field"] for error in result.json()["errors"]]
    assert "SYNTHETIC PRIVATE" not in result.text
    assert "input" not in result.json()["errors"][0]
    with api.connect() as db:
        assert db.execute("SELECT count(*) AS n FROM requests").fetchone()["n"] == 0


def test_failed_validation_is_traceable_without_phi(api, prior_auth_logs):
    result = api.call(
        "POST",
        "/office/requests",
        json=submission(procedures=[{"code": "SENSITIVE-INVALID-CODE", "units": 31}]),
    )
    assert result.status_code == 422
    trace = result.headers["x-request-id"]
    assert result.json()["request_id"] == trace
    support = api.call("GET", f"/admin/support/{trace}", user="admin-1")
    assert support.status_code == 200
    assert support.json()["actor_id"] == "office-maine"
    assert support.json()["status"] == 422
    assert support.json()["route"] == "/office/requests"
    assert support.json()["error_codes"]
    logged_calls = [
        json.loads(record.getMessage())
        for record in prior_auth_logs.records
        if record.name == "prior_auth" and record.levelno == logging.INFO
    ]
    assert any(event["request_id"] == trace and event["status"] == 422 for event in logged_calls)
    assert "SENSITIVE" not in support.text + prior_auth_logs.text
    assert "SYNTHETIC" not in support.text + prior_auth_logs.text
    assert api.call("GET", f"/admin/support/{trace}", user="office-maine").status_code == 403
    assert result.headers["cache-control"] == "no-store"


def test_malformed_json_and_missing_write_key(api):
    malformed = api.call(
        "POST",
        "/office/requests",
        content='{"clinical_note": "SYNTHETIC SECRET",',
        headers={"Content-Type": "application/json"},
    )
    assert malformed.status_code == 422
    assert "SYNTHETIC" not in malformed.text
    missing_key = api.client.post(
        "/office/requests", json=submission(), headers={"Authorization": f"Bearer {token()}"}
    )
    assert missing_key.status_code == 422
    assert "header.Idempotency-Key" in [e["field"] for e in missing_key.json()["errors"]]


def test_support_records_unauthenticated_calls_without_raw_urls(api):
    response = api.client.get("/office/requests?clinical_note=SYNTHETIC_SECRET")
    assert response.status_code == 401
    trace = response.headers["x-request-id"]
    support = api.call("GET", f"/admin/support/{trace}", user="admin-1")
    assert support.status_code == 200
    assert support.json()["actor_id"] is None
    assert support.json()["status"] == 401
    assert support.json()["route"] == "/office/requests"
    assert "SYNTHETIC_SECRET" not in support.text


def test_nationwide_service_dates_use_practice_timezone(api, monkeypatch):
    monkeypatch.setattr(service, "now", lambda: datetime(2026, 11, 25, 7, tzinfo=UTC))
    # At the same instant Maine has entered November 25; Hawaii is still November 24.
    body = submission(service_date="2026-11-24")
    maine = api.call("POST", "/office/requests", json=body)
    hawaii = api.call("POST", "/office/requests", user="office-hawaii", json=body)
    assert maine.status_code == 422
    assert "America/New_York" in maine.text
    assert hawaii.status_code == 201, hawaii.text
    assert (
        api.call("POST", "/office/requests", json=submission(service_date="2027-11-25")).status_code
        == 201
    )
    assert (
        api.call("POST", "/office/requests", json=submission(service_date="2027-11-26")).status_code
        == 422
    )


def test_calendar_year_limit_on_leap_day(api, monkeypatch):
    monkeypatch.setattr(service, "now", lambda: datetime(2028, 2, 29, 17, tzinfo=UTC))
    assert (
        api.call("POST", "/office/requests", json=submission(service_date="2028-02-29")).status_code
        == 201
    )
    assert (
        api.call("POST", "/office/requests", json=submission(service_date="2029-02-28")).status_code
        == 201
    )
    response = api.call("POST", "/office/requests", json=submission(service_date="2029-03-01"))
    assert response.status_code == 422
    assert response.json()["errors"][0]["code"] == "date_out_of_range"


def test_coverage_boundaries_and_combined_business_errors(api):
    with api.connect() as db:
        db.execute("DELETE FROM coverages WHERE member_id = 'member-1'")
        db.execute("""INSERT INTO coverages (member_id, starts_on, ends_on)
                   VALUES ('member-1', '2026-11-26', '2026-11-27')""")
    for day in ("2026-11-26", "2026-11-27"):
        assert (
            api.call("POST", "/office/requests", json=submission(service_date=day)).status_code
            == 201
        )
    for day in ("2026-11-25", "2026-11-28"):
        response = api.call("POST", "/office/requests", json=submission(service_date=day))
        assert response.status_code == 422
        assert response.json()["errors"][0]["code"] == "no_coverage"
    response = api.call(
        "POST",
        "/office/requests",
        json=submission(member_id="member-uncovered", service_date="2026-11-24"),
    )
    assert {e["code"] for e in response.json()["errors"]} == {"date_out_of_range", "no_coverage"}


def test_conflict_blocks_claim_and_admin_reassignment(api):
    response = api.call("POST", "/office/requests", json=submission(member_id="member-2"))
    assert response.status_code == 201, response.text
    request = response.json()
    assert api.call("GET", "/reviewer/queue", user="reviewer-1").json() == []
    conflict = api.call("POST", f"/reviewer/requests/{request['id']}/claim", user="reviewer-1")
    assert conflict.status_code == 403
    assert conflict.json()["errors"][0]["code"] == "treating_patient"
    api.claim(request, user="reviewer-2")
    conflict = api.call(
        "POST",
        f"/admin/requests/{request['id']}/reassign",
        user="admin-1",
        json={"reviewer_id": "reviewer-1"},
    )
    assert conflict.status_code == 403
    assert (
        api.call("GET", f"/reviewer/requests/{request['id']}", user="reviewer-2").status_code == 200
    )


def test_reassignment_revokes_previous_reviewer_and_decisions_are_final(api):
    request = api.claim(api.submit())
    result = api.call(
        "POST",
        f"/admin/requests/{request['id']}/reassign",
        user="admin-1",
        json={"reviewer_id": "reviewer-2"},
    )
    assert result.status_code == 200
    assert result.json()["reviewer_id"] == "reviewer-2"
    assert (
        api.call("GET", f"/reviewer/requests/{request['id']}", user="reviewer-1").status_code == 404
    )
    path = f"/reviewer/requests/{request['id']}/decision"
    assert api.call("POST", path, user="reviewer-1", json=decision(request)).status_code == 404
    assert api.call("POST", path, user="reviewer-2", json=decision(request)).status_code == 200
    assert (
        api.call("POST", path, user="reviewer-2", json=decision(request, denied=True)).status_code
        == 409
    )
    assert (
        api.call(
            "POST",
            f"/admin/requests/{request['id']}/reassign",
            user="admin-1",
            json={"reviewer_id": "reviewer-1"},
        ).status_code
        == 409
    )


@pytest.mark.parametrize("kind", ["missing", "duplicate", "unknown", "blank_denial"])
def test_invalid_decision_is_all_or_nothing(api, kind):
    request = api.claim(api.submit())
    body = decision(request)
    if kind == "missing":
        body["procedures"].pop()
    elif kind == "duplicate":
        body["procedures"][1] = body["procedures"][0]
    elif kind == "unknown":
        body["procedures"][1]["procedure_id"] = str(uuid4())
    else:
        body["procedures"][1].update(decision="denied", reason="   ")
    response = api.call(
        "POST", f"/reviewer/requests/{request['id']}/decision", user="reviewer-1", json=body
    )
    assert response.status_code == 422
    stored = api.call("GET", f"/reviewer/requests/{request['id']}", user="reviewer-1").json()
    assert stored["status"] == "in_review"
    assert all(p["decision"] == "pending" for p in stored["procedures"])
    audit = api.call("GET", f"/admin/requests/{request['id']}/audit", user="admin-1").json()
    assert [event["action"] for event in audit] == ["submitted", "claimed"]


def test_pagination_bounds_and_openapi(api):
    first = api.submit()
    second = api.submit()
    assert api.call("GET", "/office/requests?limit=1&offset=1").json()[0]["id"] == second["id"]
    assert api.call("GET", "/office/requests?limit=1").json()[0]["id"] == first["id"]
    assert api.call("GET", "/office/requests?limit=101").status_code == 422
    schema = api.client.get("/openapi.json").json()
    member_schema = schema["components"]["schemas"]["MemberRequest"]["properties"]
    assert "clinical_note" not in member_schema
    assert "InternalRequest" in schema["components"]["schemas"]
    assert schema["paths"]["/office/requests"]["post"]["security"]
