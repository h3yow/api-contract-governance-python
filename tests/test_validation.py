"""运行时校验：未注册 / 已撤销 / 版本不匹配被区分，失败不污染状态。"""

from __future__ import annotations

import pytest

from contract_governance import (
    ContractSpec,
    FieldSpec,
    FieldType,
    GovernanceError,
    Reason,
)

SPEC = ContractSpec(
    fields=(
        FieldSpec("id", FieldType.INTEGER, required=True),
        FieldSpec("name", FieldType.STRING),
        FieldSpec("active", FieldType.BOOLEAN),
    )
)


def test_validate_ok(svc, log):
    svc.register("user-api", SPEC)
    result = svc.validate("user-api", 1, {"id": 1, "name": "a", "active": True})
    log.info(
        "contract=%s version=%s normalized_ok=%s issues=%s",
        result.contract_id, result.version, result.ok, list(result.issues),
    )
    assert result.ok and result.issues == ()


def test_validate_unregistered(svc):
    with pytest.raises(GovernanceError) as exc:
        svc.validate("ghost", 1, {})
    assert exc.value.reason is Reason.CONTRACT_NOT_FOUND


def test_validate_revoked_rejected(svc, log):
    svc.register("user-api", SPEC)
    svc.revoke("user-api")
    with pytest.raises(GovernanceError) as exc:
        svc.validate("user-api", 1, {"id": 1})
    log.info("contract=user-api validate-after-revoke reason=%s", exc.value.reason.value)
    assert exc.value.reason is Reason.CONTRACT_REVOKED


def test_validate_version_mismatch(svc, log):
    svc.register("user-api", SPEC)
    svc.update("user-api", 2, SPEC)
    with pytest.raises(GovernanceError) as exc:
        svc.validate("user-api", 1, {"id": 1})
    log.info("contract=user-api requested=1 registered=2 reason=%s", exc.value.reason.value)
    assert exc.value.reason is Reason.VERSION_MISMATCH


def test_validate_failure_reports_issues_and_does_not_pollute(svc, log):
    svc.register("user-api", SPEC)
    bad = svc.validate("user-api", 1, {"name": 42, "active": 1})
    log.info(
        "contract=%s normalized_ok=%s issues=%s",
        bad.contract_id, bad.ok, [(i.path, i.code) for i in bad.issues],
    )
    assert not bad.ok
    codes = {(i.path, i.code) for i in bad.issues}
    assert ("$.id", "missing_required") in codes
    assert ("$.name", "type_mismatch") in codes
    assert ("$.active", "type_mismatch") in codes

    # 失败不污染：注册状态不变，后续合法请求照常通过。
    assert svc.get("user-api").version == 1
    ok = svc.validate("user-api", 1, {"id": 7})
    assert ok.ok


def test_non_object_payload_rejected(svc):
    svc.register("user-api", SPEC)
    result = svc.validate("user-api", 1, [1, 2, 3])
    assert not result.ok
    assert result.issues[0].code == "not_an_object"
