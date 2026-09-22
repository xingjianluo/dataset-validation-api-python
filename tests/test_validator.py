"""逐条记录结构/取值校验的单测。

每条用例都会通过日志打印「输入」与「判定依据（issues）」，
满足“单测日志中需要打印输入与判定依据”的要求。
"""

from __future__ import annotations

import logging

from app.models import RecordErrorCode
from app.validator import validate_one, validate_records

logger = logging.getLogger("tests.validator")


def _dump_result(result) -> list[dict]:
    return [i.model_dump() for i in result.issues]


def test_valid_record_passes():
    record = {"id": 1, "name": "alice", "age": 30, "email": "a@x.com", "score": 92.5}
    logger.info("用例 test_valid_record_passes 输入=%s", record)
    result = validate_one(0, record)
    logger.info("判定依据 valid=%s issues=%s", result.valid, _dump_result(result))
    assert result.valid is True and result.issues == []


def test_missing_fields_marked_and_locatable():
    record = {"id": 7}
    logger.info("用例 test_missing_fields 输入=%s", record)
    result = validate_one(3, record)
    missing = {i.field: i for i in result.issues if i.code == RecordErrorCode.MISSING_FIELD}
    logger.info("判定依据 index=%s issues=%s", result.index, _dump_result(result))
    assert result.valid is False and result.index == 3
    assert set(missing) == {"name", "age", "email", "score"}
    # 每条依据可定位到字段并说明期望类型。
    assert all(issue.expected is not None for issue in missing.values())


def test_type_mismatch_distinguished_from_invalid_value():
    record = {"id": "x", "name": 9, "age": True, "email": 3, "score": "A"}
    logger.info("用例 test_type_mismatch 输入=%s", record)
    result = validate_one(0, record)
    by_field = {i.field: i for i in result.issues}
    logger.info("判定依据 issues=%s", _dump_result(result))
    assert by_field["id"].code == RecordErrorCode.TYPE_MISMATCH
    assert by_field["name"].code == RecordErrorCode.TYPE_MISMATCH
    # bool 不得被当作合法整数年龄。
    assert by_field["age"].code == RecordErrorCode.TYPE_MISMATCH
    assert by_field["email"].code == RecordErrorCode.TYPE_MISMATCH
    assert by_field["score"].code == RecordErrorCode.TYPE_MISMATCH


def test_invalid_values_include_expected_range():
    record = {"id": 0, "name": "   ", "age": 200, "email": "bad-email", "score": 101}
    logger.info("用例 test_invalid_values 输入=%s", record)
    result = validate_one(0, record)
    by_field = {i.field: i for i in result.issues}
    logger.info("判定依据 issues=%s", _dump_result(result))
    for field in ("id", "name", "age", "email", "score"):
        assert by_field[field].code == RecordErrorCode.INVALID_VALUE
        assert by_field[field].expected, "必须给出可解释的合法取值范围"
        assert by_field[field].actual is not None


def test_non_object_record_marked_as_record_level():
    record = ["not", "an", "object"]
    logger.info("用例 test_non_object_record 输入=%r", record)
    result = validate_one(5, record)
    logger.info("判定依据 issues=%s", _dump_result(result))
    assert result.valid is False and result.index == 5
    assert result.issues[0].code == RecordErrorCode.TYPE_MISMATCH
    assert result.issues[0].field == "__record__"


def test_partial_failure_keeps_every_record_and_counts():
    records = [
        {"id": 1, "name": "a", "age": 1, "email": "a@b.co", "score": 0},
        {"id": "bad"},
        {"id": 3, "name": "c", "age": 99, "email": "c@d.co", "score": 100},
    ]
    logger.info("用例 test_partial_failure 输入=%s", records)
    data = validate_records(records)
    logger.info(
        "判定依据 overall=%s summary=%s records_valid=%s",
        data.overall_valid, data.summary.model_dump(),
        [r.valid for r in data.records],
    )
    # 部分失败不丢失任何记录。
    assert len(data.records) == 3
    assert data.overall_valid is False
    assert data.summary.model_dump() == {
        "total": 3, "valid_count": 2, "invalid_count": 1, "issue_count": 5,
    }
    assert [r.valid for r in data.records] == [True, False, True]
    # 失败记录的索引可定位回原批次位置。
    assert data.records[1].index == 1


def test_conclusion_independent_of_submission_order():
    base = [
        {"id": 1, "name": "a", "age": 1, "email": "a@b.co", "score": 0},
        {"id": "bad"},
        {"id": 3, "name": "c", "age": 99, "email": "c@d.co", "score": 100},
    ]
    shuffled = [base[2], base[1], base[0]]
    logger.info("用例 test_order_independence 输入顺序A=%s 顺序B=%s", base, shuffled)
    a = validate_records(base)
    b = validate_records(shuffled)
    # 汇总与整体结论一致；每条记录按内容得到同样的 issues。
    assert a.summary == b.summary and a.overall_valid == b.overall_valid
    signatures_a = sorted(
        tuple((i.field, i.code.value, i.message) for i in r.issues) for r in a.records
    )
    signatures_b = sorted(
        tuple((i.field, i.code.value, i.message) for i in r.issues) for r in b.records
    )
    assert signatures_a == signatures_b


def test_empty_batch_is_overall_valid():
    logger.info("用例 test_empty_batch 输入=[]")
    data = validate_records([])
    logger.info("判定依据 summary=%s", data.summary.model_dump())
    assert data.overall_valid is True
    assert data.summary.total == 0
