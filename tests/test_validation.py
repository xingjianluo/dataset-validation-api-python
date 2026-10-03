"""逐条记录校验：字段缺失/类型不匹配/取值非法都要可定位，部分失败不丢整批。"""
from __future__ import annotations

import logging

from app.validation import validate_dataset

GOOD = {"id": 1, "name": "alice", "age": 30, "score": 0.9, "tags": ["a", "b"]}


def test_record_with_many_issues_reports_all_of_them(caplog):
    # 一条记录同时命中：缺失 2 个字段、类型错误、取值越界、未知字段、数组元素问题
    record = {
        "id": -5,                       # 取值非法（负数）
        "name": "bob",
        # age 缺失
        "score": "not-a-number",        # 类型不匹配
        "tags": [1, "ok", False],       # tags[0]、tags[2] 元素非字符串
        "mystery": 1,                   # 未知字段
        "zzz": 2,                       # 未知字段（排序在 mystery 之后）
    }
    with caplog.at_level(logging.INFO, logger="dataset.validation"):
        result = validate_dataset([record])

    assert result.failed == 1
    rec = result.records[0]
    assert rec.valid is False

    # 老字段仍是首个问题，老调用方不受影响
    assert rec.field == "mystery"
    assert "未知字段" in rec.reason

    codes = [(i.code.value, i.location) for i in rec.issues]
    assert codes == [
        ("UNKNOWN_FIELD", "mystery"),   # 未知字段按名字排序
        ("UNKNOWN_FIELD", "zzz"),
        ("MISSING_FIELD", "age"),       # 缺失字段按声明顺序
        ("VALUE_INVALID", "id"),        # 各字段按声明顺序：id 取值
        ("TYPE_MISMATCH", "score"),     # score 类型
        ("INVALID_ELEMENT", "tags[0]"),  # 元素按下标
        ("INVALID_ELEMENT", "tags[2]"),
    ]

    # 每个问题都能对上位置与依据
    by_loc = {i.location: i for i in rec.issues}
    assert "非负" in by_loc["id"].reason
    assert "数值" in by_loc["score"].reason
    assert "字符串" in by_loc["tags[0]"].reason
    assert by_loc["tags[0]"].field == "tags"

    # 日志要把每个判定依据都打出来（有多少报多少）
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "判定依据" in joined
    assert "tags[2]" in joined and "tags[0]" in joined
    assert "MISSING_FIELD" in joined


def test_issue_list_is_stable_when_batch_order_is_shuffled(caplog):
    messy = {
        "id": "x",      # 类型错
        "age": 9,       # 取值越界
        "tags": [0],    # 元素错 + name/score 缺失 + 取值（tags 长度 1 合法）
    }
    other = {"id": 2, "name": "ok", "age": 20, "score": 0.1, "tags": ["t"]}

    r1 = validate_dataset([messy, other])
    r2 = validate_dataset([other, messy])

    # 汇总数字不随提交顺序变化
    assert (r1.total, r1.passed, r1.failed) == (2, 1, 1)
    assert (r1.total, r1.passed, r1.failed) == (r2.total, r2.passed, r2.failed)

    # 乱序提交后，同一条记录（按内容）的问题清单顺序与依据完全一致
    def issues_of(result):
        rec = next(x for x in result.records if not x.valid)
        return [(i.code.value, i.location, i.reason) for i in rec.issues]

    assert issues_of(r1) == issues_of(r2) == [
        ("MISSING_FIELD", "name", "字段缺失"),
        ("MISSING_FIELD", "score", "字段缺失"),
        ("TYPE_MISMATCH", "id", "类型不匹配：id 必须是整数"),
        ("VALUE_INVALID", "age", "取值非法：age 必须在 [18, 120] 范围内"),
        ("INVALID_ELEMENT", "tags[0]", "类型不匹配：tags 元素必须是字符串"),
    ]


def test_non_dict_record_reports_single_record_level_issue():
    result = validate_dataset(["not-an-object", 42])
    assert result.failed == 2
    for rec in result.records:
        assert rec.field is None
        assert rec.issues[0].code.value == "NOT_OBJECT"
        assert "JSON 对象" in rec.issues[0].reason


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
