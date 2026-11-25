from fastapi import HTTPException


def reject(status: int, field: str, code: str, message: str) -> None:
    raise HTTPException(status, detail=[{"field": field, "code": code, "message": message}])
