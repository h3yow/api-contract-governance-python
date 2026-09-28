"""单次校验的时间预算执行器：超限整单拒绝，不返回半截结果。

并发语义：
- 每次校验在独立守护线程内执行，单个调用超时不会拖住调用方线程，
  也不会影响其他并发校验或已注册契约状态（校验只读不可变快照）。
- 超限时抛 BUDGET_EXCEEDED，消息为通用安全文案，不泄漏内部细节。
- 校验函数可通过注入的 checkpoint 协作式检查截止时间，保证
  超时判定确定、无需依赖线程调度时序。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TypeVar

from .errors import GovernanceError, Reason

T = TypeVar("T")

# 超时对外只暴露通用文案，不泄漏内部执行细节。
_BUDGET_EXCEEDED_MESSAGE = "validation rejected: time budget exceeded"


def run_with_budget(
    work: Callable[[Callable[[], None]], T],
    timeout: float | None,
    *,
    clock: Callable[[], float] | None = None,
) -> T:
    """在时间预算内执行 ``work(checkpoint)``。

    - ``timeout`` 为 None 时不设预算：在调用方线程内直接执行，
      ``checkpoint`` 为空操作。
    - 否则在独立守护线程内执行，``checkpoint`` 会对照
      ``clock()`` 给出的截止时间做协作式检查，超时立即抛
      ``BUDGET_EXCEEDED``；同时以真实墙钟 ``join(timeout)`` 兜底，
      即使 work 不配合检查，调用方也不会被拖住。
    - work 抛出的任何异常原样透传给调用方。
    """
    if timeout is None:
        return work(lambda: None)

    now = clock or time.monotonic
    deadline = now() + timeout

    def checkpoint() -> None:
        if now() >= deadline:
            raise GovernanceError(Reason.BUDGET_EXCEEDED, _BUDGET_EXCEEDED_MESSAGE)

    box: dict[str, object] = {}

    def target() -> None:
        try:
            box["value"] = work(checkpoint)
        except BaseException as exc:  # 全部带回调用方线程抛出，线程内不留隐患
            box["error"] = exc

    worker = threading.Thread(target=target, name="contract-validation", daemon=True)
    worker.start()
    worker.join(max(timeout, 0.0))
    if worker.is_alive():
        # 硬超时：worker 仍在运行（守护线程，不阻塞进程退出），
        # 不读取 box、不返回半截结果。
        raise GovernanceError(Reason.BUDGET_EXCEEDED, _BUDGET_EXCEEDED_MESSAGE)

    error = box.get("error")
    if isinstance(error, BaseException):
        raise error
    return box["value"]  # type: ignore[return-value]
