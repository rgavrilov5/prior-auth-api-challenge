import hashlib
import json
from uuid import UUID

from psycopg import Connection

from app.domain import fail


class IdempotentWrite:
    """One caller's write attempt. Its receipt shares the caller's transaction."""

    def __init__(
        self,
        db: Connection[dict],
        actor_id: str,
        key: UUID,
        operation: str,
        payload: dict | None = None,
    ):
        self.db = db
        self.actor_id = actor_id
        self.key = key
        self.operation = operation
        self.fingerprint = hashlib.sha256(
            json.dumps(payload or {}, sort_keys=True).encode()
        ).hexdigest()

    def replay(self) -> UUID | None:
        # Lock the key even before a receipt exists, so concurrent retries cannot both write.
        self.db.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"{self.actor_id}:{self.key}",),
        )
        receipt = self.db.execute(
            "SELECT * FROM write_receipts WHERE actor_id = %s AND idempotency_key = %s",
            (self.actor_id, self.key),
        ).fetchone()
        if receipt is None:
            return None
        if receipt["operation"] != self.operation or receipt["fingerprint"] != self.fingerprint:
            fail("conflict", "Idempotency-Key", "key_reused", "Use a new key for a different write")
        return receipt["request_id"]

    def record(self, request_id: UUID) -> None:
        self.db.execute(
            """INSERT INTO write_receipts
               (actor_id, idempotency_key, operation, fingerprint, request_id)
               VALUES (%s,%s,%s,%s,%s)""",
            (self.actor_id, self.key, self.operation, self.fingerprint, request_id),
        )
