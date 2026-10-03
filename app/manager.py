"""幂等提交 + 并发准入控制。

并发模型（线程安全，纯本地，无外部依赖）：
- 每个 idempotency_key 只真正执行一次校验：首个提交者执行，
  并发的同 key 提交在键事件上等待执行完成，随后读取同一结论。
- 准入只统计“正在执行”的任务数。容量已满时立即拒绝，不排队、不等待。
- 同 key 重复提交但载荷指纹不同 -> 幂等冲突，明确区分。
- 被容量拒绝不写入任何缓存，调用方可在有空位时原样重试。

终态结论有三种，均只产生一次、被同 key 同载荷的后续提交（含并发等待者）
复用，绝不重复执行、绝不产生互相矛盾的结论：
- VALIDATION_PASSED / VALIDATION_FAILED：校验器正常返回的数据结论；
- EXECUTION_FAILED：执行过程本身抛异常。失败结论同样被缓存，且执行结束
  （无论成功失败）后并发槽位一定释放，等待者一定被唤醒并收敛到同一份结论。
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from app.models import (
    CapacitySnapshot,
    Conclusion,
    ExecutionError,
    SubmitResponse,
    TaskView,
    ValidationResult,
    ValidationStatus,
)

logger = logging.getLogger("dataset.manager")


@dataclass
class _Entry:
    fingerprint: str
    event: threading.Event = field(default_factory=threading.Event)
    result: ValidationResult | None = None
    execution_error: ExecutionError | None = None


class CapacityFull(Exception):
    """并发执行数已达上限，准入被明确拒绝。"""


class IdempotencyConflict(Exception):
    """同一幂等标识携带了与首次不同的载荷。"""


def _conclusion_of(entry: _Entry) -> Conclusion | None:
    """根据缓存条目推导终态结论；未结束返回 None。"""
    if entry.execution_error is not None:
        return Conclusion.EXECUTION_FAILED
    if entry.result is not None:
        return (
            Conclusion.VALIDATION_PASSED
            if entry.result.all_passed
            else Conclusion.VALIDATION_FAILED
        )
    return None


def _response(key: str, entry: _Entry, cached: bool) -> SubmitResponse:
    return SubmitResponse(
        idempotency_key=key,
        status=ValidationStatus.COMPLETED,
        result=entry.result,
        cached=cached,
        conclusion=_conclusion_of(entry),
        execution_error=entry.execution_error,
    )


class TaskManager:
    def __init__(self, max_concurrency: int = 2) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency 必须 >= 1")
        self._max_concurrency = max_concurrency
        self._inflight = 0
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}

    @property
    def max_concurrency(self) -> int:
        return self._max_concurrency

    def capacity(self) -> CapacitySnapshot:
        """当前容量占用快照：正在执行数 / 上限 / 剩余槽位。"""
        with self._lock:
            return CapacitySnapshot(
                inflight=self._inflight,
                max_concurrency=self._max_concurrency,
                available=self._max_concurrency - self._inflight,
            )

    def submit(
        self,
        idempotency_key: str,
        records: list,
        payload_fingerprint: str,
        validator: Callable[[list], ValidationResult],
    ) -> SubmitResponse:
        # 阶段一：在锁内判定角色（执行者 / 等待者 / 拒绝 / 冲突），
        # 锁内绝不执行校验，避免阻塞其他 key 的准入判定。
        with self._lock:
            entry = self._entries.get(idempotency_key)
            if entry is not None:
                if entry.fingerprint != payload_fingerprint:
                    raise IdempotencyConflict(idempotency_key)
                if entry.event.is_set():
                    # 已有终态结论（通过 / 不通过 / 执行失败）-> 直接复用
                    return _response(idempotency_key, entry, cached=True)
                # 已有同载荷任务正在执行 -> 等待同一结论
                waiter = entry
                run = False
            elif self._inflight >= self._max_concurrency:
                raise CapacityFull(idempotency_key)
            else:
                entry = _Entry(fingerprint=payload_fingerprint)
                self._entries[idempotency_key] = entry
                self._inflight += 1
                waiter = entry
                run = True

        if not run:
            waiter.event.wait()
            return _response(idempotency_key, waiter, cached=True)

        # 阶段二：锁外执行唯一一次真正校验。
        # 成功与失败都落缓存；槽位释放与等待者唤醒放在 finally，
        # 保证任何异常路径下容量都能回到基线、等待者都能及时收敛。
        try:
            result = validator(records)
            entry.result = result
        except Exception as exc:  # noqa: BLE001  # 执行失败需兜住任意异常并化为终态结论
            entry.execution_error = ExecutionError(
                type=type(exc).__name__, message=str(exc),
            )
            logger.warning(
                "执行失败: idempotency_key=%s fingerprint=%s records=%r "
                "error_type=%s error=%s",
                idempotency_key, payload_fingerprint[:12], records,
                type(exc).__name__, exc,
            )
        finally:
            with self._lock:
                self._inflight -= 1
                entry.event.set()

        return _response(idempotency_key, entry, cached=False)

    def get(self, idempotency_key: str) -> TaskView | None:
        with self._lock:
            entry = self._entries.get(idempotency_key)
            if entry is None:
                return None
            if not entry.event.is_set():
                return TaskView(
                    idempotency_key=idempotency_key,
                    status=ValidationStatus.RUNNING,
                )
            return TaskView(
                idempotency_key=idempotency_key,
                status=ValidationStatus.COMPLETED,
                result=entry.result,
                conclusion=_conclusion_of(entry),
                execution_error=entry.execution_error,
            )
