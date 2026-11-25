"""A local stand-in for the identity service, never an API endpoint."""

import argparse
from datetime import UTC, datetime, timedelta

import jwt
import psycopg

from app.auth import AUDIENCE, ISSUER, signing_key
from app.db import database_url


def issue_token(user_id: str, role: str, *, expires_in=3600) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": user_id,
            "role": role,
            "iss": ISSUER,
            "aud": AUDIENCE,
            "iat": now,
            "exp": now + timedelta(seconds=expires_in),
        },
        signing_key(),
        algorithm="HS256",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("user_id", help="Seeded person or partner system, e.g. partner-maine")
    args = parser.parse_args()
    with psycopg.connect(database_url()) as db:
        user = db.execute("SELECT role FROM users WHERE id = %s", (args.user_id,)).fetchone()
    if user is None:
        parser.error("Unknown synthetic identity")
    print(issue_token(args.user_id, user[0]))
