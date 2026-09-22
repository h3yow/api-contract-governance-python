"""可配置上限：契约规模、校验耗时与错误详情暴露程度。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GovernanceLimits:
    """治理服务的可配置上限。

    - max_contracts: 注册表容量上限，超限拒绝注册。
    - max_fields_per_contract: 单契约字段数上限，超限拒绝注册/更新。
    - max_validation_issues: 单次校验最多返回的错误条数，超出部分截断。
    - expose_error_details: 是否对外暴露字段级错误详情；关闭时只返回原因码。
    """

    max_contracts: int = 1024
    max_fields_per_contract: int = 256
    max_validation_issues: int = 32
    expose_error_details: bool = True
