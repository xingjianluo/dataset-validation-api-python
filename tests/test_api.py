"""HTTP 层：三类结论稳定可区分；并发同 key 只执行一次；超限立即拒绝。"""

from __future__ import annotations

import logging
import threading
import time

from fastapi.testclient import TestClient

from app import api as api_module
from app import validation as validation_module
from app.api import app

GOOD = {"id": 1, "name": "alice", "age": 30, "score": 0.9, "tags": ["a"]}
BAD_AGE = {"id": 2, "name": "bob", "age": 9, "score": 0.5, "tags": ["x"]}
MISSING = {"id": 3, "name": "c", "score": 0.5, "tags": ["x"]}  # 缺 age


class BlockingValidator:
    """可阻塞的校验器：执行到即设置 started，放行前一直占着并发槽位。"""

    def __init__(self, monkeypatch) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self._lock = threading.Lock()

        def blocked(records):
            with self._lock:
                self.calls += 1
            self.started.set()
            assert self.release.wait(timeout=5)
            return validation_module.validate_dataset(records)

        monkeypatch.setattr(api_module, "validate_dataset", blocked)

    def open(self) -> None:
        self.release.set()


def _client() -> TestClient:
    return TestClient(app)


def test_repeat_submit_same_key_returns_identical_result_and_executes_once(monkeypatch):
    gate = BlockingValidator(monkeypatch)
    results: list = []
    errors: list = []

    def worker() -> None:
        c = _client()
        r = c.post("/validations", json={"idempotency_key": "k-dup", "records": [GOOD]})
        (results if r.status_code == 200 else errors).append(r)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    assert gate.started.wait(timeout=2)
    gate.open()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive()

    assert errors == []
    assert gate.calls == 1, "同一幂等标识在 HTTP 并发下执行了多次"
    assert len(results) == 6
    bodies = [r.json() for r in results]
    assert all(b["conclusion"] == "VALIDATION_PASSED" for b in bodies)
    assert sum(1 for b in bodies if b["cached"] is False) == 1
    assert sum(1 for b in bodies if b["cached"] is True) == 5
    # 所有并发结论完全一致
    first = bodies[0]
    assert all(b["result"] == first["result"] for b in bodies)

    # 查询接口返回同一结论
    got = _client().get("/validations/k-dup")
    assert got.status_code == 200
    assert got.json()["result"] == first["result"]


def test_capacity_rejected_is_distinct_from_invalid_and_failed(monkeypatch):
    gate = BlockingValidator(monkeypatch)
    holder = threading.Thread(
        target=lambda: _client().post(
            "/validations", json={"idempotency_key": "holder", "records": [GOOD]}
        )
    )
    holder.start()
    assert gate.started.wait(timeout=2)

    # 两个不同 key 占满全部 2 个并发槽位
    extra = threading.Thread(
        target=lambda: _client().post(
            "/validations", json={"idempotency_key": "holder2", "records": [GOOD]}
        )
    )
    extra.start()
    deadline = time.time() + 2
    while time.time() < deadline and api_module.manager._inflight < 2:
        time.sleep(0.01)
    assert api_module.manager._inflight == 2

    rejected = _client().post(
        "/validations", json={"idempotency_key": "overflow", "records": [GOOD]}
    )
    assert rejected.status_code == 503
    assert rejected.json()["error"]["code"] == "CAPACITY_REJECTED"
    assert rejected.json()["error"]["details"]["max_concurrency"] == 2

    # 参数非法必须与超限区分
    invalid = _client().post("/validations", json={"idempotency_key": "ok"})
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "INVALID_REQUEST"
    bad_json = _client().post(
        "/validations",
        content=b"{not-json",
        headers={"content-type": "application/json"},
    )
    assert bad_json.status_code == 400
    assert bad_json.json()["error"]["code"] == "INVALID_REQUEST"

    gate.open()
    holder.join(timeout=10)
    extra.join(timeout=10)

    # 校验不通过必须与前两类区分：200 已执行，但 conclusion=FAILED
    failed = _client().post(
        "/validations", json={"idempotency_key": "bad-data", "records": [BAD_AGE]}
    )
    assert failed.status_code == 200
    body = failed.json()
    assert body["conclusion"] == "VALIDATION_FAILED"
    assert body["result"]["failed"] == 1


def test_partial_failure_keeps_full_batch_and_is_order_independent():
    c = _client()
    r1 = c.post(
        "/validations",
        json={"idempotency_key": "batch", "records": [GOOD, BAD_AGE, MISSING]},
    )
    r2 = c.post(
        "/validations",
        json={"idempotency_key": "batch2", "records": [MISSING, GOOD, BAD_AGE]},
    )
    for r in (r1, r2):
        assert r.status_code == 200
        b = r.json()
        assert b["conclusion"] == "VALIDATION_FAILED"
        assert b["result"]["total"] == 3
        assert b["result"]["passed"] == 1
        assert b["result"]["failed"] == 2
        assert len(b["result"]["records"]) == 3  # 部分失败不丢整批
    recs1 = r1.json()["result"]["records"]
    recs2 = r2.json()["result"]["records"]
    # 失败原因集合与提交顺序无关（两条失败记录都定位在 age，但依据不同）
    fail_reasons1 = sorted(x["reason"] for x in recs1 if not x["valid"])
    fail_reasons2 = sorted(x["reason"] for x in recs2 if not x["valid"])
    assert (
        fail_reasons1
        == fail_reasons2
        == ["取值非法：age 必须在 [18, 120] 范围内", "字段缺失"]
    )
    assert all(x["field"] == "age" for x in recs1 + recs2 if not x["valid"])


def test_conflict_and_not_found_codes_are_stable():
    c = _client()
    c.post("/validations", json={"idempotency_key": "k", "records": [GOOD]})
    conflict = c.post(
        "/validations", json={"idempotency_key": "k", "records": [BAD_AGE]}
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    missing = c.get("/validations/unknown")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOT_FOUND"


class FailingValidator:
    """执行到即阻塞，放行后抛异常的校验器：制造 HTTP 层“执行失败”并发窗口。"""

    def __init__(self, monkeypatch) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self._lock = threading.Lock()

        def failing(records):
            with self._lock:
                self.calls += 1
            self.started.set()
            if not self.release.wait(timeout=5):
                raise TimeoutError("FailingValidator 未放行")
            raise RuntimeError("执行过程失败（模拟）")

        monkeypatch.setattr(api_module, "validate_dataset", failing)

    def open(self) -> None:
        self.release.set()


def test_execution_failure_is_distinct_and_its_capacity_recovers(monkeypatch, caplog):
    def always_fail(records):
        raise RuntimeError("执行过程失败（模拟）")

    monkeypatch.setattr(api_module, "validate_dataset", always_fail)

    with caplog.at_level(logging.WARNING, logger="dataset.manager"):
        r = _client().post(
            "/validations", json={"idempotency_key": "exec-fail", "records": [GOOD]}
        )
    assert r.status_code == 200
    body = r.json()
    # 与 VALIDATION_FAILED / INVALID_REQUEST / CAPACITY_REJECTED 明确区分
    assert body["conclusion"] == "EXECUTION_FAILED"
    assert body["failure"] == {
        "type": "RuntimeError",
        "message": "执行过程失败（模拟）",
    }
    assert body["result"] is None
    # 失败信息进日志
    assert "执行失败" in caplog.text and "RuntimeError" in caplog.text

    # 查询接口可查到执行失败结论
    got = _client().get("/validations/exec-fail")
    assert got.status_code == 200
    gb = got.json()
    assert gb["conclusion"] == "EXECUTION_FAILED"
    assert gb["failure"] == body["failure"]

    # 容量回到基线
    cap = _client().get("/capacity")
    assert cap.status_code == 200
    assert cap.json() == {"inflight": 0, "max_concurrency": 2, "available": 2}


def test_execution_failure_is_idempotent_and_concurrent_submissions_converge(
    monkeypatch,
):
    gate = FailingValidator(monkeypatch)
    outcomes: list = []

    def worker() -> None:
        c = _client()
        r = c.post(
            "/validations", json={"idempotency_key": "fail-dup", "records": [GOOD]}
        )
        outcomes.append(r)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    assert gate.started.wait(timeout=2)
    gate.open()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive()

    # 只执行一次，6 份响应收敛到同一份失败结论
    assert gate.calls == 1
    assert len(outcomes) == 6
    assert all(r.status_code == 200 for r in outcomes)
    bodies = [r.json() for r in outcomes]
    assert all(b["conclusion"] == "EXECUTION_FAILED" for b in bodies)
    assert all(b["failure"] == bodies[0]["failure"] for b in bodies)
    assert sum(1 for b in bodies if b["cached"] is False) == 1
    assert sum(1 for b in bodies if b["cached"] is True) == 5

    # 全部等待者已及时返回、没有排队卡住；容量回基线
    assert _client().get("/capacity").json()["inflight"] == 0

    # 再次同载荷提交仍复用同一份失败结论，不产生矛盾结论
    again = _client().post(
        "/validations", json={"idempotency_key": "fail-dup", "records": [GOOD]}
    )
    ab = again.json()
    assert ab["cached"] is True
    assert ab["conclusion"] == "EXECUTION_FAILED"
    assert ab["failure"] == bodies[0]["failure"]
    assert gate.calls == 1


def test_full_issue_list_is_in_response_and_order_is_stable(caplog):
    bad = {
        "id": -1,
        "age": "x",
        "score": 9,
        "tags": ["ok", 3],
        "name": "a",
        "extra": 1,
    }
    with caplog.at_level(logging.INFO, logger="dataset.validation"):
        r1 = _client().post(
            "/validations", json={"idempotency_key": "issues-1", "records": [bad, GOOD]}
        )
        r2 = _client().post(
            "/validations", json={"idempotency_key": "issues-2", "records": [GOOD, bad]}
        )
    assert r1.status_code == r2.status_code == 200
    i1 = r1.json()["result"]["records"][0]["issues"]
    i2 = next(x for x in r2.json()["result"]["records"] if not x["valid"])["issues"]
    # 乱序批次中单条记录的问题清单完全一致、顺序固定
    assert i1 == i2
    assert [i["field"] for i in i1] == [
        "extra",
        "id",
        "age",
        "score",
        "tags[1]",
    ]
    # 老字段仍可用：field/reason 取首条问题
    legacy = r1.json()["result"]["records"][0]
    assert legacy["field"] == "extra" and legacy["reason"] == i1[0]["reason"]
    # 输入与每条判定依据进日志
    assert "校验输入" in caplog.text
    assert caplog.text.count("判定依据") == 10  # 两个批次同一坏记录各命中 5 条
