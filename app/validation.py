"""逐条记录的结构与取值校验。

每条记录一次性返回命中的**全部**问题（有多少报多少），每个问题都可定位：
- 记录级问题（记录不是对象）：location=None；
- 字段级问题：location 为字段名（``age``）；
- 数组元素问题：location 为元素位置（``tags[0]``），归属字段 ``tags``。

单条记录内问题的展示顺序固定（未知字段按字段名排序 → 缺失字段按声明顺序
→ 各字段按声明顺序做类型、取值检查 → 数组元素按下标），只取决于记录本身；
因此整批记录乱序提交时，单条记录的问题清单与汇总数字都不随提交顺序变化。
"""
from __future__ import annotations

import logging

from app.models import Issue, IssueCode, RecordResult, ValidationResult

logger = logging.getLogger("dataset.validation")

ALLOWED_FIELDS = ("id", "name", "age", "score", "tags")


def _is_int(value: object) -> bool:
    # bool 是 int 的子类型，校验语义上必须排除
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_record(record: object, index: int) -> list[Issue]:
    """返回该记录命中的全部问题；空列表表示通过。检查顺序固定。"""
    issues: list[Issue] = []

    if not isinstance(record, dict):
        issues.append(Issue(
            code=IssueCode.NOT_OBJECT,
            location=None,
            field=None,
            reason="记录必须是 JSON 对象",
        ))
        return issues

    # 1) 未知字段：按字段名排序，逐个报告
    for name in sorted(k for k in record if k not in ALLOWED_FIELDS):
        issues.append(Issue(
            code=IssueCode.UNKNOWN_FIELD,
            location=name,
            field=name,
            reason="未知字段，不在允许的字段集合内",
        ))

    # 2) 必填字段缺失：按字段声明顺序报告
    for name in ALLOWED_FIELDS:
        if name not in record:
            issues.append(Issue(
                code=IssueCode.MISSING_FIELD,
                location=name,
                field=name,
                reason="字段缺失",
            ))

    # 3) 逐字段做类型/取值检查（仅检查存在的字段；类型不对则不再查取值）
    if "id" in record:
        value = record["id"]
        if not _is_int(value):
            issues.append(Issue(
                code=IssueCode.TYPE_MISMATCH, location="id", field="id",
                reason="类型不匹配：id 必须是整数",
            ))
        elif value < 0:
            issues.append(Issue(
                code=IssueCode.VALUE_INVALID, location="id", field="id",
                reason="取值非法：id 必须为非负整数",
            ))

    if "name" in record:
        value = record["name"]
        if not isinstance(value, str):
            issues.append(Issue(
                code=IssueCode.TYPE_MISMATCH, location="name", field="name",
                reason="类型不匹配：name 必须是字符串",
            ))
        elif not value.strip() or len(value) > 100:
            issues.append(Issue(
                code=IssueCode.VALUE_INVALID, location="name", field="name",
                reason="取值非法：name 必须是 1-100 个非空白字符",
            ))

    if "age" in record:
        value = record["age"]
        if not _is_int(value):
            issues.append(Issue(
                code=IssueCode.TYPE_MISMATCH, location="age", field="age",
                reason="类型不匹配：age 必须是整数",
            ))
        elif not 18 <= value <= 120:
            issues.append(Issue(
                code=IssueCode.VALUE_INVALID, location="age", field="age",
                reason="取值非法：age 必须在 [18, 120] 范围内",
            ))

    if "score" in record:
        value = record["score"]
        if not _is_number(value):
            issues.append(Issue(
                code=IssueCode.TYPE_MISMATCH, location="score", field="score",
                reason="类型不匹配：score 必须是数值",
            ))
        elif not 0.0 <= float(value) <= 1.0:
            issues.append(Issue(
                code=IssueCode.VALUE_INVALID, location="score", field="score",
                reason="取值非法：score 必须在 [0.0, 1.0] 范围内",
            ))

    if "tags" in record:
        value = record["tags"]
        if not isinstance(value, list):
            issues.append(Issue(
                code=IssueCode.TYPE_MISMATCH, location="tags", field="tags",
                reason="类型不匹配：tags 必须是数组",
            ))
        else:
            if not 1 <= len(value) <= 10:
                issues.append(Issue(
                    code=IssueCode.VALUE_INVALID, location="tags", field="tags",
                    reason="取值非法：tags 元素数量必须在 [1, 10]",
                ))
            # 元素逐个检查：无论长度是否合法，元素自身的问题都要报全
            for i, item in enumerate(value):
                if not isinstance(item, str):
                    issues.append(Issue(
                        code=IssueCode.INVALID_ELEMENT,
                        location=f"tags[{i}]", field="tags",
                        reason="类型不匹配：tags 元素必须是字符串",
                    ))

    return issues


def validate_dataset(records: list) -> ValidationResult:
    logger.info("校验输入: records=%r", records)

    results: list[RecordResult] = []
    failed_count = 0
    for index, record in enumerate(records):
        issues = _check_record(record, index)
        if not issues:
            results.append(RecordResult(index=index, valid=True))
            continue

        failed_count += 1
        # 老字段 field/reason 保留“首个问题”的语义，老调用方无需改动；
        # 新增 issues 承载完整问题清单。
        first = issues[0]
        results.append(RecordResult(
            index=index,
            valid=False,
            field=first.location,
            reason=first.reason,
            issues=issues,
        ))
        for issue in issues:
            logger.info(
                "判定依据: index=%d code=%s field=%s reason=%s record=%r",
                index, issue.code.value, issue.location, issue.reason, record,
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
