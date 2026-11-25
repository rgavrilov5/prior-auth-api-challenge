from dataclasses import dataclass
from typing import Literal, Never, TypedDict

ErrorKind = Literal["forbidden", "not_found", "conflict", "invalid"]


class FieldError(TypedDict):
    field: str
    code: str
    message: str


class RequestError(Exception):
    """An expected workflow failure; the HTTP adapter chooses the response status."""

    def __init__(self, kind: ErrorKind, errors: list[FieldError]):
        super().__init__(kind)
        self.kind = kind
        self.errors = errors


def fail(kind: ErrorKind, field: str, code: str, message: str) -> Never:
    raise RequestError(kind, [{"field": field, "code": code, "message": message}])


@dataclass(frozen=True)
class Actor:
    """A verified identity, not a caller-supplied user ID or role."""

    id: str
    role: str
    practice_id: str | None

    def require_role(self, role: str) -> None:
        if self.role != role:
            fail("forbidden", "role", "forbidden", f"This action requires the {role} role")
