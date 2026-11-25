"""The workflow. HTTP routes and identity-provider details stay outside it."""

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from app.auth import Actor
from app.errors import reject
from app.policy import check_conflict, visibility
from app.schemas import DecisionInput, Submission


def now() -> datetime:
    return datetime.now(UTC)


def begin_write(db, actor: Actor, key: UUID, operation: str, payload: dict) -> UUID | None:
    """Serialize retries of one caller/key; different writes still run concurrently."""
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"{actor.id}:{key}",))
    receipt = db.execute(
        "SELECT * FROM write_receipts WHERE actor_id = %s AND idempotency_key = %s",
        (actor.id, key),
    ).fetchone()
    if receipt:
        if receipt["operation"] != operation or receipt["fingerprint"] != fingerprint:
            reject(409, "Idempotency-Key", "key_reused", "Use a new key for a different write")
        return receipt["request_id"]
    return None


def finish_write(db, actor: Actor, key: UUID, operation: str, payload: dict, request_id: UUID):
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    db.execute(
        """INSERT INTO write_receipts
           (actor_id, idempotency_key, operation, fingerprint, request_id)
           VALUES (%s,%s,%s,%s,%s)""",
        (actor.id, key, operation, fingerprint, request_id),
    )


def fetch(db, actor: Actor, request_id: UUID, *, lock=False) -> dict:
    condition, parameters = visibility(actor)
    # Only internal, fixed policy fragments are interpolated; all values are bound.
    row = db.execute(
        f"SELECT r.* FROM requests r WHERE r.id = %s AND ({condition})"
        + (" FOR UPDATE" if lock else ""),
        (request_id, *parameters),
    ).fetchone()
    if row is None:
        reject(404, "request_id", "not_found", "Request not found")
    row["procedures"] = db.execute(
        "SELECT * FROM procedures WHERE request_id = %s ORDER BY code, id", (request_id,)
    ).fetchall()
    return row


def list_requests(db, actor: Actor, limit: int, offset: int) -> list[dict]:
    condition, parameters = visibility(actor)
    rows = db.execute(
        f"SELECT r.* FROM requests r WHERE {condition} ORDER BY created_at, id LIMIT %s OFFSET %s",
        (*parameters, limit, offset),
    ).fetchall()
    if not rows:
        return []
    # Two queries per page, rather than one procedure query for every request.
    procedures = db.execute(
        "SELECT * FROM procedures WHERE request_id = ANY(%s) ORDER BY code, id",
        ([row["id"] for row in rows],),
    ).fetchall()
    grouped = {row["id"]: [] for row in rows}
    for procedure in procedures:
        grouped[procedure["request_id"]].append(procedure)
    return [{**row, "procedures": grouped[row["id"]]} for row in rows]


def submit(db, actor: Actor, key: UUID, body: Submission) -> dict:
    payload = body.model_dump(mode="json")
    operation = "submit"
    if existing := begin_write(db, actor, key, operation, payload):
        return fetch(db, actor, existing)
    linked = db.execute(
        "SELECT 1 FROM practice_members WHERE practice_id = %s AND member_id = %s",
        (actor.practice_id, body.member_id),
    ).fetchone()
    if not linked:
        reject(403, "member_id", "patient_not_linked", "Patient must be linked to your practice")
    practice = db.execute(
        "SELECT timezone FROM practices WHERE id = %s", (actor.practice_id,)
    ).fetchone()
    today = now().astimezone(ZoneInfo(practice["timezone"])).date()
    try:
        last_day = today.replace(year=today.year + 1)
    except ValueError:  # February 29 -> February 28 in the following year.
        last_day = today.replace(year=today.year + 1, day=28)
    errors = []
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
    covered = db.execute(
        """SELECT 1 FROM coverages WHERE member_id = %s AND starts_on <= %s AND ends_on >= %s""",
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
        raise HTTPException(422, detail=errors)
    request_id = uuid4()
    db.execute(
        """INSERT INTO requests (id, member_id, office_id, diagnosis, service_date, clinical_note)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (
            request_id,
            body.member_id,
            actor.id,
            body.diagnosis,
            body.service_date,
            body.clinical_note,
        ),
    )
    for procedure in body.procedures:
        db.execute(
            "INSERT INTO procedures (id, request_id, code, units) VALUES (%s,%s,%s,%s)",
            (uuid4(), request_id, procedure.code, procedure.units),
        )
    finish_write(db, actor, key, operation, payload, request_id)
    return fetch(db, actor, request_id)


def queue(db, actor: Actor, limit: int, offset: int) -> list[dict]:
    rows = db.execute(
        """SELECT r.id FROM requests r WHERE status = 'queued' AND NOT EXISTS (
           SELECT 1 FROM reviewer_patients rp WHERE rp.member_id = r.member_id
           AND rp.reviewer_id = %s) ORDER BY created_at, id LIMIT %s OFFSET %s""",
        (actor.id, limit, offset),
    ).fetchall()
    # Queue is deliberately minimal: patient/clinical data is available after claim.
    return rows


def claim(db, actor: Actor, key: UUID, request_id: UUID) -> dict:
    operation = f"claim:{request_id}"
    if existing := begin_write(db, actor, key, operation, {}):
        return fetch(db, actor, existing)
    row = db.execute("SELECT * FROM requests WHERE id = %s FOR UPDATE", (request_id,)).fetchone()
    if row is None:
        reject(404, "request_id", "not_found", "Request not found")
    check_conflict(db, actor.id, row["member_id"])
    if row["status"] != "queued":
        reject(409, "status", "already_claimed", "Request is no longer in the queue")
    db.execute(
        "UPDATE requests SET reviewer_id = %s, status = 'in_review' WHERE id = %s",
        (actor.id, request_id),
    )
    finish_write(db, actor, key, operation, {}, request_id)
    return fetch(db, actor, request_id)


def decide(db, actor: Actor, key: UUID, request_id: UUID, body: DecisionInput) -> dict:
    operation = f"decide:{request_id}"
    payload = body.model_dump(mode="json")
    if existing := begin_write(db, actor, key, operation, payload):
        return fetch(db, actor, existing)
    row = fetch(db, actor, request_id, lock=True)
    check_conflict(db, actor.id, row["member_id"])
    if row["status"] != "in_review":
        reject(
            409, "status", "decision_final", "Only an assigned, unfinished request can be decided"
        )
    submitted = [item.procedure_id for item in body.procedures]
    expected = {item["id"] for item in row["procedures"]}
    if len(submitted) != len(set(submitted)) or set(submitted) != expected:
        reject(422, "procedures", "incomplete_decision", "Decide every procedure exactly once")
    decided_at = now()
    for item in body.procedures:
        db.execute(
            """UPDATE procedures SET decision = %s, reason = %s, decided_by = %s, decided_at = %s
               WHERE id = %s""",
            (item.decision, item.reason, actor.id, decided_at, item.procedure_id),
        )
    db.execute(
        "UPDATE requests SET status = 'decided', decided_at = %s WHERE id = %s",
        (decided_at, request_id),
    )
    finish_write(db, actor, key, operation, payload, request_id)
    return fetch(db, actor, request_id)


def reassign(db, actor: Actor, key: UUID, request_id: UUID, reviewer_id: str) -> dict:
    operation = f"reassign:{request_id}"
    payload = {"reviewer_id": reviewer_id}
    if existing := begin_write(db, actor, key, operation, payload):
        return fetch(db, actor, existing)
    row = fetch(db, actor, request_id, lock=True)
    if row["status"] != "in_review":
        reject(
            409, "status", "not_in_review", "Only an unfinished claimed request can be reassigned"
        )
    reviewer = db.execute(
        "SELECT 1 FROM users WHERE id = %s AND role = 'reviewer'", (reviewer_id,)
    ).fetchone()
    if not reviewer:
        reject(422, "reviewer_id", "invalid_reviewer", "Choose an existing reviewer")
    check_conflict(db, reviewer_id, row["member_id"])
    if row["reviewer_id"] != reviewer_id:
        db.execute("UPDATE requests SET reviewer_id = %s WHERE id = %s", (reviewer_id, request_id))
    finish_write(db, actor, key, operation, payload, request_id)
    return fetch(db, actor, request_id)
