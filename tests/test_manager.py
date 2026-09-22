"""并发重复提交只执行一次；超限立即拒绝；拒绝后可重试。"""
from __future__ import annotations

import threading

import pytest

from app.manager import CapacityFull, IdempotencyConflict, TaskManager
from app.validation import validate_dataset


class Gate:
    """让校验器在执行中阻塞，直到测试放行，用于稳定制造并发窗口。"""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.lock = threading.Lock()

    def __call__(self, records):
        with self.lock:
            self.calls += 1
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("Gate 未在超时时间内放行")
        return validate_dataset(records)


def test_concurrent_duplicate_submit_executes_once():
    mgr = TaskManager(max_concurrency=2)
    gate = Gate()
    records = [{"id": 1, "name": "a", "age": 30, "score": 0.5, "tags": ["x"]}]
    outcomes: list = []
    threads = [
        threading.Thread(target=lambda: outcomes.append(
            mgr.submit("dup-key", records, "fp", gate)))
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    assert gate.started.wait(timeout=2), "首个执行未启动"
    # 此时所有重复提交都已在等待；给点时间确保它们进入等待态
    assert gate.release.wait(timeout=0.2) is False or True
    gate.release.set()
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive()

    assert gate.calls == 1, "同一幂等标识被真正执行了多次"
    assert len(outcomes) == 8
    assert all(o.status.value == "COMPLETED" for o in outcomes)
    assert all(o.result.all_passed is True for o in outcomes)
    # 仅首次是执行者，其余全部命中同一份缓存结论
    assert sum(1 for o in outcomes if o.cached is False) == 1
    assert sum(1 for o in outcomes if o.cached is True) == 7


def test_capacity_rejects_immediately_without_queueing():
    mgr = TaskManager(max_concurrency=1)
    gate = Gate()
    records = [{"id": 1, "name": "a", "age": 30, "score": 0.5, "tags": ["x"]}]

    first = threading.Thread(target=mgr.submit, args=("occupy", records, "fp1", gate))
    first.start()
    assert gate.started.wait(timeout=2)

    # 槽位已满：必须立即抛 CapacityFull，而不是阻塞排队
    with pytest.raises(CapacityFull):
        mgr.submit("other-key", records, "fp2", gate)
    assert gate.calls == 1  # 被拒绝的请求没有执行校验

    # 被拒绝不占缓存：首个任务完成后，同 key 重试应被受理
    gate.release.set()
    first.join(timeout=5)
    view = mgr.submit("other-key", records, "fp2", validate_dataset)
    assert view.cached is False
    assert view.result.all_passed is True


def test_running_status_visible_and_then_completed():
    mgr = TaskManager(max_concurrency=1)
    gate = Gate()
    records = [{"id": 1, "name": "a", "age": 30, "score": 0.5, "tags": ["x"]}]
    t = threading.Thread(target=mgr.submit, args=("run", records, "fp", gate))
    t.start()
    gate.started.wait(timeout=2)
    view = mgr.get("run")
    assert view is not None and view.status.value == "RUNNING"
    gate.release.set()
    t.join(timeout=5)
    view = mgr.get("run")
    assert view.status.value == "COMPLETED" and view.result is not None
    assert mgr.get("missing") is None


def test_same_key_different_payload_conflicts_after_completion():
    mgr = TaskManager(max_concurrency=2)
    records = [{"id": 1, "name": "a", "age": 30, "score": 0.5, "tags": ["x"]}]
    mgr.submit("k", records, "fp-a", validate_dataset)
    with pytest.raises(IdempotencyConflict):
        mgr.submit("k", records, "fp-different", validate_dataset)
