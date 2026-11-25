"""Run the synthetic submit-to-decision flow against the local API."""

import argparse
import json
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from scripts.token import issue_token


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()

    def call(method, path, subject, role, body=None, key=None, expected=200):
        headers = {"Authorization": "Bearer " + issue_token(subject, role)}
        if method == "POST":
            headers["Idempotency-Key"] = key or str(uuid4())
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            args.base_url + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
            method=method,
        )
        try:
            response = urlopen(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            result = json.load(response)
            if response.status != expected:
                raise RuntimeError(f"Unexpected status {response.status}: {result}")
            return result, response.headers["X-Request-ID"]

    submission = {
        "member_id": "member-1",
        "diagnosis": "M54.5",
        "service_date": str((datetime.now(UTC) + timedelta(days=1)).date()),
        "procedures": [{"code": "99213", "units": 1}, {"code": "J1100", "units": 2}],
        "clinical_note": "Synthetic clinical note",
    }
    key = str(uuid4())
    created, _ = call("POST", "/office/requests", "partner-maine", "office", submission, key, 201)
    replay, _ = call("POST", "/office/requests", "partner-maine", "office", submission, key, 201)
    assert created["id"] == replay["id"]
    request_id = created["id"]
    print(f"1. Partner submitted {request_id}; retry returned the same request.")
    call("POST", f"/reviewer/requests/{request_id}/claim", "reviewer-2", "reviewer")
    print("2. Reviewer claimed it.")
    decision = {
        "procedures": [
            {
                "procedure_id": item["id"],
                "decision": "denied" if index else "approved",
                **({"reason": "Synthetic internal reason"} if index else {}),
            }
            for index, item in enumerate(created["procedures"])
        ]
    }
    call("POST", f"/reviewer/requests/{request_id}/decision", "reviewer-2", "reviewer", decision)
    print("3. Reviewer submitted every procedure decision together.")
    member, _ = call("GET", f"/member/requests/{request_id}", "member-1", "member")
    assert "clinical_note" not in member
    assert all("reason" not in item for item in member["procedures"])
    print("4. Member sees outcomes:", [item["decision"] for item in member["procedures"]])
    audit, _ = call("GET", f"/admin/requests/{request_id}/audit", "admin-1", "admin")
    print("5. Admin sees history:", [event["action"] for event in audit])
    invalid = {**submission, "procedures": [{"code": "99213", "units": 31}]}
    _, trace = call("POST", "/office/requests", "partner-maine", "office", invalid, expected=422)
    support, _ = call("GET", f"/admin/support/{trace}", "admin-1", "admin")
    print(f"6. Support found rejected call {trace}: {support['error_codes']}")


if __name__ == "__main__":
    main()
