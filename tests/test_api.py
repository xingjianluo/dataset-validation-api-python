"""HTTP 层：三类结论稳定可区分；并发同 key 只执行一次；超限立即拒绝。"""
from __future__ import annotations

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
    """可阻塞的校验器：执行到即设置 started，放行前一直占着并发槽位。

    fail=True 时放行后抛出执行异常，用于模拟执行失败。
    """

    def __init__(self, monkeypatch, fail: bool = False) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.fail = fail
        self._lock = threading.Lock()

        def blocked(records):
            with self._lock:
                self.calls += 1
            self.started.set()
            assert self.release.wait(timeout=5)
            if self.fail:
                raise RuntimeError("执行依赖不可用（模拟）")
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
        "/validations", content=b"{not-json", headers={"content-type": "application/json"}
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
    r1 = c.post("/validations", json={"idempotency_key": "batch", "records": [GOOD, BAD_AGE, MISSING]})
    r2 = c.post("/validations", json={"idempotency_key": "batch2", "records": [MISSING, GOOD, BAD_AGE]})
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
    assert fail_reasons1 == fail_reasons2 == ["取值非法：age 必须在 [18, 120] 范围内", "字段缺失"]
    assert all(x["field"] == "age" for x in recs1 + recs2 if not x["valid"])


def test_conflict_and_not_found_codes_are_stable():
    c = _client()
    c.post("/validations", json={"idempotency_key": "k", "records": [GOOD]})
    conflict = c.post("/validations", json={"idempotency_key": "k", "records": [BAD_AGE]})
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    missing = c.get("/validations/unknown")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOT_FOUND"


def test_full_issue_list_is_returned_and_legacy_fields_kept():
    c = _client()
    messy = {
        "id": -1,
        "name": "bob",
        # 缺 age
        "score": "x",
        "tags": [1, "a"],
        "extra": 1,
    }
    r = c.post("/validations", json={"idempotency_key": "issues", "records": [messy]})
    assert r.status_code == 200
    body = r.json()
    assert body["conclusion"] == "VALIDATION_FAILED"
    rec = body["result"]["records"][0]

    # 老字段继续可用：指向首个问题
    assert rec["field"] == "extra"
    assert "未知字段" in rec["reason"]

    # 新增 issues 承载完整清单，顺序固定
    codes = [(i["code"], i["location"]) for i in rec["issues"]]
    assert codes == [
        ("UNKNOWN_FIELD", "extra"),
        ("MISSING_FIELD", "age"),
        ("VALUE_INVALID", "id"),
        ("TYPE_MISMATCH", "score"),
        ("INVALID_ELEMENT", "tags[0]"),
    ]
    assert rec["issues"][-1]["field"] == "tags"


def test_execution_failure_end_to_end(monkeypatch, caplog):
    import logging as _logging

    caplog.set_level(_logging.WARNING, logger="dataset.manager")
    gate = BlockingValidator(monkeypatch, fail=True)
    c = _client()
    t = threading.Thread(
        target=lambda: _client().post(
            "/validations", json={"idempotency_key": "boom", "records": [GOOD]}
        )
    )
    t.start()
    assert gate.started.wait(timeout=2)

    # 执行中可查到 RUNNING，且容量被占用
    assert c.get("/validations/boom").json()["status"] == "RUNNING"
    assert c.get("/capacity").json()["inflight"] == 1

    gate.open()
    t.join(timeout=10)

    # 执行失败：200 + 专属 conclusion，与其他三类明确区分
    r = c.post("/validations", json={"idempotency_key": "boom", "records": [GOOD]})
    assert r.status_code == 200
    body = r.json()
    assert body["conclusion"] == "EXECUTION_FAILED"
    assert body["cached"] is True  # 复用失败结论，未再次执行
    assert body["execution_error"]["type"] == "RuntimeError"
    assert gate.calls == 1

    # 查询接口同样可查到失败结论
    got = c.get("/validations/boom").json()
    assert got["conclusion"] == "EXECUTION_FAILED"
    assert got["execution_error"]["message"] == "执行依赖不可用（模拟）"

    # 容量回到基线
    snap = c.get("/capacity").json()
    assert snap == {"inflight": 0, "max_concurrency": 2, "available": 2}
    assert "执行失败" in " ".join(rec.getMessage() for rec in caplog.records)


def test_concurrent_same_key_failure_submissions_converge(monkeypatch):
    gate = BlockingValidator(monkeypatch, fail=True)
    bodies: list = []
    errors: list = []

    def worker() -> None:
        c = _client()
        r = c.post("/validations", json={"idempotency_key": "boom-dup",
                                         "records": [GOOD]})
        (bodies if r.status_code == 200 else errors).append(r.json() if r.status_code == 200 else r)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    assert gate.started.wait(timeout=2)
    gate.open()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive()

    assert errors == []
    assert gate.calls == 1
    assert len(bodies) == 6
    assert all(b["conclusion"] == "EXECUTION_FAILED" for b in bodies)
    assert sum(1 for b in bodies if b["cached"] is False) == 1
    assert sum(1 for b in bodies if b["cached"] is True) == 5
    # 没有矛盾结论：失败载荷逐字节一致
    first = bodies[0]["execution_error"]
    assert all(b["execution_error"] == first for b in bodies)
    assert _client().get("/capacity").json()["inflight"] == 0


def test_capacity_snapshot_during_and_after_run(monkeypatch):
    gate = BlockingValidator(monkeypatch)
    c = _client()
    t = threading.Thread(
        target=lambda: _client().post(
            "/validations", json={"idempotency_key": "cap", "records": [GOOD]}
        )
    )
    t.start()
    assert gate.started.wait(timeout=2)
    snap = c.get("/capacity").json()
    assert snap == {"inflight": 1, "max_concurrency": 2, "available": 1}
    gate.open()
    t.join(timeout=10)
    assert c.get("/capacity").json()["inflight"] == 0
