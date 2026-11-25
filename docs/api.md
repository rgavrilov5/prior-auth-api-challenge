# Partner contract

Use JSON and `Authorization: Bearer <token>`. Obtain the token from the identity service; the demo CLI replaces it locally. People and partner systems have a stable subject and stored permissions.

Every POST requires `Idempotency-Key: <uuid>`. Reuse it for the same operation and body. Keys are scoped to the subject across all writes. A successful replay returns the same resource's current state without repeating the write. Failed writes do not consume a key. A successful key used for different content gets `409`. Replays still require current authorization.

| Method | Path | Result |
| --- | --- | --- |
| POST | `/office/requests` | Submit; `201`, including retries |
| GET | `/office/requests` or `/office/requests/{id}` | Own submissions |
| GET | `/member/requests` or `/member/requests/{id}` | Own requests; no internal fields |
| GET | `/reviewer/queue` | Claimable IDs; treating-patient conflicts excluded |
| GET | `/reviewer/requests` or `/reviewer/requests/{id}` | Assigned requests |
| POST | `/reviewer/requests/{id}/claim` | Claim, no body; `200` |
| POST | `/reviewer/requests/{id}/decision` | Decide all procedures; `200` |
| GET | `/admin/requests` or `/admin/requests/{id}` | Any request |
| POST | `/admin/requests/{id}/reassign` | Reassign unfinished work; `200` |
| GET | `/admin/requests/{id}/audit` | Ordered transition history |
| GET | `/admin/support/{request_id}` | Find a call by its `X-Request-ID` |

Lists accept `limit` (1–100, default 50) and `offset` (0–10,000, default 0). Requests sort by creation time and ID. Queue contents change; a later claim may get `409`.

## Submit

Choose a covered service date in the allowed local window:

```json
{
  "member_id": "member-1",
  "diagnosis": "M54.5",
  "service_date": "2026-12-01",
  "procedures": [{"code": "99213", "units": 1}, {"code": "J1100", "units": 2}],
  "clinical_note": "Synthetic clinical note"
}
```

The response includes request and procedure IDs. Use the procedure IDs when deciding. Dates are `YYYY-MM-DD`; timestamps are UTC ISO 8601. Unknown fields are rejected.

## Decide or reassign

```json
{
  "procedures": [
    {"procedure_id": "<first procedure UUID>", "decision": "approved"},
    {"procedure_id": "<second procedure UUID>", "decision": "denied", "reason": "Internal reason"}
  ]
}
```

Include every procedure exactly once. Only the assigned reviewer can decide. Admin reassignment takes `{"reviewer_id": "reviewer-2"}`. Decided requests cannot be changed with a fresh key.

## Errors and support

Every response has an `X-Request-ID`. Errors include the same ID and actionable fields:

```json
{
  "request_id": "<call UUID>",
  "errors": [{"field": "body.procedures.0.units", "code": "less_than_equal", "message": "Input should be less than or equal to 30"}]
}
```

`401`: invalid identity. `403`: wrong role or forbidden relationship. `404`: missing or inaccessible resource. `409`: workflow conflict or reused key. `422`: fix the named fields. `503`: temporary failure; retry with the same key. Keep the key after a timeout: the write may already have committed.

Give support the request ID, not clinical data. An admin can look it up even when validation failed before a request existed. During a database outage, call metadata is available in service logs.

Exact schemas: `/docs` or `/openapi.json`. Executable example with fresh IDs and dates: `python -m scripts.walkthrough`.
