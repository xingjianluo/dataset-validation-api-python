"""请求/响应与领域模型定义（仅类型，无业务逻辑）。"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class ErrorCode(str, Enum):
    """三类不可混淆的结论 + 请求/幂等冲突分类。"""

    INVALID_REQUEST = "INVALID_REQUEST"        # 参数非法（未进入执行）
    CAPACITY_REJECTED = "CAPACITY_REJECTED"    # 超出并发上限，准入被拒绝
    VALIDATION_FAILED = "VALIDATION_FAILED"    # 已执行，数据校验不通过
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"  # 同 key 不同载荷
    NOT_FOUND = "NOT_FOUND"                    # 查询的幂等标识不存在


class ValidationStatus(str, Enum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"


class RecordResult(BaseModel):
    index: int
    valid: bool
    field: str | None = None
    reason: str | None = None


class ValidationResult(BaseModel):
    total: int = 0
    passed: int = 0
    failed: int = 0
    all_passed: bool = False
    records: list[RecordResult] = Field(default_factory=list)


class TaskView(BaseModel):
    idempotency_key: str
    status: ValidationStatus
    result: ValidationResult | None = None


class SubmitResponse(BaseModel):
    idempotency_key: str
    status: ValidationStatus
    result: ValidationResult | None = None
    cached: bool = False
