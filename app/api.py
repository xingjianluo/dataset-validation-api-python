"""HTTP 接口层。

四类判定（HTTP 状态码 + 稳定 code/conclusion，绝不混用）：
- 400 INVALID_REQUEST        参数非法，任务未进入执行
- 503 CAPACITY_REJECTED      并发执行数已达上限，准入被立即拒绝（不排队）
- 409 IDEMPOTENCY_CONFLICT   同一幂等标识提交了不同载荷
- 404 NOT_FOUND              查询未知标识
- 200 conclusion=VALIDATION_PASSED  已执行，数据全部通过
- 200 conclusion=VALIDATION_FAILED  已执行，数据校验不通过
- 200 conclusion=EXECUTION_FAILED   已执行，执行过程本身失败（同标识同载荷
                                    再提交复用同一份失败结论，不重复执行）
"""
from __future__ import annotations

import hashlib
import json
import logging

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app.errors import error_response
from app.manager import CapacityFull, IdempotencyConflict, TaskManager
from app.models import ErrorCode
from app.validation import validate_dataset

logger = logging.getLogger("dataset.api")

MAX_CONCURRENCY = 2

app = FastAPI(title="Local Dataset Validation API", version="0.1.0")
manager = TaskManager(max_concurrency=MAX_CONCURRENCY)


def _fingerprint(records: list) -> str:
    """对记录内容做规范化哈希：键排序 + 无空白，保证同内容同指纹。"""
    canonical = json.dumps(records, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _invalid(message: str, details: dict | None = None) -> JSONResponse:
    return error_response(ErrorCode.INVALID_REQUEST, message, 400, details)


@app.post("/validations")
async def submit_validation(request: Request) -> JSONResponse:
    body = await request.body()
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _invalid("请求体必须是合法 JSON 对象")
    if not isinstance(payload, dict):
        return _invalid("请求体必须是 JSON 对象，包含 idempotency_key 与 records")

    key = payload.get("idempotency_key")
    if not isinstance(key, str) or not key.strip():
        return _invalid("idempotency_key 必须是非空字符串")
    key = key.strip()
    if len(key) > 200:
        return _invalid("idempotency_key 长度不得超过 200")

    if "records" not in payload:
        return _invalid("缺少 records 字段")
    records = payload["records"]
    if not isinstance(records, list):
        return _invalid("records 必须是数组")
    if len(records) > 1000:
        return _invalid("records 数量不得超过 1000")

    fingerprint = _fingerprint(records)
    logger.info(
        "提交: idempotency_key=%s record_count=%d fingerprint=%s records=%r",
        key, len(records), fingerprint[:12], records,
    )

    try:
        view = await run_in_threadpool(
            manager.submit, key, records, fingerprint, validate_dataset
        )
    except CapacityFull:
        logger.warning(
            "准入拒绝: idempotency_key=%s 原因=并发上限 %d 已满",
            key, manager.max_concurrency,
        )
        return error_response(
            ErrorCode.CAPACITY_REJECTED,
            f"并发执行数已达上限 {manager.max_concurrency}，请求被拒绝（不排队）",
            503,
            {"max_concurrency": manager.max_concurrency},
        )
    except IdempotencyConflict:
        logger.warning("幂等冲突: idempotency_key=%s", key)
        return error_response(
            ErrorCode.IDEMPOTENCY_CONFLICT,
            "同一 idempotency_key 已关联不同的 records 载荷",
            409,
            {"idempotency_key": key},
        )

    logger.info(
        "受理结论: idempotency_key=%s cached=%s conclusion=%s",
        key, view.cached,
        view.conclusion.value if view.conclusion is not None else None,
    )
    return JSONResponse(
        status_code=200,
        content=json.loads(view.model_dump_json()),
    )


@app.get("/validations/{key}")
def get_validation(key: str) -> JSONResponse:
    view = manager.get(key)
    if view is None:
        return error_response(
            ErrorCode.NOT_FOUND,
            "未找到该 idempotency_key 对应的校验任务",
            404,
            {"idempotency_key": key},
        )
    return JSONResponse(status_code=200, content=json.loads(view.model_dump_json()))


@app.get("/capacity")
def get_capacity() -> JSONResponse:
    """本地容量占用观测：正在执行多少、上限多少、还剩多少槽位。

    压测后可立即调用确认 inflight 已回到 0（占用未泄漏）。
    """
    snapshot = manager.capacity()
    logger.info("容量观测: %s", snapshot.model_dump())
    return JSONResponse(status_code=200, content=snapshot.model_dump())
