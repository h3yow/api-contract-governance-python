"""兼容性判定：破坏性变更被标记、兼容变更不误判、结论可复现。"""

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
        FieldSpec("age", FieldType.INTEGER),
    )
)


def _register_two_versions(svc, spec_v2):
    svc.register("user-api", V1)
    svc.update("user-api", 2, spec_v2)
    return svc.check_compatibility("user-api", 1, 2)


def test_field_removed_is_breaking(svc, log):
    spec_v2 = ContractSpec(fields=(FieldSpec("id", FieldType.INTEGER, required=True),))
    report = _register_two_versions(svc, spec_v2)
    log.info(
        "contract=%s verdict=%s basis=%s",
        report.contract_id,
        report.verdict.value,
        [(c.kind.value, c.field_name) for c in report.changes],
    )
    assert report.verdict is Verdict.BREAKING
    kinds = {c.kind for c in report.changes}
    assert ChangeKind.FIELD_REMOVED in kinds
    removed = {c.field_name for c in report.changes if c.kind is ChangeKind.FIELD_REMOVED}
    assert removed == {"name", "age"}


def test_type_narrowing_is_breaking(svc, log):
    spec_v2 = ContractSpec(
        fields=(
            FieldSpec("id", FieldType.STRING, required=True),
            FieldSpec("name", FieldType.STRING),
            FieldSpec("age", FieldType.INTEGER),
        )
    )
    report = _register_two_versions(svc, spec_v2)
    log.info("contract=%s verdict=%s", report.contract_id, report.verdict.value)
    assert report.verdict is Verdict.BREAKING
    assert any(
        c.kind is ChangeKind.TYPE_NARROWED and c.field_name == "id" for c in report.changes
    )


def test_required_expansion_is_breaking(svc, log):
    spec_v2 = ContractSpec(
        fields=(
            FieldSpec("id", FieldType.INTEGER, required=True),
            FieldSpec("name", FieldType.STRING, required=True),  # 可选 -> 必填
            FieldSpec("age", FieldType.INTEGER),
            FieldSpec("email", FieldType.STRING, required=True),  # 新增必填
        )
    )
    report = _register_two_versions(svc, spec_v2)
    log.info("contract=%s verdict=%s", report.contract_id, report.verdict.value)
    assert report.verdict is Verdict.BREAKING
    expanded = {
        c.field_name for c in report.changes if c.kind is ChangeKind.REQUIRED_EXPANDED
    }
    assert expanded == {"name", "email"}


def test_compatible_changes_not_misjudged(svc, log):
    spec_v2 = ContractSpec(
        fields=(
            FieldSpec("id", FieldType.NUMBER, required=True),  # integer -> number 放宽
            FieldSpec("name", FieldType.STRING),
            FieldSpec("age", FieldType.INTEGER),  # 不变
            FieldSpec("nickname", FieldType.STRING),  # 新增可选
        )
    )
    # 把 id 从必填放宽为可选也是兼容变更。
    spec_v2_relaxed = ContractSpec(
        fields=tuple(
            FieldSpec(f.name, f.type, False) if f.name == "id" else f
            for f in spec_v2.fields
        )
    )
    report = _register_two_versions(svc, spec_v2_relaxed)
    log.info(
        "contract=%s verdict=%s basis=%s",
        report.contract_id,
        report.verdict.value,
        [(c.kind.value, c.field_name) for c in report.changes],
    )
    assert report.verdict is Verdict.COMPATIBLE
    kinds = {c.kind for c in report.changes}
    assert kinds == {
        ChangeKind.TYPE_WIDENED,
        ChangeKind.REQUIRED_RELAXED,
        ChangeKind.FIELD_ADDED_OPTIONAL,
    }


def test_verdict_is_reproducible(svc, log):
    spec_v2 = ContractSpec(fields=(FieldSpec("id", FieldType.STRING, required=True),))
    first = _register_two_versions(svc, spec_v2)
    for _ in range(5):
        again = svc.check_compatibility("user-api", 1, 2)
        assert again == first
    log.info(
        "contract=%s reproducible verdict=%s normalized=%s",
        first.contract_id,
        first.verdict.value,
        [(c.kind.value, c.field_name) for c in first.changes],
    )


def test_non_adjacent_versions_rejected(svc):
    svc.register("user-api", V1)
    svc.update("user-api", 2, V1)
    svc.update("user-api", 3, V1)
    with pytest.raises(GovernanceError) as exc:
        svc.check_compatibility("user-api", 1, 3)
    assert exc.value.reason is Reason.ILLEGAL_VERSION_TRANSITION
