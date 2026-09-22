"""任务管理：幂等去重（同一幂等键只真正执行一次）与并发准入控制。

并发模型（线程安全）：

* 每个幂等键一把串行锁：首个拿到锁的提交者负责真正执行，
  并发到达的同键提交者在锁上短暂等待，随后直接复用已落库的结论，
  因此同键永远不会执行两次，也不会产生两份互相矛盾的结论。
* 全局活动计数器做*非阻塞*准入：执行前若活动任务数已达上限，
  立即抛出 :class:`AdmissionRejectedError`，不排队、不静默等待。
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from app.errors import AdmissionRejectedError
from app.models import RejectReason, TaskOutcome, TaskStatus
from app.validator import validate_records


def default_executor(idempotency_key: str, records: list) -> TaskOutcome:
    """默认执行器：本地逐条校验，全部数据与状态均在本地产生。"""
    data = validate_records(records)
    return TaskOutcome(
        idempotency_key=idempotency_key,
        status=TaskStatus.COMPLETED,
        result=data,
    )


class TaskManager:
    """以幂等键缓存最终结论，并限制真正并发执行的任务数。"""

    def __init__(
        self,
        max_concurrency: int = 4,
        executor: Callable[[str, list], TaskOutcome] | None = None,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency 必须 >= 1")
        self.max_concurrency = max_concurrency
        self._executor = executor or default_executor

        self._guard = threading.Lock()        # 保护下面三个共享状态
        self._outcomes: dict[str, TaskOutcome] = {}
        self._key_locks: dict[str, threading.Lock] = {}
        self._active = 0                       # 真正在执行的任务数

    @property
    def active_count(self) -> int:
        with self._guard:
            return self._active

    def _get_key_lock(self, key: str) -> threading.Lock:
        with self._guard:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._key_locks[key] = lock
            return lock

    def submit(self, idempotency_key: str, records: list) -> TaskOutcome:
        """提交任务。

        * 已完成的同键任务：直接返回首次结论（不占并发名额）。
        * 正在执行的同键任务：等待其完成后复用结论。
        * 全新键：先做准入判断，超上限立即拒绝；否则执行一次并固化结论。
        """
        key_lock = self._get_key_lock(idempotency_key)

        # 快速路径：无竞争时直接读结论。
        with self._guard:
            existing = self._outcomes.get(idempotency_key)
        if existing is not None:
            return existing

        with key_lock:
            # 双检：拿到锁后可能首个提交者已写完结论。
            with self._guard:
                existing = self._outcomes.get(idempotency_key)
            if existing is not None:
                return existing

            # 非阻塞准入：达上限立即拒绝，绝不排队等待名额。
            admitted = False
            with self._guard:
                if self._active >= self.max_concurrency:
                    active = self._active
                else:
                    self._active += 1
                    admitted = True
            if not admitted:
                raise AdmissionRejectedError(
                    "已达并发执行上限，提交被准入控制明确拒绝（不排队）",
                    details={
                        "reason": RejectReason.CONCURRENCY_LIMIT.value,
                        "max_concurrency": self.max_concurrency,
                        "active": active,
                    },
                )

            try:
                outcome = self._executor(idempotency_key, records)
            finally:
                with self._guard:
                    self._active -= 1

            with self._guard:
                # 结论固化后对所有同键提交者可见且不可变。
                self._outcomes[idempotency_key] = outcome
            return outcome

    def get(self, idempotency_key: str) -> TaskOutcome | None:
        """按幂等键查询既有结论；不存在返回 None。"""
        with self._guard:
            return self._outcomes.get(idempotency_key)
