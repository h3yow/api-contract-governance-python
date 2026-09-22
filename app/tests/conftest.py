"""测试夹具：每个测试使用独立注册表，并打印契约标识、判定依据与归一化结果。"""

from __future__ import annotations

import logging

import pytest

from app.registry import reset_registry


@pytest.fixture
def registry():
    return reset_registry()


@pytest.fixture(autouse=True)
def _contract_logger(caplog):
    caplog.set_level(logging.INFO, logger="contractgov")
    yield
