"""逐条记录的结构与取值校验。

每条不通过记录一次性给出命中的**全部**问题（issues），每个问题可定位到
字段或数组元素位置（field / tags[i]）与原因。问题清单顺序固定：

1. 记录不是 JSON 对象：仅 1 条结构性问题；
2. 未知字段：按字段名升序；
3. 缺失字段：按 schema 字段顺序（id, name, age, score, tags）；
4. 字段级问题：按 schema 字段顺序，字段内先类型后取值；
   tags 的元素问题按下标升序，排在 tags 自身问题之后。

因此同一条记录的问题清单与整批汇总数字都不受提交顺序影响。
为兼容老调用方，RecordResult.field/reason 仍保留，取问题清单的第一条。
汇总结论（total/passed/failed/all_passed）只取决于记录集合本身。
"""

from __future__ import annotations

import logging

from app.models import Issue, RecordResult, ValidationResult

logger = logging.getLogger("dataset.validation")

ALLOWED_FIELDS = ("id", "name", "age", "score", "tags")


def _is_int(value: object) -> bool:
    # bool 是 int 的子类型，校验语义上必须排除
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _collect_issues(record: dict, index: int) -> list[Issue]:
    """收集单条记录命中的全部问题；返回空列表表示通过。"""
    issues: list[Issue] = []

    # 1) 未知字段：字段名升序，保证顺序稳定
    for name in sorted(k for k in record if k not in ALLOWED_FIELDS):
        issues.append(Issue(field=name, reason="未知字段，不在允许的字段集合内"))

    # 2) 缺失字段：按 schema 顺序
    missing = [f for f in ALLOWED_FIELDS if f not in record]
    for name in missing:
        issues.append(Issue(field=name, reason="字段缺失"))

    # 3) 字段级检查：缺失的字段不再做类型/取值检查
    if "id" in record:
        value = record["id"]
        if not _is_int(value):
            issues.append(Issue(field="id", reason="类型不匹配：id 必须是整数"))
        elif value < 0:
            issues.append(Issue(field="id", reason="取值非法：id 必须为非负整数"))

    if "name" in record:
        value = record["name"]
        if not isinstance(value, str):
            issues.append(Issue(field="name", reason="类型不匹配：name 必须是字符串"))
        elif not value.strip() or len(value) > 100:
            issues.append(
                Issue(field="name", reason="取值非法：name 必须是 1-100 个非空白字符")
            )

    if "age" in record:
        value = record["age"]
        if not _is_int(value):
            issues.append(Issue(field="age", reason="类型不匹配：age 必须是整数"))
        elif not 18 <= value <= 120:
            issues.append(
                Issue(field="age", reason="取值非法：age 必须在 [18, 120] 范围内")
            )

    if "score" in record:
        value = record["score"]
        if not _is_number(value):
            issues.append(Issue(field="score", reason="类型不匹配：score 必须是数值"))
        elif not 0.0 <= float(value) <= 1.0:
            issues.append(
                Issue(field="score", reason="取值非法：score 必须在 [0.0, 1.0] 范围内")
            )

    if "tags" in record:
        value = record["tags"]
        if not isinstance(value, list):
            issues.append(Issue(field="tags", reason="类型不匹配：tags 必须是数组"))
        else:
            if not 1 <= len(value) <= 10:
                issues.append(
                    Issue(field="tags", reason="取值非法：tags 元素数量必须在 [1, 10]")
                )
            # 元素级问题逐个报出，定位到具体下标；与长度问题互不掩盖
            for i, item in enumerate(value):
                if not isinstance(item, str):
                    issues.append(
                        Issue(
                            field=f"tags[{i}]",
                            reason="类型不匹配：tags 元素必须是字符串",
                        )
                    )

    return issues


def _check_record(record: object, index: int) -> RecordResult:
    """返回该记录的完整判定；valid=True 时问题清单为空。"""
    if not isinstance(record, dict):
        issues = [Issue(field=None, reason="记录必须是 JSON 对象")]
        return RecordResult(
            index=index,
            valid=False,
            field=issues[0].field,
            reason=issues[0].reason,
            issues=issues,
        )

    issues = _collect_issues(record, index)
    if not issues:
        return RecordResult(index=index, valid=True)
    return RecordResult(
        index=index,
        valid=False,
        field=issues[0].field,
        reason=issues[0].reason,
        issues=issues,
    )


def validate_dataset(records: list) -> ValidationResult:
    logger.info("校验输入: records=%r", records)

    results: list[RecordResult] = []
    failed_count = 0
    for index, record in enumerate(records):
        outcome = _check_record(record, index)
        results.append(outcome)
        if outcome.valid:
            continue
        failed_count += 1
        for issue in outcome.issues:
            logger.info(
                "判定依据: index=%d field=%s reason=%s record=%r",
                index,
                issue.field,
                issue.reason,
                record,
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
        result.total,
        result.passed,
        result.failed,
        result.all_passed,
    )
    return result
