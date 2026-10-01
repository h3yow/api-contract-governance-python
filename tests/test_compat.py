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


def test_legacy_single_side_contract_has_no_response_report(svc):
    # 老的单侧契约：只有请求侧结论，response 为 None，每条变更归属请求侧。
    spec_v2 = ContractSpec(fields=(FieldSpec("id", FieldType.INTEGER, required=True),))
    report = _register_two_versions(svc, spec_v2)
    assert report.response is None
    assert report.request.verdict is Verdict.BREAKING
    assert all(c.side.value == "request" for c in report.changes)


def test_paired_response_side_change_reflected_in_query(svc, log):
    v1 = ContractSpec(
        fields=(FieldSpec("id", FieldType.INTEGER, required=True),),
        response_fields=(
            FieldSpec("code", FieldType.INTEGER, required=True),
            FieldSpec("ok", FieldType.BOOLEAN),
        ),
    )
    # 请求侧不动；响应侧删掉必填 code、ok 改成必填。
    v2 = ContractSpec(
        fields=v1.fields,
        response_fields=(FieldSpec("ok", FieldType.BOOLEAN, required=True),),
    )
    svc.register("pair-api", v1)
    svc.update("pair-api", 2, v2)
    report = svc.check_compatibility("pair-api", 1, 2)
    log.info(
        "contract=pair-api request=%s response=%s basis=%s",
        report.request.verdict.value,
        report.response.verdict.value,
        [(c.kind.value, c.side.value, c.field_name) for c in report.changes],
    )
    assert report.verdict is Verdict.BREAKING
    assert report.request.verdict is Verdict.COMPATIBLE
    assert report.response.verdict is Verdict.BREAKING
    resp_changes = report.response.changes
    assert {c.field_name for c in resp_changes} == {"code", "ok"}
    assert {c.kind for c in resp_changes} == {
        ChangeKind.FIELD_REMOVED,
        ChangeKind.REQUIRED_EXPANDED,
    }
    assert all(c.side.value == "response" for c in resp_changes)
    # 请求侧无任何变化。
    assert report.request.changes == ()
