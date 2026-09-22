"""逐条记录校验：字段缺失/类型不匹配/取值非法都要可定位，部分失败不丢整批。"""
from __future__ import annotations

import logging

from app.validation import validate_dataset

GOOD = {"id": 1, "name": "alice", "age": 30, "score": 0.9, "tags": ["a", "b"]}


def test_all_records_validated_and_partial_failure_is_preserved(caplog):
    records = [
        GOOD,
        {"id": 2, "name": "bob", "age": 17, "score": 0.5, "tags": ["x"]},   # age 非法
        {"id": 3, "age": 30, "score": 0.5, "tags": ["x"], "name": "c"},     # 仅顺序不同，应通过
        "not-an-object",                                                     # 结构错误
    ]
    with caplog.at_level(logging.INFO, logger="dataset.validation"):
        result = validate_dataset(records)

    assert result.total == 4
    assert result.passed == 2
    assert result.failed == 2
    assert result.all_passed is False
    # 每条记录都保留判定结果，部分失败不丢整批
    assert len(result.records) == 4
    assert result.records[0].valid is True
    assert result.records[1].valid is False
    assert result.records[1].field == "age"
    assert "18" in result.records[1].reason
    assert result.records[3].field is None
    assert "JSON 对象" in result.records[3].reason
    # 日志中必须打印输入与判定依据
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "校验输入" in joined
    assert "判定依据" in joined and "age" in joined


def test_failure_categories_are_distinguishable():
    cases = [
        ({k: v for k, v in GOOD.items() if k != "id"}, "id", "缺失"),
        ({"id": "x", "name": "a", "age": 30, "score": 0.5, "tags": ["x"]}, "id", "类型"),
        ({"id": 1, "name": "a", "age": 30, "score": 2.0, "tags": ["x"]}, "score", "非法"),
        ({"id": 1, "name": "a", "age": 30, "score": 0.5, "tags": []}, "tags", "非法"),
        ({"id": 1, "name": "a", "age": True, "score": 0.5, "tags": ["x"]}, "age", "类型"),
        ({"id": 1, "name": "a", "age": 30, "score": 0.5, "tags": ["x"], "extra": 1},
         "extra", "未知字段"),
    ]
    for record, field, reason_part in cases:
        r = validate_dataset([record])
        assert r.all_passed is False
        first = r.records[0]
        assert first.field == field, record
        assert reason_part in (first.reason or ""), record


def test_conclusion_independent_of_record_order():
    a = GOOD
    b = {"id": 2, "name": "bob", "age": 200, "score": 0.5, "tags": ["x"]}
    r1 = validate_dataset([a, b])
    r2 = validate_dataset([b, a])
    assert (r1.total, r1.passed, r1.failed, r1.all_passed) == (2, 1, 1, False)
    assert (r1.passed, r1.failed, r1.all_passed) == (r2.passed, r2.failed, r2.all_passed)
    # 每条记录的判定内容不随整体提交顺序改变（按内容核对）
    def find(records_result, field):
        return next(x for x in records_result if x.field == field)
    assert find(r1.records, "age").reason == find(r2.records, "age").reason
