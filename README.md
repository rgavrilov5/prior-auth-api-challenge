# Prior Authorization API

**What:** An office asks for approval before a patient's procedure. A reviewer decides each procedure, and the patient can see the outcome.

**Why:** The wrong person must not see patient data or make a decision. Every change needs an owner and a history we can explain.

**How:** The caller gets a token from the identity service. This API verifies it, authorizes the request, and saves the change and its audit record together in PostgreSQL.

## Run it

Requires Docker with Compose:

```sh
docker compose up --build
```

Open [the API docs](http://localhost:8000/docs). All seed data is synthetic. The database persists across restarts.

Run the complete submit → claim → decide → member-view example:

```sh
docker compose exec api .venv/bin/python -m scripts.walkthrough
```

For a token to paste into **Authorize** in the API docs:

```sh
docker compose exec api .venv/bin/python -m scripts.token office-maine
```

Other subjects: `office-hawaii`, `office-colleague`, `partner-maine` (a service principal), `member-1`, `member-2`, `member-3`, `member-uncovered`, `reviewer-1`, `reviewer-2`, `admin-1`. Tokens last one hour. `reviewer-1` treats `member-2` and cannot review their requests.

## Test it

```sh
docker compose --profile test run --build --rm test
```

Tests use real PostgreSQL and an isolated schema per test. They cover access boundaries, local dates, concurrent writes, safe retries, support traces, audit rollback and final decisions.

GitHub Actions reports lint and PostgreSQL integration separately. Integration builds the containers, runs the suite, waits for API readiness, then exercises the full public workflow. Failed runs include container diagnostics; every run removes its test data.

Without Docker, install Python 3.12+, [uv](https://docs.astral.sh/uv/) and PostgreSQL 17, create a database, then:

```sh
uv sync --frozen
export DATABASE_URL=postgresql://localhost/prior_auth
export JWT_SECRET=synthetic-demo-only-signing-key-please-replace
uv run python -m app.bootstrap --seed
uv run uvicorn app.main:app --no-access-log
```

In another shell, use the same environment and run `uv run python -m scripts.walkthrough`. For tests, set `TEST_DATABASE_URL` to a local test database, then run `uv run pytest`. Checks: `uv run ruff check .` and `uv run ruff format --check .`.

## Who can do what

| Caller | Actions |
| --- | --- |
| Office or partner system | Submit, list and read its own requests |
| Member | List and read their own requests and procedure outcomes |
| Reviewer | See the queue, claim work, read assignments, submit a complete decision |
| Admin | Read everything, reassign unfinished work, inspect audit and support records |

Each group has its own route module and response contract. `PriorAuthorizationService` owns the workflow and caller permissions; `IdempotentWrite` owns safe retries. HTTP wiring stays in `app/main.py`, and error/tracing code in `app/http.py`. See [design choices](docs/design.md), [partner contract](docs/api.md), and the [original challenge](CHALLENGE.md).

## Calls I made where the brief was open

- “Office” means the token's subject, including a partner system. Two callers at one practice cannot read each other's submissions.
- Clinical notes are optional text, up to 20,000 characters. Offices can read their own notes. Reviewer reasoning stays with reviewers and admins; members receive neither.
- Dates run from today through the same calendar date next year in the submitting practice's IANA timezone, inclusively. February 29 maps to February 28. Coverage includes both boundary dates.
- Units must be integers from 1 to 30. Codes are checked for CPT/HCPCS and ICD-10 shape; catalog validation is outside this exercise. Combine duplicate procedure codes.
- A mixed result ends in `decided`; each procedure says `approved` or `denied`. Denials need a nonblank internal reason. Decisions and submitted content cannot be edited.
- Reviewers get clinical details after claiming. Admins can reassign claimed, unfinished work, but cannot override a treating-patient conflict or decide on a reviewer's behalf.
- Every write needs a UUID `Idempotency-Key`. Retrying returns the same resource's current state. Changing the body or operation requires a new key. Successful keys are retained for this exercise.

## Back burner

Reads use the primary database and one committed snapshot per GET. A read already running can show its earlier snapshot after a write finishes. No replica or cache yet. Follow-ups: [read freshness #1](https://github.com/spfuzzylink/prior-auth-api-challenge-submission/issues/1), [real identity service #2](https://github.com/spfuzzylink/prior-auth-api-challenge-submission/issues/2), [AWS and patient-data readiness #3](https://github.com/spfuzzylink/prior-auth-api-challenge-submission/issues/3), [notifications and workload counts #4](https://github.com/spfuzzylink/prior-auth-api-challenge-submission/issues/4).

The demo key and database-owner account are local conveniences. The container can run on AWS with PostgreSQL on RDS; production credentials, transport security and audit retention belong in the deployment issue.

## AI use

I used Codex to map requirements, implement the API, write PostgreSQL integration tests and review the result. I steered it toward client-obtained identity, server-side authorization, idempotent writes, and a small backlog instead of speculative infrastructure. Review caught a startup race, fixed with a `/health` readiness check. A second pass moved role checks into the service and separated programming errors from retryable database failures. CodeRabbit then caught a mixed-state retry response and a direct-SQL procedure insert gap; both now have regression tests. Independent review and the PostgreSQL suite verify each iteration before publication.
