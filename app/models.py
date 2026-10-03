"""请求/响应与领域模型定义（仅类型，无业务逻辑）。"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class ErrorCode(str, Enum):
    """请求级错误分类（任务未产生终态结论）。"""

    INVALID_REQUEST = "INVALID_REQUEST"        # 参数非法（未进入执行）
    CAPACITY_REJECTED = "CAPACITY_REJECTED"    # 超出并发上限，准入被拒绝
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"  # 同 key 不同载荷
    NOT_FOUND = "NOT_FOUND"                    # 查询的幂等标识不存在


class Conclusion(str, Enum):
    """任务终态结论（任务已结束，与请求级错误严格区分）。"""

    VALIDATION_PASSED = "VALIDATION_PASSED"    # 已执行，数据全部通过
    VALIDATION_FAILED = "VALIDATION_FAILED"    # 已执行，数据校验不通过
    EXECUTION_FAILED = "EXECUTION_FAILED"      # 已执行，执行过程本身失败（异常）


class IssueCode(str, Enum):
    """单条记录内单个问题的稳定机器码。"""

    NOT_OBJECT = "NOT_OBJECT"                  # 记录不是 JSON 对象（记录级）
    UNKNOWN_FIELD = "UNKNOWN_FIELD"            # 出现不认识的字段
    MISSING_FIELD = "MISSING_FIELD"            # 必填字段缺失
    TYPE_MISMATCH = "TYPE_MISMATCH"            # 字段类型不对
    VALUE_INVALID = "VALUE_INVALID"            # 取值越界/非法
    INVALID_ELEMENT = "INVALID_ELEMENT"        # 数组元素有问题


class ValidationStatus(str, Enum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"


class Issue(BaseModel):
    """一条记录命中的单个问题。

    - ``location``：精确定位，与老版 ``RecordResult.field`` 同语义
      （字段名 ``"age"``、数组元素 ``"tags[0]"``、记录级问题为 None）。
    - ``field``：问题归属的字段名（数组元素问题归属 ``"tags"``）。
    - ``reason``：稳定的中文判定依据。
    """

    code: IssueCode
    location: str | None = None
    field: str | None = None
    reason: str


class RecordResult(BaseModel):
    index: int
    valid: bool
    # 老字段保留：等价于 issues[0] 的定位与依据，老调用方无需改动
    field: str | None = None
    reason: str | None = None
    # 新增：该记录命中的全部问题（固定顺序，有多少报多少）
    issues: list[Issue] = Field(default_factory=list)


class ValidationResult(BaseModel):
    total: int = 0
    passed: int = 0
    failed: int = 0
    all_passed: bool = False
    records: list[RecordResult] = Field(default_factory=list)


class ExecutionError(BaseModel):
    """执行失败结论的载荷：记录执行过程中发生的异常。"""

    type: str
    message: str


class CapacitySnapshot(BaseModel):
    """本地容量占用快照，用于压测后确认占用没有泄漏。"""

    inflight: int          # 正在执行的任务数
    max_concurrency: int   # 并发上限
    available: int         # 剩余可用槽位


class TaskView(BaseModel):
    idempotency_key: str
    status: ValidationStatus
    result: ValidationResult | None = None
    conclusion: Conclusion | None = None
    execution_error: ExecutionError | None = None


class SubmitResponse(BaseModel):
    idempotency_key: str
    status: ValidationStatus
    result: ValidationResult | None = None
    cached: bool = False
    conclusion: Conclusion | None = None
    execution_error: ExecutionError | None = None
