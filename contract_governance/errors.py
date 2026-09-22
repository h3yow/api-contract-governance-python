"""错误分类：每类错误带可区分的原因码，失败路径不泄漏内部细节。"""

from __future__ import annotations

import enum


class Reason(enum.Enum):
    """可归一化、可区分的失败原因码。"""

    CONTRACT_EXISTS = "contract_exists"
    CONTRACT_NOT_FOUND = "contract_not_found"
    CONTRACT_REVOKED = "contract_revoked"
    ILLEGAL_VERSION_TRANSITION = "illegal_version_transition"
    VERSION_MISMATCH = "version_mismatch"
    INVALID_CONTRACT = "invalid_contract"
    LIMIT_EXCEEDED = "limit_exceeded"
    VALIDATION_FAILED = "validation_failed"


class GovernanceError(Exception):
    """治理层统一异常：对外只暴露 reason 与安全消息。"""

    def __init__(self, reason: Reason, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
