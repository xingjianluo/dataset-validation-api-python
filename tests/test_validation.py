"""逐条记录校验：字段缺失/类型不匹配/取值非法都要可定位，部分失败不丢整批。"""

from __future__ import annotations

import logging

from app.validation import validate_dataset

GOOD = {"id": 1, "name": "alice", "age": 30, "score": 0.9, "tags": ["a", "b"]}


def test_all_records_validated_and_partial_failure_is_preserved(caplog):
    records = [
        GOOD,
        {"id": 2, "name": "bob", "age": 17, "score": 0.5, "tags": ["x"]},  # age 非法
        {
            "id": 3,
            "age": 30,
            "score": 0.5,
            "tags": ["x"],
            "name": "c",
        },  # 仅顺序不同，应通过
        "not-an-object",  # 结构错误
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
        (
            {"id": "x", "name": "a", "age": 30, "score": 0.5, "tags": ["x"]},
            "id",
            "类型",
        ),
        (
            {"id": 1, "name": "a", "age": 30, "score": 2.0, "tags": ["x"]},
            "score",
            "非法",
        ),
        ({"id": 1, "name": "a", "age": 30, "score": 0.5, "tags": []}, "tags", "非法"),
        (
            {"id": 1, "name": "a", "age": True, "score": 0.5, "tags": ["x"]},
            "age",
            "类型",
        ),
        (
            {"id": 1, "name": "a", "age": 30, "score": 0.5, "tags": ["x"], "extra": 1},
            "extra",
            "未知字段",
        ),
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
    assert (r1.passed, r1.failed, r1.all_passed) == (
        r2.passed,
        r2.failed,
        r2.all_passed,
    )

    # 每条记录的判定内容不随整体提交顺序改变（按内容核对）
    def find(records_result, field):
        return next(x for x in records_result if x.field == field)

    assert find(r1.records, "age").reason == find(r2.records, "age").reason


def _issue_fields(record):
    r = validate_dataset([record]).records[0]
    return [(i.field, i.reason) for i in r.issues]


def test_single_record_reports_all_hit_issues_with_locations():
    # 一条记录同时命中：未知字段、缺失、类型、越界、两个数组元素位置错误
    record = {
        "id": -1,
        "age": "x",
        "score": 5,
        "tags": ["ok", 3, 9],
        "extra": 1,
        "zzz": 2,
    }
    r = validate_dataset([record]).records[0]
    assert r.valid is False
    fields = [i.field for i in r.issues]
    assert fields == [
        "extra",
        "zzz",  # 未知字段：字段名升序
        "name",  # 缺失字段：schema 顺序
        "id",  # id 越界
        "age",  # age 类型错
        "score",  # score 越界
        "tags[1]",
        "tags[2]",  # 元素位置逐个报，按下标
    ]
    reasons = [i.reason for i in r.issues]
    assert all(reasons), "每个问题都必须给出原因"
    assert "未知字段" in reasons[0]
    assert reasons[2] == "字段缺失"
    assert "非负整数" in reasons[3]
    assert "整数" in reasons[4]
    assert "1.0" in reasons[5]
    assert reasons[6] == reasons[7] == "类型不匹配：tags 元素必须是字符串"
    # 兼容老字段：field/reason 取问题清单第一条
    assert r.field == "extra" and r.reason == reasons[0]


def test_valid_record_has_empty_issue_list():
    r = validate_dataset([GOOD]).records[0]
    assert r.valid is True and r.issues == []
    assert r.field is None and r.reason is None


def test_tags_length_and_element_issues_both_reported():
    tags = [
        "a",
        "b",
        "c",
        "d",
        "e",
        4,
        "g",
        "h",
        "i",
        "j",
        "k",
    ]  # 11 个，且 tags[5] 非字符串
    record = {"id": 1, "name": "a", "age": 30, "score": 0.5, "tags": tags}
    fields = [f for f, _ in _issue_fields(record)]
    # 长度非法与元素类型错误互不掩盖，长度问题在前、元素位置问题在后
    assert fields == ["tags", "tags[5]"]


def test_issue_list_order_is_stable_under_key_and_record_shuffle():
    bad = {"zzz": 1, "extra": 2, "tags": ["ok", 3], "score": 9, "age": "x", "id": -1}
    bad_shuffled = {
        "score": 9,
        "id": -1,
        "extra": 2,
        "zzz": 1,
        "tags": ["ok", 3],
        "age": "x",
    }
    r1 = validate_dataset([GOOD, bad])
    r2 = validate_dataset([bad_shuffled, GOOD])

    def issues_of(result):
        rec = next(x for x in result.records if not x.valid)
        return [(i.field, i.reason) for i in rec.issues]

    assert issues_of(r1) == issues_of(r2)
    # 整批汇总数字也不随乱序变化
    assert (
        (r1.total, r1.passed, r1.failed)
        == (r2.total, r2.passed, r2.failed)
        == (2, 1, 1)
    )
    # 多次执行结果逐字段一致（确定性）
    r3 = validate_dataset([bad_shuffled, GOOD])
    assert [i.model_dump() for i in r2.records[0].issues] == [
        i.model_dump() for i in r3.records[0].issues
    ]


def test_each_issue_and_input_are_logged(caplog):
    record = {"id": "x", "name": "a", "age": 30, "score": 0.5, "tags": ["x"], "e": 1}
    with caplog.at_level(logging.INFO, logger="dataset.validation"):
        validate_dataset([record])
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "校验输入" in joined
    # 两个命中问题（未知字段 e、id 类型错）都应作为判定依据进日志
    assert joined.count("判定依据") == 2
    assert "field=e" in joined and "field=id" in joined
