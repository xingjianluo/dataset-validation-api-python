"""统一错误结构：所有被拒绝/非法的结论都带稳定的机器可读 code。"""
from __future__ import annotations

from fastapi.responses import JSONResponse

from app.models import ErrorCode


def error_response(
    code: ErrorCode,
    message: str,
    status_code: int,
    details: dict | None = None,
) -> JSONResponse:
    payload: dict = {
        "error": {
            "code": code.value,
            "message": message,
        }
    }
    if details is not None:
        payload["error"]["details"] = details
    return JSONResponse(status_code=status_code, content=payload)
