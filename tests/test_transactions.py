from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest

from tests.conftest import decision, submission


def test_idempotency_for_every_workflow_write(api):
    submit_key, claim_key, reassign_key, decide_key = [uuid4() for _ in range(4)]
    first = api.call("POST", "/office/requests", key=submit_key, json=submission())
    request = first.json()
    repeat = api.call("POST", "/office/requests", key=submit_key, json=submission())
    assert first.status_code == repeat.status_code == 201
    assert repeat.json() == request
    changed = api.call(
        "POST", "/office/requests", key=submit_key, json=submission(diagnosis="M54.9")
    )
    assert changed.status_code == 409
    assert changed.json()["errors"][0]["code"] == "key_reused"

    claim_path = f"/reviewer/requests/{request['id']}/claim"
    claimed = api.call("POST", claim_path, key=claim_key, user="reviewer-1")
    assert claimed.status_code == 200
    assert api.call("POST", claim_path, key=claim_key, user="reviewer-1").json() == claimed.json()
    # A key belongs to the caller across operations and resources, not just one endpoint.
    different = api.call(
        "POST", f"/reviewer/requests/{uuid4()}/claim", key=claim_key, user="reviewer-1"
    )
    assert different.status_code == 409

    reassign_path = f"/admin/requests/{request['id']}/reassign"
    reassigned = api.call(
        "POST", reassign_path, key=reassign_key, user="admin-1", json={"reviewer_id": "reviewer-2"}
    )
    assert reassigned.status_code == 200
    assert api.call("POST", claim_path, key=claim_key, user="reviewer-1").status_code == 404
    assert (
        api.call(
            "POST",
            reassign_path,
            key=reassign_key,
            user="admin-1",
            json={"reviewer_id": "reviewer-2"},
        ).json()
        == reassigned.json()
    )
    assert (
        api.call(
            "POST",
            reassign_path,
            key=reassign_key,
            user="admin-1",
            json={"reviewer_id": "reviewer-1"},
        ).status_code
        == 409
    )

    decide_path = f"/reviewer/requests/{request['id']}/decision"
    decided = api.call(
        "POST", decide_path, key=decide_key, user="reviewer-2", json=decision(request)
    )
    assert decided.status_code == 200
    assert (
        api.call(
            "POST", decide_path, key=decide_key, user="reviewer-2", json=decision(request)
        ).json()
        == decided.json()
    )
    assert (
        api.call(
            "POST",
            decide_path,
            key=decide_key,
            user="reviewer-2",
            json=decision(request, denied=True),
        ).status_code
        == 409
    )
    # Replaying the original submit returns the current state, not a stale queued snapshot.
    assert (
        api.call("POST", "/office/requests", key=submit_key, json=submission()).json()["status"]
        == "decided"
    )
    audit = api.call("GET", f"/admin/requests/{request['id']}/audit", user="admin-1").json()
    assert [row["action"] for row in audit] == ["submitted", "claimed", "reassigned", "decided"]
    with api.connect() as db:
        assert db.execute("SELECT count(*) AS n FROM write_receipts").fetchone()["n"] == 4


def test_failed_submission_does_not_consume_key(api):
    key = uuid4()
    rejected = api.call(
        "POST", "/office/requests", key=key, json=submission(service_date="2020-01-01")
    )
    assert rejected.status_code == 422
    accepted = api.call("POST", "/office/requests", key=key, json=submission())
    assert accepted.status_code == 201
    with api.connect() as db:
        assert db.execute("SELECT count(*) AS n FROM write_receipts").fetchone()["n"] == 1


def concurrent(*functions):
    barrier = Barrier(len(functions))

    def run(function):
        barrier.wait(timeout=10)
        return function()

    with ThreadPoolExecutor(max_workers=len(functions)) as pool:
        return list(pool.map(run, functions))


def test_competing_claims_have_one_owner_and_one_audit_event(api):
    request = api.submit()
    path = f"/reviewer/requests/{request['id']}/claim"
    responses = concurrent(
        lambda: api.call("POST", path, user="reviewer-1"),
        lambda: api.call("POST", path, user="reviewer-2"),
    )
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(
        response.json()["reviewer_id"] for response in responses if response.status_code == 200
    )
    stored = api.call("GET", f"/admin/requests/{request['id']}", user="admin-1").json()
    assert stored["reviewer_id"] == winner
    audit = api.call("GET", f"/admin/requests/{request['id']}/audit", user="admin-1").json()
    assert [event["action"] for event in audit] == ["submitted", "claimed"]
    assert audit[-1]["actor_id"] == winner


def test_concurrent_same_key_submission_creates_one_resource(api):
    key = uuid4()
    responses = concurrent(
        lambda: api.call("POST", "/office/requests", key=key, json=submission()),
        lambda: api.call("POST", "/office/requests", key=key, json=submission()),
    )
    assert [response.status_code for response in responses] == [201, 201]
    assert responses[0].json() == responses[1].json()
    with api.connect() as db:
        assert db.execute("SELECT count(*) AS n FROM requests").fetchone()["n"] == 1
        assert db.execute("SELECT count(*) AS n FROM audit_events").fetchone()["n"] == 1


def test_competing_decisions_cannot_overwrite_winner(api):
    request = api.claim(api.submit())
    path = f"/reviewer/requests/{request['id']}/decision"
    responses = concurrent(
        lambda: api.call("POST", path, user="reviewer-1", json=decision(request)),
        lambda: api.call("POST", path, user="reviewer-1", json=decision(request, denied=True)),
    )
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(response.json() for response in responses if response.status_code == 200)
    assert (
        api.call("GET", f"/reviewer/requests/{request['id']}", user="reviewer-1").json() == winner
    )
    audit = api.call("GET", f"/admin/requests/{request['id']}/audit", user="admin-1").json()
    assert len([event for event in audit if event["action"] == "decided"]) == 1


def test_concurrent_same_key_decisions_replay_one_commit(api):
    request = api.claim(api.submit())
    key = uuid4()
    path = f"/reviewer/requests/{request['id']}/decision"
    responses = concurrent(
        lambda: api.call("POST", path, key=key, user="reviewer-1", json=decision(request)),
        lambda: api.call("POST", path, key=key, user="reviewer-1", json=decision(request)),
    )
    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].json() == responses[1].json()
    audit = api.call("GET", f"/admin/requests/{request['id']}/audit", user="admin-1").json()
    assert len([event for event in audit if event["action"] == "decided"]) == 1


def test_audit_insert_failure_rolls_back_entire_decision(api):
    request = api.claim(api.submit())
    with api.connect() as db:
        db.execute("""CREATE FUNCTION reject_test_decision_audit() RETURNS trigger
                   LANGUAGE plpgsql AS $$ BEGIN
                     IF NEW.action = 'decided' THEN
                       RAISE EXCEPTION 'injected audit outage' USING ERRCODE = '08006';
                     END IF;
                     RETURN NEW;
                   END $$""")
        db.execute("""CREATE TRIGGER fail_decision_audit BEFORE INSERT ON audit_events
                   FOR EACH ROW EXECUTE FUNCTION reject_test_decision_audit()""")
    key = uuid4()
    response = api.call(
        "POST",
        f"/reviewer/requests/{request['id']}/decision",
        user="reviewer-1",
        key=key,
        json=decision(request),
    )
    assert response.status_code == 503
    assert "injected audit outage" not in response.text
    with api.connect() as db:
        row = db.execute("SELECT * FROM requests WHERE id = %s", (request["id"],)).fetchone()
        assert row["status"] == "in_review"
        assert row["decided_at"] is None
        procedures = db.execute(
            "SELECT * FROM procedures WHERE request_id = %s", (request["id"],)
        ).fetchall()
        assert all(p["decision"] == "pending" and p["decided_by"] is None for p in procedures)
        assert (
            db.execute("SELECT 1 FROM write_receipts WHERE idempotency_key = %s", (key,)).fetchone()
            is None
        )
        assert db.execute("SELECT count(*) AS n FROM audit_events").fetchone()["n"] == 2
        db.execute("DROP TRIGGER fail_decision_audit ON audit_events")
    assert (
        api.call(
            "POST",
            f"/reviewer/requests/{request['id']}/decision",
            user="reviewer-1",
            key=key,
            json=decision(request),
        ).status_code
        == 200
    )


def test_database_rejects_partial_decision_at_commit(api):
    request = api.claim(api.submit())
    with pytest.raises(psycopg.errors.CheckViolation):
        with api.connect() as db:
            db.execute("SELECT set_config('app.actor_id', 'reviewer-1', true)")
            db.execute(
                """UPDATE procedures SET decision = 'approved', decided_by = 'reviewer-1',
                   decided_at = now() WHERE id = %s""",
                (request["procedures"][0]["id"],),
            )
    stored = api.call("GET", f"/reviewer/requests/{request['id']}", user="reviewer-1").json()
    assert all(item["decision"] == "pending" for item in stored["procedures"])


def test_database_requires_audit_actor_for_new_request(api):
    with pytest.raises(psycopg.errors.NotNullViolation):
        with api.connect() as db:
            db.execute(
                """INSERT INTO requests (id, member_id, office_id, diagnosis, service_date)
                   VALUES (%s, 'member-1', 'office-maine', 'M54.5', '2026-11-26')""",
                (uuid4(),),
            )
    with api.connect() as db:
        assert db.execute("SELECT count(*) AS n FROM requests").fetchone()["n"] == 0


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_events SET actor_id = 'admin-1'",
        "DELETE FROM audit_events",
        "TRUNCATE audit_events",
        "UPDATE access_events SET status = 200",
        "DELETE FROM access_events",
        "TRUNCATE access_events",
        "DELETE FROM requests",
        "DELETE FROM procedures",
        "UPDATE requests SET clinical_note = 'changed'",
        "UPDATE procedures SET units = 2",
    ],
)
def test_database_protects_audit_and_submitted_content(api, statement):
    api.submit()
    with pytest.raises(psycopg.errors.CheckViolation):
        with api.connect() as db:
            db.execute(statement)


def test_database_prevents_final_decision_edits(api):
    request = api.claim(api.submit())
    assert (
        api.call(
            "POST",
            f"/reviewer/requests/{request['id']}/decision",
            user="reviewer-1",
            json=decision(request),
        ).status_code
        == 200
    )
    for statement in (
        "UPDATE procedures SET decision = 'denied', reason = 'changed'",
        "UPDATE requests SET status = 'in_review', decided_at = NULL",
    ):
        with pytest.raises(psycopg.errors.CheckViolation):
            with api.connect() as db:
                db.execute("SELECT set_config('app.actor_id', 'reviewer-1', true)")
                db.execute(statement)
