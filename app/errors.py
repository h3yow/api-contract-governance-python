"""可区分的错误类型定义。"""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    """契约治理失败原因的稳定编码。"""

    INVALID_CONTRACT = "INVALID_CONTRACT"
    DUPLICATE_CONTRACT = "DUPLICATE_CONTRACT"
    NOT_FOUND = "NOT_FOUND"
    REVOKED = "REVOKED"
    VERSION_NOT_NEWER = "VERSION_NOT_NEWER"
    REVISION_CONFLICT = "REVISION_CONFLICT"
    VERSION_MISMATCH = "VERSION_MISMATCH"
    VERSION_NOT_ADJACENT = "VERSION_NOT_ADJACENT"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    INTERNAL = "INTERNAL"


class ContractError(Exception):
    """所有契约治理失败的基类。

    code 用于稳定归类，message 为安全的、可对外暴露的说明，
    details 仅在允许暴露错误详情时返回。
    """

    def __init__(self, code: ErrorCode, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
