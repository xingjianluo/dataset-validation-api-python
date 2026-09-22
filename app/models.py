"""请求/响应数据模型（仅类型定义，不含业务逻辑）。"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RecordErrorCode(str, Enum):
    """单条记录级别的校验问题分类。"""

    MISSING_FIELD = "missing_field"
    TYPE_MISMATCH = "type_mismatch"
    INVALID_VALUE = "invalid_value"
    UNKNOWN_FIELD = "unknown_field"


class RejectReason(str, Enum):
    """提交被准入控制拒绝的可区分原因。"""

    CONCURRENCY_LIMIT = "concurrency_limit"


class RecordIssue(BaseModel):
    """单条记录上的一个判定依据，可定位到字段。"""

    field: str
    code: RecordErrorCode
    message: str
    expected: str | None = None
    actual: str | None = None


class RecordResult(BaseModel):
    """单条记录的校验结论。"""

    index: int
    valid: bool
    issues: list[RecordIssue] = Field(default_factory=list)


class ValidationSummary(BaseModel):
    """整批数据的汇总信息（与提交顺序无关）。"""

    total: int = 0
    valid_count: int = 0
    invalid_count: int = 0
    issue_count: int = 0


class TaskStatus(str, Enum):
    """任务生命周期状态。"""

    COMPLETED = "completed"
    FAILED = "failed"


class ValidationResultData(BaseModel):
    """任务完成后随查询返回的完整结论。"""

    overall_valid: bool
    summary: ValidationSummary
    records: list[RecordResult]


class TaskOutcome(BaseModel):
    """幂等存储中为每个幂等键保存的最终结论（任务只真正执行一次）。"""

    idempotency_key: str
    status: TaskStatus
    result: ValidationResultData | None = None
    error: dict | None = None


class SubmitResponse(BaseModel):
    idempotency_key: str
    accepted: bool
    outcome: TaskOutcome | None = None


class ErrorResponse(BaseModel):
    """统一错误响应体：非法参数 / 超限拒绝 / 未命中 均通过该结构区分。"""

    error: str
    category: str
    message: str
    details: dict = Field(default_factory=dict)


class SubmitRequest(BaseModel):
    """提交校验任务的请求信封。"""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=128)
    records: list = Field(default_factory=list, max_length=1000)

    @field_validator("idempotency_key")
    @classmethod
    def _non_blank_key(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("idempotency_key 不能为空白字符串")
        return value
