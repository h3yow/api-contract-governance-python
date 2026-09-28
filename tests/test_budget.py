"""单次校验时间预算：单侧/成对都受限；超限整单拒绝、不留半截结果、不污染状态。"""

from __future__ import annotations

import threading

import pytest

from contract_governance import (
    ContractSpec,
    FieldSpec,
    FieldType,
    GovernanceError,
    GovernanceLimits,
    GovernanceService,
    Reason,
)
from contract_governance.budget import run_with_budget

# 字段级校验本身很廉价，用大字段规格在真实生产路径上制造足够计算量。
N = 200_000
BIG_LIMITS = GovernanceLimits(
    max_fields_per_contract=1_000_000,
    max_validation_issues=1_000_000,  # 不提前截断，保证计算量真实发生
    validation_timeout=0.005,
)
BIG_SPEC = ContractSpec(
    fields=tuple(FieldSpec(f"f{i}", FieldType.STRING, required=True) for i in range(N))
)
PAIR_SPEC = ContractSpec(
    request_fields=tuple(
        FieldSpec(f"r{i}", FieldType.STRING, required=True) for i in range(N)
    ),
    response_fields=tuple(
        FieldSpec(f"p{i}", FieldType.STRING, required=True) for i in range(N)
    ),
)


def test_cooperative_checkpoint_timeout(clock):
    # 直接验证执行器：假时钟推进后 checkpoint 协作式判定超时。
    state = {"done": False}

    def work(checkpoint):
        checkpoint()
        clock.advance(5.0)
        checkpoint()  # 越过 deadline
        state["done"] = True

    with pytest.raises(GovernanceError) as exc:
        run_with_budget(work, 1.0, clock=clock)
    assert exc.value.reason is Reason.BUDGET_EXCEEDED
    assert state["done"] is False


def test_hard_timeout_does_not_block_caller():
    # work 不配合检查点时，真实墙钟 join 兜底，调用方在预算后被释放。
    def work(checkpoint):
        import time
        time.sleep(1.0)

    with pytest.raises(GovernanceError) as exc:
        run_with_budget(work, 0.05)
    assert exc.value.reason is Reason.BUDGET_EXCEEDED


def test_timeout_message_hides_internals():
    def work(checkpoint):
        import time
        time.sleep(0.2)

    with pytest.raises(GovernanceError) as exc:
        run_with_budget(work, 0.02)
    # 只暴露通用安全文案，不含路径、类型等内部细节。
    assert exc.value.message == "validation rejected: time budget exceeded"


def test_single_validate_within_budget_unchanged():
    svc = GovernanceService(GovernanceLimits(validation_timeout=2.0))
    svc.register("a", ContractSpec(fields=(FieldSpec("id", FieldType.INTEGER, required=True),)))
    result = svc.validate("a", 1, {"id": 1})
    assert result.ok
    bad = svc.validate("a", 1, {})
    assert not bad.ok and bad.issues[0].code == "missing_required"


def test_single_validate_hard_timeout_rejected_and_no_state_left(log):
    svc = GovernanceService(BIG_LIMITS)
    svc.register("big", BIG_SPEC)
    with pytest.raises(GovernanceError) as exc:
        # 20 万个必填字段全缺，字段级校验足以越过 5ms 预算。
        svc.validate("big", 1, {})
    log.info("contract=big single-validate reason=%s", exc.value.reason.value)
    assert exc.value.reason is Reason.BUDGET_EXCEEDED
    assert exc.value.message == "validation rejected: time budget exceeded"

    # 不留半截结果、不污染注册状态：契约仍是版本 1，后续正常校验不受影响。
    current = svc.get("big")
    assert current.version == 1 and len(current.spec.fields) == N
    assert svc.validate(
            "big", 1, {f"f{i}": "x" for i in range(N)}, timeout=None
        ).ok


def test_pair_validate_hard_timeout_rejected_and_no_state_left(log):
    svc = GovernanceService(BIG_LIMITS)
    svc.register("big-pair", PAIR_SPEC)
    with pytest.raises(GovernanceError) as exc:
        svc.validate_pair("big-pair", 1, request={}, response={})
    log.info("contract=big-pair pair-validate reason=%s", exc.value.reason.value)
    assert exc.value.reason is Reason.BUDGET_EXCEEDED

    # 不能有“单侧已完成”的半截结果——异常即终态，注册状态不变。
    current = svc.get("big-pair")
    assert current.version == 1


def test_none_timeout_disables_budget():
    svc = GovernanceService(
        GovernanceLimits(
            max_fields_per_contract=1_000_000,
            max_validation_issues=1_000_000,
            validation_timeout=None,
        )
    )
    svc.register("big", BIG_SPEC)
    result = svc.validate("big", 1, {})
    assert not result.ok  # 预算关闭，再多字段也能跑完拿到完整结论。


def test_per_call_timeout_override():
    svc = GovernanceService(
        GovernanceLimits(
            max_fields_per_contract=1_000_000,
            max_validation_issues=1_000_000,
            validation_timeout=5.0,
        )
    )
    svc.register("big", BIG_SPEC)
    # 用极小的单次预算覆盖宽松的默认配置。
    with pytest.raises(GovernanceError) as exc:
        svc.validate("big", 1, {}, timeout=0.001)
    assert exc.value.reason is Reason.BUDGET_EXCEEDED


def test_one_timed_out_validation_does_not_block_others():
    # 慢调用在真实墙钟上越过预算（worker 独立、互不共享时钟），
    # 验证个别调用超时既不拖住调用方、也不影响并发的其他请求。
    svc = GovernanceService(BIG_LIMITS)
    svc.register("big", BIG_SPEC)
    svc.register(
        "small", ContractSpec(fields=(FieldSpec("id", FieldType.INTEGER, required=True),))
    )
    outcomes: list[str] = []
    start = threading.Event()
    lock = threading.Lock()

    def slow() -> None:
        start.wait()
        try:
            svc.validate("big", 1, {})
        except GovernanceError as e:
            with lock:
                outcomes.append(f"big:{e.reason.value}")

    def fast() -> None:
        start.wait()
        # 让出一拍，确保慢调用已先开跑并占住后台计算。
        import time
        time.sleep(0.05)
        t0 = time.monotonic()
        try:
            result = svc.validate("small", 1, {"id": 1})
            elapsed = time.monotonic() - t0
            with lock:
                outcomes.append(f"small:{'ok' if result.ok else 'bad'}")
                outcomes.append(f"small-elapsed:{elapsed:.3f}")
        except GovernanceError as e:  # pragma: no cover - 快请求不应超时
            with lock:
                outcomes.append(f"small:{e.reason.value}")

    threads = [threading.Thread(target=slow), threading.Thread(target=fast)]
    for t in threads:
        t.start()
    start.set()
    for t in threads:
        t.join(timeout=5.0)

    assert "big:budget_exceeded" in outcomes
    assert "small:ok" in outcomes
    # 快请求必须在自身预算内返回，没有被慢调用拖住。
    elapsed = float(
        next(o for o in outcomes if o.startswith("small-elapsed:")).split(":")[1]
    )
    assert elapsed < BIG_LIMITS.validation_timeout

    # 超时调用不影响已注册契约状态。
    assert svc.get("small").version == 1
    assert svc.get("big").version == 1
