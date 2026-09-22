"""统一错误分类：非法参数 / 超限拒绝，二者不得混为一谈。"""

from __future__ import annotations


class AppError(Exception):
    """所有可向调用方解释的应用错误基类。"""

    error_code: str = "internal_error"
    category: str = "internal"
    status_code: int = 500

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ValidationRequestError(AppError):
    """请求参数非法（请求形态、幂等键、记录列表等不符合要求）。"""

    error_code = "invalid_request"
    category = "invalid_request"
    status_code = 422


class AdmissionRejectedError(AppError):
    """通过参数校验，但因并发执行上限等准入规则被明确拒绝（不排队）。"""

    error_code = "admission_rejected"
    category = "rejected"
    status_code = 503


class NotFoundError(AppError):
    """按幂等键查询时不存在任何已固化结论。"""

    error_code = "not_found"
    category = "not_found"
    status_code = 404
