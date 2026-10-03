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
        threading.Thread(
            target=lambda: outcomes.append(mgr.submit("dup-key", records, "fp", gate))
        )
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


class FailGate:
    """执行到即抛异常的校验器，可稳定制造“执行失败”的并发窗口。"""

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
            raise TimeoutError("FailGate 未在超时时间内放行")
        raise RuntimeError("执行过程失败（模拟）")


def test_execution_failure_releases_capacity_and_is_cached():
    mgr = TaskManager(max_concurrency=1)
    records = [{"id": 1, "name": "a", "age": 30, "score": 0.5, "tags": ["x"]}]
    view = mgr.submit("fail", records, "fp", FailGate_with_release())

    assert view.failure is not None
    assert view.result is None
    assert view.failure.type == "RuntimeError"
    assert "执行过程失败" in view.failure.message
    # 失败后容量回到基线，不泄漏
    cap = mgr.capacity()
    assert (cap.inflight, cap.available, cap.max_concurrency) == (0, 1, 1)

    # 同 key 同载荷再提交：复用同一份失败结论，不重复执行
    again = mgr.submit("fail", records, "fp", FailGate_with_release())
    assert again.cached is True
    assert again.failure == view.failure


def FailGate_with_release():
    gate = FailGate()
    gate.release.set()
    return gate


def test_concurrent_same_key_converges_to_single_failure_conclusion():
    mgr = TaskManager(max_concurrency=2)
    gate = FailGate()
    records = [{"id": 1, "name": "a", "age": 30, "score": 0.5, "tags": ["x"]}]
    outcomes: list = []

    def worker() -> None:
        outcomes.append(mgr.submit("fail-conv", records, "fp", gate))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    assert gate.started.wait(timeout=2), "执行未启动"
    gate.release.set()
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive()

    # 只真正执行一次；8 个并发提交收敛到同一份失败结论
    assert gate.calls == 1
    assert len(outcomes) == 8
    assert all(o.failure is not None and o.result is None for o in outcomes)
    assert all(o.failure == outcomes[0].failure for o in outcomes)
    assert sum(1 for o in outcomes if o.cached is False) == 1
    assert sum(1 for o in outcomes if o.cached is True) == 7
    # 等待者没有永久卡住：全部已返回，且容量回基线
    assert mgr.capacity().inflight == 0


def test_capacity_snapshot_reflects_inflight_and_rejected_leaves_no_trace():
    mgr = TaskManager(max_concurrency=1)
    gate = Gate()
    records = [{"id": 1, "name": "a", "age": 30, "score": 0.5, "tags": ["x"]}]
    t = threading.Thread(target=mgr.submit, args=("occupy", records, "fp1", gate))
    t.start()
    assert gate.started.wait(timeout=2)

    cap = mgr.capacity()
    assert (cap.inflight, cap.available, cap.max_concurrency) == (1, 0, 1)

    with pytest.raises(CapacityFull):
        mgr.submit("rejected", records, "fp2", gate)
    # 被拒绝请求不留占位：查询不到该 key，容量不变
    assert mgr.get("rejected") is None
    assert mgr.capacity().inflight == 1

    gate.release.set()
    t.join(timeout=5)
    assert mgr.capacity().inflight == 0
