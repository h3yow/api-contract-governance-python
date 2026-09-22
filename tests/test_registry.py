"""注册 / 更新 / 撤销：非法版本流转与失败不污染已有状态。"""

from __future__ import annotations

import pytest

from contract_governance import (
    ContractSpec,
    FieldSpec,
    FieldType,
    GovernanceError,
    Reason,
)

SPEC_V1 = ContractSpec(fields=(FieldSpec("id", FieldType.INTEGER, required=True),))
SPEC_V2 = ContractSpec(
    fields=(
        FieldSpec("id", FieldType.INTEGER, required=True),
        FieldSpec("name", FieldType.STRING),
    )
)


def test_register_and_get(svc, log):
    c = svc.register("user-api", SPEC_V1)
    log.info("contract=%s action=register normalized_version=%s", c.contract_id, c.version)
    assert c.version == 1
    assert svc.get("user-api").spec == SPEC_V1


def test_duplicate_register_rejected_with_distinguishable_reason(svc, log):
    svc.register("user-api", SPEC_V1)
    with pytest.raises(GovernanceError) as exc:
        svc.register("user-api", SPEC_V1)
    log.info("contract=user-api reason=%s", exc.value.reason.value)
    assert exc.value.reason is Reason.CONTRACT_EXISTS


def test_illegal_version_transitions_rejected(svc, log):
    svc.register("user-api", SPEC_V1)
    for bad_version in (1, 3, 10, 0, -1):
        with pytest.raises(GovernanceError) as exc:
            svc.update("user-api", bad_version, SPEC_V2)
        log.info(
            "contract=user-api transition=1->%s reason=%s",
            bad_version,
            exc.value.reason.value,
        )
        assert exc.value.reason is Reason.ILLEGAL_VERSION_TRANSITION
    # 失败不污染：当前版本仍为 1，合法流转仍可进行。
    assert svc.get("user-api").version == 1
    assert svc.update("user-api", 2, SPEC_V2).version == 2


def test_update_unknown_contract_rejected(svc):
    with pytest.raises(GovernanceError) as exc:
        svc.update("ghost", 2, SPEC_V2)
    assert exc.value.reason is Reason.CONTRACT_NOT_FOUND


def test_revoke_semantics(svc, log):
    svc.register("user-api", SPEC_V1)
    revoked = svc.revoke("user-api")
    log.info("contract=%s action=revoke status=%s", revoked.contract_id, revoked.status.value)

    with pytest.raises(GovernanceError) as exc:
        svc.update("user-api", 2, SPEC_V2)
    assert exc.value.reason is Reason.CONTRACT_REVOKED

    with pytest.raises(GovernanceError) as exc:
        svc.revoke("user-api")
    assert exc.value.reason is Reason.CONTRACT_REVOKED

    with pytest.raises(GovernanceError) as exc:
        svc.register("user-api", SPEC_V1)
    # 撤销后的标识仍被占用，但原因与活跃重复注册可区分。
    assert exc.value.reason is Reason.CONTRACT_REVOKED
    log.info("contract=user-api re-register-after-revoke reason=%s", exc.value.reason.value)


def test_get_unknown_contract(svc):
    with pytest.raises(GovernanceError) as exc:
        svc.get("ghost")
    assert exc.value.reason is Reason.CONTRACT_NOT_FOUND


def test_invalid_spec_rejected_without_pollution(svc):
    dup_fields = ContractSpec(
        fields=(FieldSpec("a", FieldType.STRING), FieldSpec("a", FieldType.STRING))
    )
    with pytest.raises(GovernanceError) as exc:
        svc.register("bad", dup_fields)
    assert exc.value.reason is Reason.INVALID_CONTRACT
    with pytest.raises(GovernanceError) as exc:
        svc.get("bad")
    assert exc.value.reason is Reason.CONTRACT_NOT_FOUND
