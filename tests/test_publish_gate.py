"""版本发布门禁：破坏性变更默认拒绝、显式接受才落库，判定依据可复查。"""

from __future__ import annotations

import pytest

from contract_governance import (
    ChangeKind,
    ContractSpec,
    FieldSpec,
    FieldType,
    GovernanceError,
    Reason,
    Verdict,
)

V1 = ContractSpec(
    fields=(
        FieldSpec("id", FieldType.INTEGER, required=True),
        FieldSpec("name", FieldType.STRING),
    )
)
COMPATIBLE_V2 = ContractSpec(
    fields=(
        FieldSpec("id", FieldType.NUMBER, required=True),  # integer -> number 放宽
        FieldSpec("name", FieldType.STRING),
        FieldSpec("nickname", FieldType.STRING),  # 新增可选
    )
)
BREAKING_V2 = ContractSpec(
    fields=(FieldSpec("id", FieldType.INTEGER, required=True),)  # 删除了 name
)


def test_compatible_change_publishes_without_confirmation(svc, log):
    svc.register("user-api", V1)
    published = svc.publish("user-api", 2, COMPATIBLE_V2)
    log.info(
        "contract=%s action=publish version=%s",
        published.contract_id, published.version,
    )
    assert published.version == 2

    decision = svc.get_publish_decision("user-api", 2)
    assert decision.verdict is Verdict.COMPATIBLE
    assert decision.breaking_accepted is False
    kinds = {c.kind for c in decision.changes}
    assert ChangeKind.TYPE_WIDENED in kinds
    assert ChangeKind.FIELD_ADDED_OPTIONAL in kinds


def test_breaking_change_rejected_by_default_and_not_persisted(svc, log):
    svc.register("user-api", V1)
    with pytest.raises(GovernanceError) as exc:
        svc.publish("user-api", 2, BREAKING_V2)
    log.info("contract=user-api breaking-default reason=%s", exc.value.reason.value)
    assert exc.value.reason is Reason.BREAKING_CHANGE_REJECTED

    # 未确认：版本不落库、门禁记录不落库，当前版本仍是 1。
    assert svc.get("user-api").version == 1
    with pytest.raises(GovernanceError) as query_exc:
        svc.get_publish_decision("user-api", 2)
    assert query_exc.value.reason is Reason.DECISION_NOT_FOUND

    # 拒绝消息不泄漏内部字段细节之外的结构，只含通用确认指引。
    assert "accept_breaking" in exc.value.message


def test_breaking_change_persisted_after_explicit_acceptance(svc, log):
    svc.register("user-api", V1)
    published = svc.publish("user-api", 2, BREAKING_V2, accept_breaking=True)
    assert published.version == 2
    assert svc.get("user-api").version == 2

    decision = svc.get_publish_decision("user-api", 2)
    log.info(
        "contract=user-api decision verdict=%s accepted=%s basis=%s",
        decision.verdict.value,
        decision.breaking_accepted,
        [(c.kind.value, c.field_name) for c in decision.changes],
    )
    assert decision.verdict is Verdict.BREAKING
    assert decision.breaking_accepted is True
    removed = {c.field_name for c in decision.changes if c.kind is ChangeKind.FIELD_REMOVED}
    assert removed == {"name"}

    # 判定可复现：重复查询结果一致，与兼容性判定入口结论一致。
    assert svc.get_publish_decision("user-api", 2) == decision
    report = svc.check_compatibility("user-api", 1, 2)
    assert report.verdict is decision.verdict
    assert report.changes == decision.changes


def test_rejected_breaking_does_not_block_later_compatible_publish(svc):
    svc.register("user-api", V1)
    with pytest.raises(GovernanceError) as exc:
        svc.publish("user-api", 2, BREAKING_V2)
    assert exc.value.reason is Reason.BREAKING_CHANGE_REJECTED

    # 失败不留状态：仍可发布一个兼容的 v2。
    published = svc.publish("user-api", 2, COMPATIBLE_V2)
    assert published.version == 2
    decision = svc.get_publish_decision("user-api", 2)
    assert decision.verdict is Verdict.COMPATIBLE


def test_gate_distinguishes_breaking_from_illegal_transitions(svc, log):
    svc.register("user-api", V1)
    svc.publish("user-api", 2, COMPATIBLE_V2)

    for bad_version, label in ((2, "重复"), (4, "跳号"), (1, "回退")):
        with pytest.raises(GovernanceError) as exc:
            svc.publish("user-api", bad_version, BREAKING_V2, accept_breaking=True)
        log.info(
            "contract=user-api transition=2->%s(%s) reason=%s",
            bad_version, label, exc.value.reason.value,
        )
        assert exc.value.reason is Reason.ILLEGAL_VERSION_TRANSITION


def test_acceptance_flag_only_applies_to_breaking(svc):
    svc.register("user-api", V1)
    svc.publish("user-api", 2, COMPATIBLE_V2, accept_breaking=True)
    decision = svc.get_publish_decision("user-api", 2)
    # 兼容变更即使带了确认标记，也如实记录为未发生破坏性确认。
    assert decision.verdict is Verdict.COMPATIBLE
    assert decision.breaking_accepted is False


def test_publish_on_unknown_or_revoked_contract(svc):
    with pytest.raises(GovernanceError) as exc:
        svc.publish("ghost", 1, V1)
    assert exc.value.reason is Reason.CONTRACT_NOT_FOUND

    svc.register("user-api", V1)
    svc.revoke("user-api")
    with pytest.raises(GovernanceError) as exc:
        svc.publish("user-api", 2, BREAKING_V2, accept_breaking=True)
    assert exc.value.reason is Reason.CONTRACT_REVOKED
