"""HTTP 路由层：请求信封校验 -> 提交/查询 -> 统一错误响应与日志。

三类结论严格区分：
* 参数非法      -> HTTP 422, category=invalid_request
* 超限拒绝      -> HTTP 503, category=rejected
* 校验不通过    -> HTTP 200, status=completed, result.overall_valid=false
另外查询未命中 -> HTTP 404, category=not_found
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.errors import AppError, NotFoundError
from app.manager import TaskManager
from app.models import (
    SubmitRequest,
    SubmitResponse,
)

logger = logging.getLogger("app.validation")


def _error_payload(exc: AppError) -> dict:
    return {
        "error": exc.error_code,
        "category": exc.category,
        "message": exc.message,
        "details": exc.details,
    }


def _safe_validation_errors(exc: RequestValidationError) -> list[dict]:
    """裁剪 pydantic 错误，剔除 ctx 中不可序列化的原始异常对象。"""
    safe: list[dict] = []
    for err in exc.errors():
        safe.append({
            "loc": [str(part) for part in err.get("loc", [])],
            "type": err.get("type", ""),
            "msg": err.get("msg", ""),
        })
    return safe


def create_app(max_concurrency: int | None = None) -> FastAPI:
    """应用工厂，便于测试用较小并发上限构建独立应用。"""
    limit = max_concurrency if max_concurrency is not None else int(
        os.environ.get("VALIDATION_MAX_CONCURRENCY", "4")
    )
    global manager
    manager = TaskManager(max_concurrency=limit)

    app = FastAPI(title="dataset-validation-api", version="0.1.0")
    app.state.manager = manager

    @app.exception_handler(AppError)
    async def _handle_app_error(request, exc: AppError) -> JSONResponse:
        logger.warning(
            "准入/应用错误 key=%s category=%s code=%s details=%s",
            getattr(exc, "_key", "-"), exc.category, exc.error_code, exc.details,
        )
        return JSONResponse(status_code=exc.status_code, content=_error_payload(exc))

    @app.exception_handler(RequestValidationError)
    async def _handle_request_error(request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI/pydantic 信封校验失败 -> 统一归类为参数非法。
        logger.warning("参数非法 path=%s errors=%s", request.url.path, exc.errors())
        return JSONResponse(
            status_code=422,
            content={
                "error": "invalid_request",
                "category": "invalid_request",
                "message": "请求参数非法",
                "details": {"errors": _safe_validation_errors(exc)},
            },
        )

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "active": manager.active_count,
                "max_concurrency": manager.max_concurrency}

    @app.post("/api/v1/validations", response_model=SubmitResponse)
    def submit_validation(body: SubmitRequest) -> SubmitResponse:
        key = body.idempotency_key
        record_count = len(body.records)
        logger.info("收到提交 key=%s records=%d 输入=%s",
                    key, record_count, body.records)
        try:
            outcome = manager.submit(key, body.records)
        except AppError as exc:
            exc._key = key  # type: ignore[attr-defined]
            raise
        overall = (outcome.result.overall_valid if outcome.result else None)
        logger.info(
            "提交判定 key=%s status=%s overall_valid=%s",
            key, outcome.status.value, overall,
        )
        return SubmitResponse(idempotency_key=key, accepted=True, outcome=outcome)

    @app.get("/api/v1/validations/{idempotency_key}",
             response_model=SubmitResponse)
    def get_validation(idempotency_key: str) -> SubmitResponse:
        logger.info("收到查询 key=%s", idempotency_key)
        outcome = manager.get(idempotency_key)
        if outcome is None:
            logger.warning("查询未命中 key=%s", idempotency_key)
            raise NotFoundError(f"未找到幂等键 {idempotency_key} 的任务",
                                details={"idempotency_key": idempotency_key})
        overall = outcome.result.overall_valid if outcome.result else None
        logger.info("查询命中 key=%s status=%s overall_valid=%s",
                    idempotency_key, outcome.status.value, overall)
        return SubmitResponse(
            idempotency_key=idempotency_key, accepted=True, outcome=outcome,
        )

    return app


app = create_app()
