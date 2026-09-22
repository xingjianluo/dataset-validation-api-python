"""任务管理器单测：幂等只执行一次 + 非阻塞准入控制。

日志会打印每次提交的输入键、执行器真实调用序列与最终判定（命中/拒绝），
作为“只真正执行一次”的可核验依据。
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.errors import AdmissionRejectedError, ValidationRequestError
from app.manager import TaskManager, default_executor
from app.models import RejectReason

logger = logging.getLogger("tests.manager")


class BlockingExecutor:
    """记录真实执行次数，并用事件阻塞，用于制造确定的并发窗口。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.release = threading.Event()
        self._lock = threading.Lock()

    def __call__(self, key: str, records: list):
        with self._lock:
            self.calls.append(key)
        # 在并发窗口内阻塞，直到测试放行。
        self.release.wait(timeout=5)
        return default_executor(key, records)


def test_sequential_duplicate_submit_executes_once():
    manager = TaskManager(max_concurrency=4)
    records = [{"id": 1, "name": "a", "age": 1, "email": "a@b.co", "score": 1}]
    logger.info("用例 sequential_duplicate 输入 key=dup records=%s", records)
    first = manager.submit("dup", records)
    second = manager.submit("dup", [{"different": True}])
    logger.info("判定依据 first_is_second=%s status=%s", first is second, first.status.value)
    assert first is second  # 同一对象：第二次没有真正执行
    assert first.result.overall_valid is True


def test_concurrent_same_key_executes_exactly_once():
    manager = TaskManager(max_concurrency=4)
    executor = BlockingExecutor()
    manager._executor = executor

    n = 16
    logger.info("用例 concurrent_same_key 输入 key=hot 并发线程数=%d", n)
    outcomes: list = []
    barrier = threading.Barrier(n)

    def worker():
        barrier.wait()  # 让所有线程尽量同时进入 submit
        outcomes.append(manager.submit("hot", []))

    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = [pool.submit(worker) for _ in range(n)]
        # 等首个线程进入执行器后再放行，确保其余线程在并发窗口内同键提交。
        executor.release.set()
        for f in futures:
            f.result(timeout=5)

    logger.info("判定依据 执行器真实调用=%s (期望仅 ['hot'])", executor.calls)
    assert executor.calls == ["hot"], "同一幂等键只能真正执行一次"
    assert len(outcomes) == n
    assert all(outcome is outcomes[0] for outcome in outcomes), "并发结论必须一致"
    assert manager.active_count == 0


def test_over_limit_new_keys_are_rejected_without_queueing():
    manager = TaskManager(max_concurrency=2)
    executor = BlockingExecutor()
    manager._executor = executor

    def occupy(key: str):
        manager.submit(key, [])

    t1 = threading.Thread(target=occupy, args=("a",))
    t2 = threading.Thread(target=occupy, args=("b",))
    t1.start(); t2.start()
    # 等待两个名额被占满。
    while manager.active_count < 2:
        pass
    logger.info("用例 over_limit 输入 key=over, 当前 active=%d, 上限=%d",
                manager.active_count, manager.max_concurrency)

    with pytest.raises(AdmissionRejectedError) as exc_info:
        manager.submit("over", [])
    err = exc_info.value
    logger.info("判定依据 category=%s code=%s details=%s",
                err.category, err.error_code, err.details)
    assert err.status_code == 503
    assert err.category == "rejected"
    assert err.details["reason"] == RejectReason.CONCURRENCY_LIMIT.value
    assert err.details["active"] == 2 and err.details["max_concurrency"] == 2

    executor.release.set()
    t1.join(); t2.join()
    # 被拒绝者绝不占名额：执行完成后计数归零。
    assert manager.active_count == 0
    assert "over" not in [k for k in ("a", "b")]  # over 从未执行
    assert sorted(executor.calls) == ["a", "b"]


def test_same_key_concurrent_during_run_is_admitted_and_reuses():
    """同键并发提交不算新增负载：等待首个执行完成后复用，不受超限影响。"""
    manager = TaskManager(max_concurrency=1)
    executor = BlockingExecutor()
    manager._executor = executor

    result_box: list = []

    def first():
        result_box.append(manager.submit("only", []))

    def duplicate():
        # 等首个任务占满唯一名额后再提交同键。
        while manager.active_count < 1:
            pass
        result_box.append(manager.submit("only", []))  # 不应被拒绝

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=duplicate)
    t1.start(); t2.start()
    executor.release.set()
    t1.join(timeout=5); t2.join(timeout=5)

    logger.info("用例 same_key_under_full_capacity 执行器真实调用=%s", executor.calls)
    assert executor.calls == ["only"]
    assert len(result_box) == 2 and result_box[0] is result_box[1]


def test_error_categories_are_distinguishable():
    manager = TaskManager(max_concurrency=1)

    class BoomExecutor:
        def __call__(self, key, records):
            raise RuntimeError("boom")

    manager._executor = BoomExecutor()
    logger.info("用例 error_categories：构造拒绝(rejected)与参数非法(invalid_request)")
    # 503 rejected
    executor = BlockingExecutor()
    manager._executor = executor
    t = threading.Thread(target=lambda: manager.submit("z", []))
    t.start()
    while manager.active_count < 1:
        pass
    with pytest.raises(AdmissionRejectedError) as ei:
        manager.submit("z2", [])
    rejected = ei.value
    executor.release.set(); t.join()

    # 422 invalid_request（管理器自身对并发上限参数的防御性校验）
    with pytest.raises(ValueError):
        TaskManager(max_concurrency=0)

    logger.info("判定依据 rejected.category=%s status=%s；"
                "ValueError 与 AdmissionRejectedError 类型不同",
                rejected.category, rejected.status_code)
    assert rejected.category == "rejected"
    assert isinstance(rejected, AdmissionRejectedError)
    assert not isinstance(rejected, ValidationRequestError)
