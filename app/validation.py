"""逐条记录的结构与取值校验。

判定依据对每条失败记录给出：所在下标 index、字段 field、原因 reason。
汇总结论（total/passed/failed/all_passed）只取决于记录集合本身，
与记录在列表中的先后顺序无关。
"""
from __future__ import annotations

import logging

from app.models import RecordResult, ValidationResult

logger = logging.getLogger("dataset.validation")

ALLOWED_FIELDS = ("id", "name", "age", "score", "tags")


def _is_int(value: object) -> bool:
    # bool 是 int 的子类型，校验语义上必须排除
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_record(record: object, index: int) -> RecordResult | None:
    """返回 None 表示该记录通过；否则返回首条失败依据（检查顺序固定）。"""
    if not isinstance(record, dict):
        return RecordResult(index=index, valid=False, field=None,
                            reason="记录必须是 JSON 对象")

    for name in sorted(k for k in record if k not in ALLOWED_FIELDS):
        return RecordResult(index=index, valid=False, field=name,
                            reason="未知字段，不在允许的字段集合内")

    for field in ALLOWED_FIELDS:
        if field not in record:
            return RecordResult(index=index, valid=False, field=field,
                                reason="字段缺失")

    value = record["id"]
    if not _is_int(value):
        return RecordResult(index=index, valid=False, field="id",
                            reason="类型不匹配：id 必须是整数")
    if value < 0:
        return RecordResult(index=index, valid=False, field="id",
                            reason="取值非法：id 必须为非负整数")

    value = record["name"]
    if not isinstance(value, str):
        return RecordResult(index=index, valid=False, field="name",
                            reason="类型不匹配：name 必须是字符串")
    if not value.strip() or len(value) > 100:
        return RecordResult(index=index, valid=False, field="name",
                            reason="取值非法：name 必须是 1-100 个非空白字符")

    value = record["age"]
    if not _is_int(value):
        return RecordResult(index=index, valid=False, field="age",
                            reason="类型不匹配：age 必须是整数")
    if not 18 <= value <= 120:
        return RecordResult(index=index, valid=False, field="age",
                            reason="取值非法：age 必须在 [18, 120] 范围内")

    value = record["score"]
    if not _is_number(value):
        return RecordResult(index=index, valid=False, field="score",
                            reason="类型不匹配：score 必须是数值")
    if not 0.0 <= float(value) <= 1.0:
        return RecordResult(index=index, valid=False, field="score",
                            reason="取值非法：score 必须在 [0.0, 1.0] 范围内")

    value = record["tags"]
    if not isinstance(value, list):
        return RecordResult(index=index, valid=False, field="tags",
                            reason="类型不匹配：tags 必须是数组")
    if not 1 <= len(value) <= 10:
        return RecordResult(index=index, valid=False, field="tags",
                            reason="取值非法：tags 元素数量必须在 [1, 10]")
    for i, item in enumerate(value):
        if not isinstance(item, str):
            return RecordResult(index=index, valid=False, field=f"tags[{i}]",
                                reason="类型不匹配：tags 元素必须是字符串")

    return None


def validate_dataset(records: list) -> ValidationResult:
    logger.info("校验输入: records=%r", records)

    results: list[RecordResult] = []
    failed_count = 0
    for index, record in enumerate(records):
        failure = _check_record(record, index)
        if failure is None:
            results.append(RecordResult(index=index, valid=True))
        else:
            failed_count += 1
            results.append(failure)
            logger.info(
                "判定依据: index=%d field=%s reason=%s record=%r",
                failure.index, failure.field, failure.reason, record,
            )

    result = ValidationResult(
        total=len(records),
        passed=len(records) - failed_count,
        failed=failed_count,
        all_passed=failed_count == 0,
        records=results,
    )
    logger.info(
        "校验结论: total=%d passed=%d failed=%d all_passed=%s",
        result.total, result.passed, result.failed, result.all_passed,
    )
    return result
