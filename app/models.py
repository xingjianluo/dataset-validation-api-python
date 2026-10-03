"""请求/响应与领域模型定义（仅类型，无业务逻辑）。"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class ErrorCode(str, Enum):
    """四类不可混淆的结论 + 请求/幂等冲突分类。"""

    INVALID_REQUEST = "INVALID_REQUEST"  # 参数非法（未进入执行）
    CAPACITY_REJECTED = "CAPACITY_REJECTED"  # 超出并发上限，准入被拒绝
    VALIDATION_FAILED = "VALIDATION_FAILED"  # 已执行，数据校验不通过
    EXECUTION_FAILED = "EXECUTION_FAILED"  # 已执行，执行过程本身失败
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"  # 同 key 不同载荷
    NOT_FOUND = "NOT_FOUND"  # 查询的幂等标识不存在


class ValidationStatus(str, Enum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"


class Issue(BaseModel):
    """单条记录命中的一个问题：可定位到字段/数组位置，并给出原因。"""

    field: str | None = None
    reason: str


class RecordResult(BaseModel):
    index: int
    valid: bool
    # 兼容老调用方：field/reason 仍为“首个命中问题”（issues 为空时为 None）
    field: str | None = None
    reason: str | None = None
    # 新增：该记录命中的全部问题，顺序固定、与提交顺序无关
    issues: list[Issue] = Field(default_factory=list)


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
    # 新增：执行失败结论（与数据不通过互斥，老调用方可忽略该字段）
    failure: ExecutionFailure | None = None


class SubmitResponse(BaseModel):
    idempotency_key: str
    status: ValidationStatus
    result: ValidationResult | None = None
    failure: ExecutionFailure | None = None
    cached: bool = False


class ExecutionFailure(BaseModel):
    """执行过程本身失败的结论：任务确实执行过，但没能产出校验结果。"""

    type: str
    message: str


class CapacityView(BaseModel):
    """当前容量占用快照，用于本地观察是否存在槽位泄漏。"""

    inflight: int
    max_concurrency: int
    available: int


TaskView.model_rebuild()
SubmitResponse.model_rebuild()
