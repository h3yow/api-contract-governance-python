"""成对校验：请求/响应两侧问题分别归属、合并不互相覆盖；时间预算拒绝。"""

from __future__ import annotations

import threading
import time

import pytest

from contract_governance import (
    ContractSpec,
    FieldSpec,
    FieldType,
    GovernanceError,
    GovernanceLimits,
    Reason,
    Side,
)
from contract_governance.validation import ContractValidator
from contract_governance.registry import ContractRegistry

PAIRED = ContractSpec(
    fields=(
        FieldSpec("id", FieldType.INTEGER, required=True),
        FieldSpec("name", FieldType.STRING),
    ),
    response_fields=(
        FieldSpec("code", FieldType.INTEGER, required=True),
        FieldSpec("ok", FieldType.BOOLEAN),
    ),
)

LEGACY = ContractSpec(fields=(FieldSpec("id", FieldType.INTEGER, required=True),))


def test_pair_both_sides_ok(svc):
    svc.register("api", PAIRED)
    result = svc.validate_pair(
        "api", 1, {"id": 1, "name": "a"}, {"code": 200, "ok": True}
    )
    assert result.ok
    assert result.request.ok and result.response.ok
    assert result.side(Side.REQUEST).issues == ()
    assert result.side(Side.RESPONSE).issues == ()


def test_pair_request_issue_does_not_cover_response(svc, log):
    svc.register("api", PAIRED)
    result = svc.validate_pair("api", 1, {"id": "bad"}, {"code": 200, "ok": True})
    log.info(
        "contract=api pair_ok=%s request_issues=%s response_issues=%s",
        result.ok,
        [i.code for i in result.request.issues],
        [i.code for i in result.response.issues],
    )
    assert not result.ok
    assert not result.request.ok
    assert result.response.ok  # 响应侧结论不被请求侧问题覆盖
    assert result.request.issues[0].code == "type_mismatch"


def test_pair_response_issue_does_not_cover_request(svc):
    svc.register("api", PAIRED)
    result = svc.validate_pair(
        "api", 1, {"id": 1}, {"code": "x", "ok": False}
    )
    assert not result.ok
    assert result.request.ok
    assert not result.response.ok
    assert result.response.issues[0].path == "$.code"


def test_pair_both_sides_fail_issues_are_attributed(svc):
    svc.register("api", PAIRED)
    result = svc.validate_pair("api", 1, {}, {})
    assert not result.ok
    req_codes = {i.path for i in result.request.issues}
    resp_codes = {i.path for i in result.response.issues}
    assert req_codes == {"$.id"}
    assert resp_codes == {"$.code"}
    # 两侧问题互不混入。
    assert "$.code" not in req_codes and "$.id" not in resp_codes


def test_pair_requires_paired_contract(svc):
    svc.register("legacy", LEGACY)
    with pytest.raises(GovernanceError) as exc:
        svc.validate_pair("legacy", 1, {"id": 1}, {})
    assert exc.value.reason is Reason.INVALID_CONTRACT
    # 老契约单侧用法不受影响。
    assert svc.validate("legacy", 1, {"id": 1}).ok


def test_legacy_single_shape_contract_validates_as_before(svc):
    svc.register("legacy", LEGACY)
    bad = svc.validate("legacy", 1, {})
    assert not bad.ok and bad.issues[0].code == "missing_required"


class _SleepyValidator(ContractValidator):
    """模拟超长校验：每个字段检查前先睡，用于确定性触发预算超时。"""

    def __init__(self, registry, limits, delay):
        super().__init__(registry, limits)
        self._delay = delay

    def _check_fields(self, fields, payload, deadline):
        time.sleep(self._delay)
        return super()._check_fields(fields, payload, deadline)


def test_single_validation_timeout_rejected_without_partial_result(svc, log):
    svc.register("legacy", LEGACY)
    reg = svc._registry  # 复用同一份注册数据
    sleepy = _SleepyValidator(reg, GovernanceLimits(), delay=0.05)
    with pytest.raises(GovernanceError) as exc:
        sleepy.validate("legacy", 1, {}, timeout_seconds=0.01)
    log.info("contract=legacy single timeout reason=%s", exc.value.reason.value)
    assert exc.value.reason is Reason.VALIDATION_TIMEOUT
    # 超时消息不泄漏内部细节（无堆栈/字段名）。
    assert "legacy" not in exc.value.message
    # 已注册状态不受影响。
    assert svc.get("legacy").version == 1
    assert svc.validate("legacy", 1, {"id": 1}).ok


def test_pair_validation_timeout_rejects_whole_call(svc, log):
    svc.register("api", PAIRED)
    sleepy = _SleepyValidator(svc._registry, GovernanceLimits(), delay=0.05)
    with pytest.raises(GovernanceError) as exc:
        sleepy.validate_pair("api", 1, {"id": 1}, {"code": 1}, timeout_seconds=0.01)
    log.info("contract=api pair timeout reason=%s", exc.value.reason.value)
    assert exc.value.reason is Reason.VALIDATION_TIMEOUT
    # 无半截结果：直接以异常拒绝，注册状态不变。
    assert svc.get("api").version == 1


def test_timeout_does_not_block_or_pollute_concurrent_validations(svc, log):
    svc.register("api", PAIRED)
    svc.register("legacy", LEGACY)
    sleepy = _SleepyValidator(svc._registry, GovernanceLimits(), delay=0.2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def slow_call() -> None:
        try:
            sleepy.validate_pair("api", 1, {"id": 1}, {"code": 1}, timeout_seconds=0.02)
        except GovernanceError as e:
            with lock:
                outcomes.append(f"slow:{e.reason.value}")

    def fast_call(i: int) -> None:
        r = svc.validate_pair("api", 1, {"id": i}, {"code": 200})
        with lock:
            outcomes.append("fast" if r.ok else "fast-fail")

    threads = [threading.Thread(target=slow_call)]
    threads += [threading.Thread(target=fast_call, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    log.info("concurrent-validation outcomes=%s", outcomes)
    assert outcomes.count("slow:validation_timeout") == 1
    assert outcomes.count("fast") == 8
    assert svc.get("api").version == 1
    assert svc.get("legacy").version == 1


def test_within_budget_results_unchanged(svc):
    svc.register("api", PAIRED)
    # 显式给足预算，结果与不带预算一致。
    r = svc.validate_pair(
        "api", 1, {"id": 1}, {"code": 1}, timeout_seconds=2.0
    )
    assert r.ok
