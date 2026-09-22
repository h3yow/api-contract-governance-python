"""测试公共装置：带契约标识、判定依据与归一化结果的日志输出。"""

from __future__ import annotations

import logging

import pytest

from contract_governance import GovernanceLimits, GovernanceService


@pytest.fixture()
def svc() -> GovernanceService:
    return GovernanceService()


@pytest.fixture()
def log(caplog):
    """返回一个以 INFO 级别记录判定依据的日志器，单测日志可见。"""
    logger = logging.getLogger("governance.test")
    with caplog.at_level(logging.INFO, logger="governance.test"):
        yield logger


@pytest.fixture()
def limited_svc() -> GovernanceService:
    return GovernanceService(
        GovernanceLimits(
            max_contracts=2,
            max_fields_per_contract=3,
            max_validation_issues=2,
            expose_error_details=False,
        )
    )
