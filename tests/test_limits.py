"""可配置上限：超限明确拒绝，失败路径不泄漏细节、不残留状态。"""

from __future__ import annotations

import pytest

from contract_governance import (
    ContractSpec,
    FieldSpec,
    FieldType,
    GovernanceError,
    Reason,
)


def test_max_contracts_limit(limited_svc, log):
    limited_svc.register("a", ContractSpec())
    limited_svc.register("b", ContractSpec())
    with pytest.raises(GovernanceError) as exc:
        limited_svc.register("c", ContractSpec())
    log.info("contract=c reason=%s", exc.value.reason.value)
    assert exc.value.reason is Reason.LIMIT_EXCEEDED
    # 不残留部分状态：c 不存在，a/b 完好。
    with pytest.raises(GovernanceError):
        limited_svc.get("c")
    assert limited_svc.get("a").version == 1


def test_max_fields_limit(limited_svc, log):
    spec = ContractSpec(
        fields=tuple(FieldSpec(f"f{i}", FieldType.STRING) for i in range(4))
    )
    with pytest.raises(GovernanceError) as exc:
        limited_svc.register("big", spec)
    log.info("contract=big fields=4 limit=3 reason=%s", exc.value.reason.value)
    assert exc.value.reason is Reason.LIMIT_EXCEEDED
    with pytest.raises(GovernanceError) as exc:
        limited_svc.get("big")
    assert exc.value.reason is Reason.CONTRACT_NOT_FOUND


def test_validation_issues_truncated(limited_svc, log):
    spec = ContractSpec(
        fields=tuple(FieldSpec(f"r{i}", FieldType.STRING, required=True) for i in range(3))
    )
    limited_svc.register("svc", spec)
    result = limited_svc.validate("svc", 1, {})
    log.info(
        "contract=svc normalized_ok=%s issue_count=%s",
        result.ok, len(result.issues),
    )
    assert not result.ok
    # 上限 2 条 + 1 条截断标记。
    assert len(result.issues) == 3
    assert result.issues[-1].code == "issues_truncated"


def test_error_details_hidden_when_configured(limited_svc, log):
    spec = ContractSpec(fields=(FieldSpec("secret", FieldType.STRING, required=True),))
    limited_svc.register("svc", spec)
    result = limited_svc.validate("svc", 1, {})
    log.info("contract=svc issues=%s", [(i.code, i.message) for i in result.issues])
    assert not result.ok
    for issue in result.issues:
        assert "secret" not in issue.message
        assert issue.message == "detail hidden by configuration"
