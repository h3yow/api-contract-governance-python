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


class FakeClock:
    """协作式假时钟：可显式推进，供时间预算测试确定地触发超时。"""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def clocked_svc(clock: FakeClock) -> GovernanceService:
    # 预算 1.0 秒，时钟由用例通过 clock 推进控制，不依赖真实调度时序。
    return GovernanceService(GovernanceLimits(validation_timeout=1.0), clock=clock)
