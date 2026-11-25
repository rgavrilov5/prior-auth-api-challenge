import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event, current_thread
from time import monotonic
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from app.bootstrap import MIGRATIONS, migrate, seed
from app.domain import Actor
from app.schemas import Submission
from app.service import PriorAuthorizationService
from tests.conftest import decision, submission


def set_audit_context(db, actor_id):
    db.execute("SELECT set_config('app.actor_id', %s, true)", (actor_id,))
    db.execute("SELECT set_config('app.trace_id', %s, true)", (str(uuid4()),))


@pytest.mark.parametrize("status", ["queued", "in_review", "decided"])
def test_database_rejects_procedures_added_after_submission(api, status):
    request = api.submit()
    assert len(request["procedures"]) == 2
    if status != "queued":
        api.claim(request)
    if status == "decided":
        result = api.call(
            "POST",
            f"/reviewer/requests/{request['id']}/decision",
            user="reviewer-1",
            json=decision(request),
        )
        assert result.status_code == 200
    with pytest.raises(psycopg.errors.CheckViolation) as failure:
        with api.connect() as db:
            # Even a fully attributed, pre-approved insertion into a final request is forbidden.
            set_audit_context(db, "reviewer-1" if status == "decided" else "office-maine")
            db.execute(
                """INSERT INTO procedures
                   (id, request_id, code, units, decision, decided_by, decided_at)
                   VALUES (%s, %s, '99214', 1, %s, %s, %s)""",
                (
                    uuid4(),
                    request["id"],
                    "approved" if status == "decided" else "pending",
                    "reviewer-1" if status == "decided" else None,
                    datetime(2026, 11, 25, 12, tzinfo=UTC) if status == "decided" else None,
                ),
            )
    assert failure.value.sqlstate == "23514"
    stored = api.call("GET", f"/admin/requests/{request['id']}", user="admin-1")
    assert stored.status_code == 200
    assert stored.json()["status"] == status
    assert len(stored.json()["procedures"]) == 2


def test_submitted_procedure_set_cannot_be_reopened(api):
    request = api.submit()
    with api.connect() as db:
        original = db.execute(
            "SELECT procedures_sealed FROM requests WHERE id = %s", (request["id"],)
        ).fetchone()["procedures_sealed"]
    assert original is True
    with pytest.raises(psycopg.errors.CheckViolation) as failure:
        with api.connect() as db:
            set_audit_context(db, "reviewer-1")
            # An otherwise-valid claim must not allow the procedure set to be reopened.
            db.execute(
                """UPDATE requests SET status = 'in_review', reviewer_id = 'reviewer-1',
                   procedures_sealed = false WHERE id = %s""",
                (request["id"],),
            )
    assert failure.value.sqlstate == "23514"
    with api.connect() as db:
        stored = db.execute(
            "SELECT status, procedures_sealed FROM requests WHERE id = %s", (request["id"],)
        ).fetchone()
        assert stored == {"status": "queued", "procedures_sealed": True}


def test_unsealed_submission_cannot_commit(api):
    request_id = uuid4()
    with pytest.raises(psycopg.errors.CheckViolation) as failure:
        with api.connect() as db:
            set_audit_context(db, "office-maine")
            db.execute(
                """INSERT INTO requests (id, member_id, office_id, diagnosis, service_date)
                   VALUES (%s, 'member-1', 'office-maine', 'M54.5', '2026-11-26')""",
                (request_id,),
            )
            db.execute(
                "INSERT INTO procedures (id, request_id, code, units) VALUES (%s,%s,'99213',1)",
                (uuid4(), request_id),
            )
    assert failure.value.sqlstate == "23514"
    with api.connect() as db:
        assert db.execute("SELECT 1 FROM requests WHERE id = %s", (request_id,)).fetchone() is None
        assert (
            db.execute("SELECT 1 FROM audit_events WHERE request_id = %s", (request_id,)).fetchone()
            is None
        )


def test_sealing_cannot_hide_a_change_to_submitted_content(api):
    request_id = uuid4()
    with pytest.raises(psycopg.errors.CheckViolation):
        with api.connect() as db:
            set_audit_context(db, "office-maine")
            db.execute(
                """INSERT INTO requests
                   (id, member_id, office_id, diagnosis, service_date, clinical_note)
                   VALUES (%s, 'member-1', 'office-maine', 'M54.5', '2026-11-26', 'original')""",
                (request_id,),
            )
            db.execute(
                "INSERT INTO procedures (id, request_id, code, units) VALUES (%s,%s,'99213',1)",
                (uuid4(), request_id),
            )
            # Sealing skips a second audit event only when every other field stays unchanged.
            db.execute(
                "UPDATE requests SET procedures_sealed = true, clinical_note = 'changed' "
                "WHERE id = %s",
                (request_id,),
            )
    with api.connect() as db:
        assert db.execute("SELECT 1 FROM requests WHERE id = %s", (request_id,)).fetchone() is None


def test_migration_preserves_legacy_requests_and_accepts_new_submissions(api):
    schema = f"upgrade_{uuid4().hex}"
    base_url = os.environ["TEST_DATABASE_URL"]
    legacy_id = uuid4()
    with psycopg.connect(base_url, autocommit=True) as db:
        db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    database_url = make_conninfo(base_url, options=f"-csearch_path={schema}")
    try:
        with psycopg.connect(database_url, row_factory=dict_row) as db:
            db.execute((MIGRATIONS / "001_initial.sql").read_text())
            db.execute("CREATE TABLE schema_migrations (name text PRIMARY KEY)")
            db.execute("INSERT INTO schema_migrations VALUES ('001_initial.sql')")
            seed(db)
            set_audit_context(db, "office-maine")
            db.execute(
                """INSERT INTO requests
                   (id, member_id, office_id, diagnosis, service_date, clinical_note)
                   VALUES (%s, 'member-1', 'office-maine', 'M54.5', '2026-11-26', 'old note')""",
                (legacy_id,),
            )
            db.execute(
                "INSERT INTO procedures (id, request_id, code, units) VALUES (%s,%s,'99213',1)",
                (uuid4(), legacy_id),
            )
            before = db.execute("SELECT * FROM requests WHERE id = %s", (legacy_id,)).fetchone()
        with psycopg.connect(database_url, row_factory=dict_row) as db:
            migrate(db)
        with psycopg.connect(database_url, row_factory=dict_row) as db:
            migrate(db)  # Restarting the bootstrap must be safe too.
            after = db.execute("SELECT * FROM requests WHERE id = %s", (legacy_id,)).fetchone()
            assert after.pop("procedures_sealed") is True
            assert after == before
            assert db.execute("SELECT count(*) AS n FROM audit_events").fetchone()["n"] == 1
            set_audit_context(db, "office-maine")
            workflow = PriorAuthorizationService(
                db,
                Actor("office-maine", "office", "maine"),
                clock=lambda: datetime(2026, 11, 25, 12, tzinfo=UTC),
            )
            new_request = workflow.submit(uuid4(), Submission(**submission()))
            assert len(new_request["procedures"]) == 2
            assert (
                db.execute(
                    "SELECT procedures_sealed FROM requests WHERE id = %s",
                    (new_request["id"],),
                ).fetchone()["procedures_sealed"]
                is True
            )
        with psycopg.connect(database_url, row_factory=dict_row) as db:
            set_audit_context(db, "reviewer-1")
            workflow = PriorAuthorizationService(db, Actor("reviewer-1", "reviewer", None))
            assert workflow.claim(uuid4(), legacy_id)["status"] == "in_review"
    finally:
        with psycopg.connect(base_url, autocommit=True) as db:
            db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.mark.parametrize("operation", ["submit", "claim", "reassign"])
def test_write_replay_returns_one_coherent_state_during_decision(api, monkeypatch, operation):
    key = uuid4()
    request = api.submit(key=key if operation == "submit" else uuid4())
    api.claim(request, key=key if operation == "claim" else uuid4())
    reviewer = "reviewer-1"
    if operation == "reassign":
        reviewer = "reviewer-2"
        response = api.call(
            "POST",
            f"/admin/requests/{request['id']}/reassign",
            user="admin-1",
            key=key,
            json={"reviewer_id": reviewer},
        )
        assert response.status_code == 200
    metadata_read, release_reader, decision_done = Event(), Event(), Event()
    original_procedures = PriorAuthorizationService._with_procedures
    original_decide = PriorAuthorizationService.decide
    writer_name = f"review_writer_{uuid4().hex}"

    def pause_replay(workflow, rows):
        if current_thread().name.startswith("review-replay"):
            assert rows[0]["status"] == "in_review"
            metadata_read.set()
            assert release_reader.wait(timeout=15), "Replay was never released"
        return original_procedures(workflow, rows)

    def identify_writer(workflow, *args, **kwargs):
        workflow.db.execute("SELECT set_config('application_name', %s, true)", (writer_name,))
        return original_decide(workflow, *args, **kwargs)

    monkeypatch.setattr(PriorAuthorizationService, "_with_procedures", pause_replay)
    monkeypatch.setattr(PriorAuthorizationService, "decide", identify_writer)

    def replay():
        actors = {
            "submit": Actor("office-maine", "office", "maine"),
            "claim": Actor("reviewer-1", "reviewer", None),
            "reassign": Actor("admin-1", "admin", None),
        }
        with api.connect() as db:
            actor = actors[operation]
            set_audit_context(db, actor.id)
            workflow = PriorAuthorizationService(db, actor)
            if operation == "submit":
                return workflow.submit(key, Submission(**submission()))
            if operation == "claim":
                return workflow.claim(key, UUID(request["id"]))
            return workflow.reassign(key, UUID(request["id"]), reviewer)

    def decide():
        try:
            return api.call(
                "POST",
                f"/reviewer/requests/{request['id']}/decision",
                user=reviewer,
                json=decision(request),
            )
        finally:
            decision_done.set()

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="review-replay") as readers:
        with ThreadPoolExecutor(max_workers=1) as writers:
            try:
                reading = readers.submit(replay)
                assert metadata_read.wait(timeout=10), "Replay did not read request metadata"
                writing = writers.submit(decide)
                with api.connect() as observer:
                    observer.autocommit = True
                    deadline = monotonic() + 10
                    while not decision_done.is_set():
                        blocked = observer.execute(
                            """SELECT cardinality(pg_blocking_pids(pid)) > 0 AS blocked
                               FROM pg_stat_activity WHERE application_name = %s""",
                            (writer_name,),
                        ).fetchone()
                        if blocked and blocked["blocked"]:
                            break
                        assert monotonic() < deadline, (
                            "Decision neither finished nor waited on replay"
                        )
                        decision_done.wait(timeout=0.01)
            finally:
                release_reader.set()
            replayed = reading.result(timeout=10)
            decided = writing.result(timeout=10)
    assert replayed["status"] == "in_review"
    assert all(item["decision"] == "pending" for item in replayed["procedures"])
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "decided"
