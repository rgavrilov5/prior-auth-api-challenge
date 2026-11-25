import os
from dataclasses import dataclass
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.db import DB
from app.errors import reject

ISSUER = "prior-auth-demo"
AUDIENCE = "prior-auth-api"
ROLES = {"member", "office", "reviewer", "admin"}
bearer = HTTPBearer(auto_error=False)


def signing_key() -> str:
    secret = os.environ.get("JWT_SECRET", "")
    if len(secret) < 32:
        raise RuntimeError("JWT_SECRET must contain at least 32 characters")
    return secret


@dataclass(frozen=True)
class Actor:
    id: str
    role: str
    practice_id: str | None


def authenticate(
    request: Request,
    db: DB,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Actor:
    try:
        if credentials is None:
            raise ValueError("missing token")
        claims = jwt.decode(
            credentials.credentials,
            signing_key(),
            algorithms=["HS256"],
            issuer=ISSUER,
            audience=AUDIENCE,
            options={"require": ["sub", "role", "exp", "iat", "iss", "aud"]},
        )
        if claims["role"] not in ROLES:
            raise ValueError("unknown role")
        user = db.execute("SELECT * FROM users WHERE id = %s", (claims["sub"],)).fetchone()
        if user is None or user["role"] != claims["role"]:
            raise ValueError("unknown identity")
    except (jwt.InvalidTokenError, ValueError, TypeError):
        raise HTTPException(
            401,
            detail=[{"field": "token", "code": "invalid_token", "message": "Valid token required"}],
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    actor = Actor(user["id"], user["role"], user["practice_id"])
    request.state.actor_id = actor.id
    db.execute("SELECT set_config('app.actor_id', %s, true)", (actor.id,))
    db.execute("SELECT set_config('app.trace_id', %s, true)", (request.state.trace_id,))
    return actor


Identity = Annotated[Actor, Depends(authenticate)]


def allow(actor: Actor, role: str) -> None:
    if actor.role != role:
        reject(403, "role", "forbidden", f"This action requires the {role} role")
