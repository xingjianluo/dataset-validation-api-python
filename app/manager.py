"""幂等提交 + 并发准入控制。

并发模型（线程安全，纯本地，无外部依赖）：
- 每个 idempotency_key 只真正执行一次校验：首个提交者执行，
  并发的同 key 提交在键事件上等待执行完成，随后读取同一结论。
- 准入只统计“正在执行”的任务数。容量已满时立即拒绝，不排队、不等待。
- 同 key 重复提交但载荷指纹不同 -> 幂等冲突，明确区分。
- 被容量拒绝不写入任何缓存，调用方可在有空位时原样重试。
- 校验函数自身抛异常属于“执行失败”：同样缓存为最终结论（failure），
  槽位照常释放、等待者照常收敛；同 key 同载荷再次提交直接复用，
  不会重复执行，也不会产生与首次矛盾的结论。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from app.models import (
    CapacityView,
    ExecutionFailure,
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
    failure: ExecutionFailure | None = None


class CapacityFull(Exception):
    """并发执行数已达上限，准入被明确拒绝。"""


class IdempotencyConflict(Exception):
    """同一幂等标识携带了与首次不同的载荷。"""


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
                    # 已有同载荷结论（通过/不通过/执行失败）-> 原样复用
                    return SubmitResponse(
                        idempotency_key=idempotency_key,
                        status=ValidationStatus.COMPLETED,
                        result=entry.result,
                        failure=entry.failure,
                        cached=True,
                    )
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
            return SubmitResponse(
                idempotency_key=idempotency_key,
                status=ValidationStatus.COMPLETED,
                result=waiter.result,
                failure=waiter.failure,
                cached=True,
            )

        # 阶段二：锁外执行唯一一次真正校验。
        # 无论正常返回还是执行失败，槽位都必须释放、结论都必须落缓存并唤醒等待者。
        failure: ExecutionFailure | None = None
        try:
            result = validator(records)
        except Exception as exc:  # noqa: BLE001  # 刻意兜底：任何执行异常都落成失败结论
            result = None
            failure = ExecutionFailure(type=type(exc).__name__, message=str(exc))
            logger.warning(
                "执行失败: idempotency_key=%s error_type=%s message=%s",
                idempotency_key,
                failure.type,
                failure.message,
            )
        finally:
            with self._lock:
                entry.result = result
                entry.failure = failure
                self._inflight -= 1
                entry.event.set()

        return SubmitResponse(
            idempotency_key=idempotency_key,
            status=ValidationStatus.COMPLETED,
            result=result,
            failure=failure,
            cached=False,
        )

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
                failure=entry.failure,
            )

    def capacity(self) -> CapacityView:
        """返回当前容量占用快照：在执行数 / 上限 / 可用数。"""
        with self._lock:
            inflight = self._inflight
        return CapacityView(
            inflight=inflight,
            max_concurrency=self._max_concurrency,
            available=self._max_concurrency - inflight,
        )
