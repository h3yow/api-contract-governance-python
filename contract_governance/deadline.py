"""单次校验的时间预算：在独立守护线程内执行，超时整体拒绝。

语义：
- 预算覆盖整次校验（含取快照与两侧字段检查），适用于单侧与成对校验。
- 超限时抛 VALIDATION_TIMEOUT，不返回半截结果，不泄漏内部细节。
- 每次调用使用独立线程：个别调用超时只阻塞自身等待，不会拖住其他请求；
  校验本身是纯读取操作，超时后残留的守护线程也不会改动注册表状态。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, TypeVar

from .errors import GovernanceError, Reason

T = TypeVar("T")

_TIMEOUT_MESSAGE = "validation exceeded its time budget"

# 预算占位哨兵：表示沿用 GovernanceLimits.validation_timeout_seconds 默认值，
# 与显式传入的 None / 0（不设预算）区分。
USE_DEFAULT_BUDGET: Any = object()


def run_with_budget(timeout_seconds: float | None, fn: Callable[[], T]) -> T:
    """在时间预算内执行 fn；超时或 timeout<=0（视为不设限）以外时正常返回。"""
    if not timeout_seconds or timeout_seconds <= 0:
        return fn()

    box: dict[str, object] = {}
    done = threading.Event()

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # 校验抛出的任何异常都原样交还调用方
            box["error"] = exc
        finally:
            done.set()

    worker = threading.Thread(target=target, name="validation-budget", daemon=True)
    worker.start()
    if not done.wait(timeout_seconds):
        raise GovernanceError(Reason.VALIDATION_TIMEOUT, _TIMEOUT_MESSAGE)
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box["value"]  # type: ignore[return-value]
