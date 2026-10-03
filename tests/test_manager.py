"""并发重复提交只执行一次；超限立即拒绝；拒绝后可重试；执行失败可收敛。"""
from __future__ import annotations

import threading

import pytest

from app.manager import CapacityFull, IdempotencyConflict, TaskManager
from app.models import Conclusion
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


class FailingGate(Gate):
    """放行后抛出执行异常的校验器，模拟执行过程失败。"""

    def __call__(self, records):
        with self.lock:
            self.calls += 1
        self.started.set()
        self.release.wait(timeout=5)
        raise RuntimeError("执行依赖不可用（模拟）")


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


def test_execution_failure_releases_capacity_and_is_cached():
    mgr = TaskManager(max_concurrency=1)
    gate = FailingGate()
    records = [{"id": 1, "name": "a", "age": 30, "score": 0.5, "tags": ["x"]}]

    t = threading.Thread(target=mgr.submit, args=("fail", records, "fp", gate))
    t.start()
    assert gate.started.wait(timeout=2)
    # 执行中：槽位被占用
    assert mgr.capacity().inflight == 1
    gate.release.set()
    t.join(timeout=5)

    # 结束后容量必须回到基线，没有泄漏
    snap = mgr.capacity()
    assert (snap.inflight, snap.max_concurrency, snap.available) == (0, 1, 1)

    # 首个提交拿到明确的执行失败结论
    view = mgr.get("fail")
    assert view is not None
    assert view.status.value == "COMPLETED"
    assert view.conclusion is Conclusion.EXECUTION_FAILED
    assert view.execution_error is not None
    assert view.execution_error.type == "RuntimeError"
    assert "不可用" in view.execution_error.message

    # 同标识同载荷再次提交：复用同一份失败结论，不重复执行
    again = mgr.submit("fail", records, "fp", gate)
    assert again.cached is True
    assert again.conclusion is Conclusion.EXECUTION_FAILED
    assert again.execution_error.type == "RuntimeError"
    assert gate.calls == 1

    # 槽位已释放：另一个 key 可正常受理，且不再被失败任务挡住
    ok = mgr.submit("other", records, "fp2", validate_dataset)
    assert ok.conclusion is Conclusion.VALIDATION_PASSED
    assert mgr.capacity().inflight == 0


def test_concurrent_same_key_submissions_converge_on_failure(caplog):
    mgr = TaskManager(max_concurrency=2)
    gate = FailingGate()
    records = [{"id": 1, "name": "a", "age": 30, "score": 0.5, "tags": ["x"]}]
    outcomes: list = []
    threads = [
        threading.Thread(target=lambda: outcomes.append(
            mgr.submit("fail-dup", records, "fp", gate)))
        for _ in range(6)
    ]
    for t in threads:
        t.start()
    assert gate.started.wait(timeout=2)
    gate.release.set()
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive(), "等待者被永久卡住"

    # 只真正执行一次；全部等待者收敛到同一份失败结论
    assert gate.calls == 1
    assert len(outcomes) == 6
    assert all(o.conclusion is Conclusion.EXECUTION_FAILED for o in outcomes)
    assert sum(1 for o in outcomes if o.cached is False) == 1
    assert sum(1 for o in outcomes if o.cached is True) == 5
    assert len({(o.execution_error.type, o.execution_error.message)
                for o in outcomes}) == 1
    # 容量收敛回基线，没有变成事实排队
    assert mgr.capacity().inflight == 0

    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "执行失败" in joined and "fail-dup" in joined


def test_failed_task_does_not_block_capacity_for_other_keys():
    mgr = TaskManager(max_concurrency=1)

    def boom(records):
        raise ValueError("boom")

    mgr.submit("dead", [], "fp", boom)
    assert mgr.capacity().inflight == 0
    records = [{"id": 1, "name": "a", "age": 30, "score": 0.5, "tags": ["x"]}]
    view = mgr.submit("alive", records, "fp2", validate_dataset)
    assert view.conclusion is Conclusion.VALIDATION_PASSED

