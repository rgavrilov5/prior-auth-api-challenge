"""Prior authorization workflow, shared by HTTP and internal callers."""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from psycopg import Connection

from app.domain import Actor, FieldError, RequestError, fail
from app.idempotency import IdempotentWrite
from app.policy import check_conflict, visibility
from app.schemas import DecisionInput, Submission


def now() -> datetime:
    return datetime.now(UTC)


class PriorAuthorizationService:
    """Workflow for one verified actor and one transaction.

    Supply matching transaction-local app.actor_id and app.trace_id audit context.
    The caller owns commit/rollback. Returned records contain internal fields;
    HTTP adapters apply the caller-specific response models.
    """

    def __init__(
        self, db: Connection[dict], actor: Actor, *, clock: Callable[[], datetime] | None = None
    ):
        self.db = db
        self.actor = actor
        self.clock = clock or now

    def get(self, request_id: UUID, *, lock: bool = False) -> dict:
        condition, parameters = visibility(self.actor)
        # Only fixed policy fragments are interpolated; all caller values are bound.
        row = self.db.execute(
            f"SELECT r.* FROM requests r WHERE r.id = %s AND ({condition})"
            + (" FOR UPDATE" if lock else ""),
            (request_id, *parameters),
        ).fetchone()
        if row is None:
            fail("not_found", "request_id", "not_found", "Request not found")
        return self._with_procedures([row])[0]

    def list_requests(self, limit: int = 50, offset: int = 0) -> list[dict]:
        condition, parameters = visibility(self.actor)
        rows = self.db.execute(
            f"SELECT r.* FROM requests r WHERE {condition} "
            "ORDER BY created_at, id LIMIT %s OFFSET %s",
            (*parameters, limit, offset),
        ).fetchall()
        return self._with_procedures(rows)

    def _with_procedures(self, rows: list[dict]) -> list[dict]:
        if not rows:
            return []
        # Batch the page's procedures so list reads do not add a query per request.
        procedures = self.db.execute(
            "SELECT * FROM procedures WHERE request_id = ANY(%s) ORDER BY code, id",
            ([row["id"] for row in rows],),
        ).fetchall()
        grouped = {row["id"]: [] for row in rows}
        for procedure in procedures:
            grouped[procedure["request_id"]].append(procedure)
        return [{**row, "procedures": grouped[row["id"]]} for row in rows]

    def submit(self, key: UUID, body: Submission) -> dict:
        self.actor.require_role("office")
        write = IdempotentWrite(self.db, self.actor.id, key, "submit", body.model_dump(mode="json"))
        if existing := write.replay():
            return self.get(existing, lock=True)
        self._validate_submission(body)
        request_id = uuid4()
        self.db.execute(
            """INSERT INTO requests
               (id, member_id, office_id, diagnosis, service_date, clinical_note)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (
                request_id,
                body.member_id,
                self.actor.id,
                body.diagnosis,
                body.service_date,
                body.clinical_note,
            ),
        )
        for procedure in body.procedures:
            self.db.execute(
                "INSERT INTO procedures (id, request_id, code, units) VALUES (%s,%s,%s,%s)",
                (uuid4(), request_id, procedure.code, procedure.units),
            )
        # Close the procedure list before commit; the database rejects any later additions.
        self.db.execute("UPDATE requests SET procedures_sealed = true WHERE id = %s", (request_id,))
        write.record(request_id)
        return self.get(request_id)

    def _validate_submission(self, body: Submission) -> None:
        practice = self.db.execute(
            """SELECT p.timezone FROM practices p JOIN practice_members pm ON pm.practice_id = p.id
               WHERE p.id = %s AND pm.member_id = %s""",
            (self.actor.practice_id, body.member_id),
        ).fetchone()
        if not practice:
            fail(
                "forbidden",
                "member_id",
                "patient_not_linked",
                "Patient must be linked to your practice",
            )
        # The practice's local date is authoritative, including around midnight in Hawaii.
        today = self.clock().astimezone(ZoneInfo(practice["timezone"])).date()
        try:
            last_day = today.replace(year=today.year + 1)
        except ValueError:  # February 29 -> February 28 in the following year.
            last_day = today.replace(year=today.year + 1, day=28)
        errors: list[FieldError] = []
        if not today <= body.service_date <= last_day:
            errors.append(
                {
                    "field": "service_date",
                    "code": "date_out_of_range",
                    "message": (
                        f"Choose a date from {today} through {last_day} ({practice['timezone']})"
                    ),
                }
            )
        covered = self.db.execute(
            "SELECT 1 FROM coverages WHERE member_id = %s AND starts_on <= %s AND ends_on >= %s",
            (body.member_id, body.service_date, body.service_date),
        ).fetchone()
        if not covered:
            errors.append(
                {
                    "field": "service_date",
                    "code": "no_coverage",
                    "message": "Patient needs active coverage on the planned service date",
                }
            )
        if errors:
            raise RequestError("invalid", errors)

    def queue(self, limit: int = 50, offset: int = 0) -> list[dict]:
        self.actor.require_role("reviewer")
        # Clinical data becomes available only after claim.
        return self.db.execute(
            """SELECT r.id FROM requests r WHERE status = 'queued' AND NOT EXISTS (
               SELECT 1 FROM reviewer_patients rp WHERE rp.member_id = r.member_id
               AND rp.reviewer_id = %s) ORDER BY created_at, id LIMIT %s OFFSET %s""",
            (self.actor.id, limit, offset),
        ).fetchall()

    def claim(self, key: UUID, request_id: UUID) -> dict:
        self.actor.require_role("reviewer")
        write = IdempotentWrite(self.db, self.actor.id, key, f"claim:{request_id}")
        if existing := write.replay():
            return self.get(existing, lock=True)
        # Keep the state check and assignment under one lock so competing claims have one winner.
        row = self.db.execute(
            "SELECT * FROM requests WHERE id = %s FOR UPDATE", (request_id,)
        ).fetchone()
        if row is None:
            fail("not_found", "request_id", "not_found", "Request not found")
        check_conflict(self.db, self.actor.id, row["member_id"])
        if row["status"] != "queued":
            fail("conflict", "status", "already_claimed", "Request is no longer in the queue")
        self.db.execute(
            "UPDATE requests SET reviewer_id = %s, status = 'in_review' WHERE id = %s",
            (self.actor.id, request_id),
        )
        write.record(request_id)
        return self.get(request_id)

    def decide(self, key: UUID, request_id: UUID, body: DecisionInput) -> dict:
        self.actor.require_role("reviewer")
        write = IdempotentWrite(
            self.db, self.actor.id, key, f"decide:{request_id}", body.model_dump(mode="json")
        )
        if existing := write.replay():
            return self.get(existing, lock=True)
        # Reassignment takes the same lock; ownership is checked again after any wait.
        row = self.get(request_id, lock=True)
        check_conflict(self.db, self.actor.id, row["member_id"])
        if row["status"] != "in_review":
            fail(
                "conflict",
                "status",
                "decision_final",
                "Only an assigned, unfinished request can be decided",
            )
        submitted = {item.procedure_id for item in body.procedures}
        expected = {item["id"] for item in row["procedures"]}
        if len(submitted) != len(body.procedures) or submitted != expected:
            fail(
                "invalid",
                "procedures",
                "incomplete_decision",
                "Decide every procedure exactly once",
            )
        decided_at = self.clock()
        # Procedure outcomes, final status, trigger-written audit and receipt commit together.
        for item in body.procedures:
            self.db.execute(
                """UPDATE procedures
                   SET decision = %s, reason = %s, decided_by = %s, decided_at = %s
                   WHERE id = %s""",
                (item.decision, item.reason, self.actor.id, decided_at, item.procedure_id),
            )
        self.db.execute(
            "UPDATE requests SET status = 'decided', decided_at = %s WHERE id = %s",
            (decided_at, request_id),
        )
        write.record(request_id)
        return self.get(request_id)

    def reassign(self, key: UUID, request_id: UUID, reviewer_id: str) -> dict:
        self.actor.require_role("admin")
        write = IdempotentWrite(
            self.db, self.actor.id, key, f"reassign:{request_id}", {"reviewer_id": reviewer_id}
        )
        if existing := write.replay():
            return self.get(existing, lock=True)
        row = self.get(request_id, lock=True)
        if row["status"] != "in_review":
            fail(
                "conflict",
                "status",
                "not_in_review",
                "Only an unfinished claimed request can be reassigned",
            )
        reviewer = self.db.execute(
            "SELECT 1 FROM users WHERE id = %s AND role = 'reviewer'", (reviewer_id,)
        ).fetchone()
        if not reviewer:
            fail("invalid", "reviewer_id", "invalid_reviewer", "Choose an existing reviewer")
        check_conflict(self.db, reviewer_id, row["member_id"])
        # A no-op still gets a retry receipt, but must not invent a reassignment audit event.
        if row["reviewer_id"] != reviewer_id:
            self.db.execute(
                "UPDATE requests SET reviewer_id = %s WHERE id = %s", (reviewer_id, request_id)
            )
        write.record(request_id)
        return self.get(request_id)

    def audit(self, request_id: UUID) -> list[dict]:
        self.actor.require_role("admin")
        if not self.db.execute("SELECT 1 FROM requests WHERE id = %s", (request_id,)).fetchone():
            fail("not_found", "request_id", "not_found", "Request not found")
        return self.db.execute(
            "SELECT * FROM audit_events WHERE request_id = %s ORDER BY id", (request_id,)
        ).fetchall()

    def support(self, trace_id: UUID) -> dict:
        self.actor.require_role("admin")
        row = self.db.execute(
            "SELECT * FROM access_events WHERE trace_id = %s", (trace_id,)
        ).fetchone()
        if row is None:
            fail("not_found", "trace_id", "not_found", "Call not found")
        return row
