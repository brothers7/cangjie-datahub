"""统一业务异常与错误码。"""
from __future__ import annotations


class AppError(Exception):
    code: str = "INTERNAL_ERROR"
    http_status: int = 500

    def __init__(self, message: str = "", *, code: str | None = None, http_status: int | None = None):
        super().__init__(message or self.__class__.code)
        self.message = message or self.__class__.code
        if code:
            self.code = code
        if http_status:
            self.http_status = http_status


class NotFoundError(AppError):
    code = "NOT_FOUND"
    http_status = 404


class AuthError(AppError):
    code = "UNAUTHORIZED"
    http_status = 401


class PermissionDenied(AppError):
    code = "FORBIDDEN"
    http_status = 403


class ValidationError(AppError):
    code = "INVALID_PARAMS"
    http_status = 422


class DatasourceError(AppError):
    code = "DATASOURCE_ERROR"
    http_status = 400


class LLMError(AppError):
    code = "LLM_ERROR"
    http_status = 502


class SQLGuardError(AppError):
    code = "SQL_GUARD_FAILED"
    http_status = 400


class KGConstraintError(AppError):
    code = "KG_CONSTRAINT_VIOLATION"
    http_status = 400
