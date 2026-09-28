"""成对契约的兼容性判定：两侧形状逐侧比较，变更按侧归属，模式不可混用。"""

from __future__ import annotations

import pytest

from contract_governance import (
    ChangeKind,
    ContractSpec,
    FieldSpec,
    FieldType,
    GovernanceError,
    Reason,
    Side,
    Verdict,
)

P1 = ContractSpec(
    request_fields=(
        FieldSpec("id", FieldType.INTEGER, required=True),
        FieldSpec("name", FieldType.STRING),
    ),
    response_fields=(
        FieldSpec("code", FieldType.INTEGER, required=True),
        FieldSpec("body", FieldType.STRING),
    ),
)


def test_paired_breaking_change_attributed_per_side(svc, log):
    # 请求侧：id 类型收窄（破坏性）；响应侧：删除 body（破坏性）。
    p2 = ContractSpec(
        request_fields=(FieldSpec("id", FieldType.STRING, required=True),),
        response_fields=(FieldSpec("code", FieldType.INTEGER, required=True),),
    )
    svc.register("echo-api", P1)
    svc.update("echo-api", 2, p2)
    report = svc.check_compatibility("echo-api", 1, 2)

    log.info(
        "contract=echo-api verdict=%s changes=%s",
        report.verdict.value,
        [(c.kind.value, c.field_name, c.side.value if c.side else None) for c in report.changes],
    )
    assert report.verdict is Verdict.BREAKING

    by_identity = {(c.kind, c.field_name, c.side) for c in report.changes}
    assert (ChangeKind.TYPE_NARROWED, "id", Side.REQUEST) in by_identity
    assert (ChangeKind.FIELD_REMOVED, "name", Side.REQUEST) in by_identity
    assert (ChangeKind.FIELD_REMOVED, "body", Side.RESPONSE) in by_identity


def test_paired_compatible_change(svc):
    p2 = ContractSpec(
        request_fields=(
            FieldSpec("id", FieldType.NUMBER, required=True),  # 放宽
            FieldSpec("name", FieldType.STRING),
            FieldSpec("trace", FieldType.STRING),  # 新增可选
        ),
        response_fields=(
            FieldSpec("code", FieldType.INTEGER, required=True),
            FieldSpec("body", FieldType.STRING),
        ),
    )
    svc.register("echo-api", P1)
    svc.update("echo-api", 2, p2)
    report = svc.check_compatibility("echo-api", 1, 2)
    assert report.verdict is Verdict.COMPATIBLE
    assert all(c.side in (Side.REQUEST, Side.RESPONSE) for c in report.changes)
    # 报告按（侧、字段名）有序、可复现。
    order = [(c.side, c.field_name) for c in report.changes]
    assert order == sorted(order)
    assert svc.check_compatibility("echo-api", 1, 2) == report


def test_mixed_shape_mode_rejected(svc):
    legacy = ContractSpec(fields=(FieldSpec("id", FieldType.INTEGER, required=True),))
    svc.register("mix", legacy)
    svc.update("mix", 2, P1)
    with pytest.raises(GovernanceError) as exc:
        svc.check_compatibility("mix", 1, 2)
    assert exc.value.reason is Reason.INVALID_CONTRACT


def test_paired_publish_gate_uses_per_side_judgement(svc):
    # 门禁与判定一致：任一侧的破坏性变更都要显式确认。
    p2_breaking = ContractSpec(
        request_fields=(FieldSpec("id", FieldType.INTEGER, required=True),),
        response_fields=(FieldSpec("code", FieldType.INTEGER, required=True),),
    )
    svc.register("echo-api", P1)
    with pytest.raises(GovernanceError) as exc:
        svc.publish("echo-api", 2, p2_breaking)
    assert exc.value.reason is Reason.BREAKING_CHANGE_REJECTED

    published = svc.publish("echo-api", 2, p2_breaking, accept_breaking=True)
    assert published.version == 2
    decision = svc.get_publish_decision("echo-api", 2)
    assert decision.verdict is Verdict.BREAKING and decision.breaking_accepted
