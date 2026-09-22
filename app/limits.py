"""可配置上限与超限策略。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class LimitAction(str, Enum):
    REJECT = "REJECT"
    TRUNCATE = "TRUNCATE"


@dataclass(frozen=True)
class LimitConfig:
    max_fields: int = 128
    max_versions: int = 64
    max_payload_nodes: int = 10_000
    max_validation_ms: int = 1_000
    max_error_details: int = 10
    expose_internal_details: bool = False
    on_limit_exceeded: LimitAction = LimitAction.REJECT
