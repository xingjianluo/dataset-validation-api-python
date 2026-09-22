"""HTTP 接口测试：三类结论稳定可区分 + 端到端并发幂等与准入。

日志打印每个请求的输入与响应判定依据（状态码/category/overall_valid）。
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.manager import default_executor

logger = logging.getLogger("tests.api")


@pytest.fixture()
def client():
    app = create_app(max_concurrency=2)
    return TestClient(app)


VALID_RECORD = {"id": 1, "name": "a", "age": 1, "email": "a@b.co", "score": 1}
PARTIAL_RECORDS = [VALID_RECORD, {"id": "bad", "age": 200}]


def test_submit_and_query_happy_path(client):
    logger.info("用例 happy_path 输入=%s", PARTIAL_RECORDS)
    r = client.post("/api/v1/validations",
                    json={"idempotency_key": "k", "records": PARTIAL_RECORDS})
    body = r.json()
    logger.info("判定依据 status=%s overall=%s summary=%s",
                r.status_code, body["outcome"]["result"]["overall_valid"],
                body["outcome"]["result"]["summary"])
    assert r.status_code == 200 and body["accepted"] is True
    g = client.get("/api/v1/validations/k")
    assert g.status_code == 200
    assert g.json()["outcome"] == body["outcome"]


def test_partial_failure_is_http_200_not_rejected(client):
    """校验不通过是业务结论(200, overall_valid=false)，不是参数错误也不是拒绝。"""
    r = client.post("/api/v1/validations",
                    json={"idempotency_key": "pf", "records": PARTIAL_RECORDS})
    body = r.json()
    logger.info("用例 partial_failure 输入=%s -> status=%s category字段存在性=%s",
                PARTIAL_RECORDS, r.status_code, "category" in body)
    assert r.status_code == 200
    result = body["outcome"]["result"]
    assert result["overall_valid"] is False
    assert result["summary"]["invalid_count"] == 1
    assert len(result["records"]) == 2  # 部分失败不丢整批
    invalid = result["records"][1]
    assert invalid["valid"] is False and invalid["issues"]


def test_three_categories_never_mixed(client):
    # 1) 参数非法 422 invalid_request
    r_bad = client.post("/api/v1/validations",
                        json={"idempotency_key": "", "records": []})
    # 2) 校验不通过 200 overall_valid=false
    r_partial = client.post("/api/v1/validations",
                            json={"idempotency_key": "p", "records": PARTIAL_RECORDS})
    # 3) 超限拒绝 503 rejected
    manager = client.app.state.manager
    release = threading.Event()
    real_calls = []

    def blocking_executor(key, records):
        real_calls.append(key)
        release.wait(timeout=5)
        return default_executor(key, records)

    manager._executor = blocking_executor

    def occupy(key):
        client.post("/api/v1/validations", json={"idempotency_key": key, "records": []})

    threads = [threading.Thread(target=occupy, args=(k,)) for k in ("c1", "c2")]
    for t in threads:
        t.start()
    while manager.active_count < 2:
        pass
    r_reject = client.post("/api/v1/validations",
                           json={"idempotency_key": "c3", "records": []})
    release.set()
    for t in threads:
        t.join(timeout=5)

    logger.info("用例 three_categories 判定依据：参数非法=%s/%s；部分失败=%s/overall=%s；"
                "超限=%s/%s reason=%s",
                r_bad.status_code, r_bad.json()["category"],
                r_partial.status_code,
                r_partial.json()["outcome"]["result"]["overall_valid"],
                r_reject.status_code, r_reject.json()["category"],
                r_reject.json()["details"].get("reason"))

    assert r_bad.status_code == 422
    assert r_bad.json()["category"] == "invalid_request"
    assert r_partial.status_code == 200
    assert r_partial.json()["outcome"]["result"]["overall_valid"] is False
    assert r_reject.status_code == 503
    assert r_reject.json()["category"] == "rejected"
    assert r_reject.json()["error"] == "admission_rejected"
    assert r_reject.json()["details"]["reason"] == "concurrency_limit"


def test_concurrent_duplicate_returns_identical_single_execution(client):
    manager = client.app.state.manager
    gate = threading.Barrier(8)
    calls: list[str] = []
    lock = threading.Lock()

    def counting_executor(key, records):
        with lock:
            calls.append(key)
        return default_executor(key, records)

    manager._executor = counting_executor

    n = 8

    def fire():
        gate.wait()
        return client.post("/api/v1/validations",
                           json={"idempotency_key": "same",
                                 "records": PARTIAL_RECORDS}).json()

    logger.info("用例 concurrent_duplicate 输入 key=same 并发=%d", n)
    with ThreadPoolExecutor(max_workers=n) as pool:
        bodies = [f.result() for f in [pool.submit(fire) for _ in range(n)]]

    logger.info("判定依据 执行次数=%d（期望1），各响应是否一致=%s",
                len(calls), all(b == bodies[0] for b in bodies))
    assert calls == ["same"]
    assert all(b == bodies[0] for b in bodies)
    assert bodies[0]["outcome"]["result"]["summary"]["total"] == 2


def test_query_unknown_key_returns_not_found(client):
    r = client.get("/api/v1/validations/missing")
    logger.info("用例 not_found 判定依据 status=%s body=%s", r.status_code, r.json())
    assert r.status_code == 404
    assert r.json()["category"] == "not_found"


def test_health_reports_limits(client):
    r = client.get("/health")
    logger.info("用例 health 判定依据 %s", r.json())
    assert r.status_code == 200
    assert r.json()["max_concurrency"] == 2
