"""逐条记录的结构与取值校验（纯函数，无 I/O、无随机、与顺序无关）。

每条记录的判定彼此独立：同一条记录无论出现在批次的什么位置，
得到的 issues 与 valid 结论完全一致；汇总计数只取决于记录内容。
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from typing import Any

from app.models import (
    RecordErrorCode,
    RecordIssue,
    RecordResult,
    ValidationResultData,
    ValidationSummary,
)

# 记录整体不是对象时，用该字段名表示“整条记录”的定位。
RECORD_FIELD = "__record__"

# 邮箱的最小可用形态：local@domain.tld，不含空白。
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_MAX_NAME_LEN = 100
_MAX_EMAIL_LEN = 254


def _is_int(value: Any) -> bool:
    # bool 是 int 的子类，需显式排除。
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    # bool 是 int 的子类，需显式排除。
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _actual(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 50 else text[:47] + "..."


def _type_issue(field: str, expected: str, value: Any) -> RecordIssue:
    return RecordIssue(
        field=field,
        code=RecordErrorCode.TYPE_MISMATCH,
        message=f"字段 {field} 类型不匹配",
        expected=expected,
        actual=type(value).__name__,
    )


def _value_issue(field: str, expected: str, value: Any) -> RecordIssue:
    return RecordIssue(
        field=field,
        code=RecordErrorCode.INVALID_VALUE,
        message=f"字段 {field} 取值非法",
        expected=expected,
        actual=_actual(value),
    )


# 每个字段的规则：(期望类型描述, 类型判定, 取值判定, 取值范围描述)
_RULES: dict[str, tuple[str, Callable[[Any], bool], Callable[[Any], bool], str]] = {
    "id": (
        "整数",
        _is_int,
        lambda v: v >= 1,
        "正整数 (>=1)",
    ),
    "name": (
        "字符串",
        lambda v: isinstance(v, str),
        lambda v: 0 < len(v.strip()) <= _MAX_NAME_LEN,
        f"非空字符串，长度 <= {_MAX_NAME_LEN}",
    ),
    "age": (
        "整数",
        _is_int,
        lambda v: 1 <= v <= 150,
        "1~150 的整数",
    ),
    "email": (
        "字符串",
        lambda v: isinstance(v, str),
        lambda v: len(v) <= _MAX_EMAIL_LEN and _EMAIL_RE.match(v) is not None,
        "形如 local@domain.tld 的邮箱",
    ),
    "score": (
        "数值",
        _is_number,
        lambda v: math.isfinite(v) and 0 <= v <= 100,
        "0~100 的有限数值",
    ),
}

REQUIRED_FIELDS = tuple(_RULES.keys())


def _validate_object(record: dict) -> list[RecordIssue]:
    issues: list[RecordIssue] = []

    # 未知字段：结构上不属于约定 schema。
    for field in sorted(set(record) - set(REQUIRED_FIELDS)):
        issues.append(
            RecordIssue(
                field=field,
                code=RecordErrorCode.UNKNOWN_FIELD,
                message=f"字段 {field} 不在约定 schema 中",
                expected=f"仅允许 {list(REQUIRED_FIELDS)}",
                actual=_actual(record[field]),
            )
        )

    # 固定 schema 顺序检查必填字段，保证判定依据稳定。
    for field, (type_desc, type_check, value_check, value_desc) in _RULES.items():
        if field not in record:
            issues.append(
                RecordIssue(
                    field=field,
                    code=RecordErrorCode.MISSING_FIELD,
                    message=f"必填字段 {field} 缺失",
                    expected=type_desc,
                    actual=None,
                )
            )
            continue
        value = record[field]
        if not type_check(value):
            issues.append(_type_issue(field, type_desc, value))
            continue
        if not value_check(value):
            issues.append(_value_issue(field, value_desc, value))

    return issues


def validate_one(index: int, record: Any) -> RecordResult:
    """校验单条记录；非对象记录整体标记为类型不匹配。"""
    if not isinstance(record, dict):
        issues = [
            RecordIssue(
                field=RECORD_FIELD,
                code=RecordErrorCode.TYPE_MISMATCH,
                message="记录必须是 JSON 对象",
                expected="object",
                actual=type(record).__name__,
            )
        ]
        return RecordResult(index=index, valid=False, issues=issues)

    issues = _validate_object(record)
    # 同一字段一般只会产生一个问题；排序保证输出确定性。
    issues.sort(key=lambda i: (i.field, i.code.value))
    return RecordResult(index=index, valid=not issues, issues=issues)


def validate_records(records: list) -> ValidationResultData:
    """对整批记录逐条校验，返回与提交顺序无关的确定性结论。"""
    results = [validate_one(index, record) for index, record in enumerate(records)]
    invalid_count = sum(1 for r in results if not r.valid)
    issue_count = sum(len(r.issues) for r in results)
    summary = ValidationSummary(
        total=len(results),
        valid_count=len(results) - invalid_count,
        invalid_count=invalid_count,
        issue_count=issue_count,
    )
    return ValidationResultData(
        overall_valid=(invalid_count == 0),
        summary=summary,
        records=results,
    )
