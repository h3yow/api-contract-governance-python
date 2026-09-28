"""成对契约：请求/响应两侧分别声明形状，一次校验按侧归属，任一侧不过则整体不过。"""

from __future__ import annotations

import pytest

from contract_governance import (
    ContractSpec,
    FieldSpec,
    FieldType,
    GovernanceError,
    MISSING,
    Reason,
    Side,
)

PAIR_SPEC = ContractSpec(
    request_fields=(
        FieldSpec("id", FieldType.INTEGER, required=True),
        FieldSpec("name", FieldType.STRING),
    ),
    response_fields=(
        FieldSpec("code", FieldType.INTEGER, required=True),
        FieldSpec("ok", FieldType.BOOLEAN),
    ),
)

REQ_ONLY_SPEC = ContractSpec(
    request_fields=(FieldSpec("id", FieldType.INTEGER, required=True),)
)

LEGACY_SPEC = ContractSpec(fields=(FieldSpec("id", FieldType.INTEGER, required=True),))


def test_pair_validate_both_sides_ok(svc, log):
    svc.register("echo-api", PAIR_SPEC)
    result = svc.validate_pair(
        "echo-api", 1,
        request={"id": 1, "name": "a"},
        response={"code": 0, "ok": True},
    )
    log.info(
        "contract=%s normalized_ok=%s sides=%s",
        result.contract_id, result.ok,
        [(s.side.value, s.ok, tuple(i.code for i in s.issues)) for s in result.sides],
    )
    assert result.ok
    assert [s.side for s in result.sides] == [Side.REQUEST, Side.RESPONSE]
    assert all(s.ok and s.issues == () for s in result.sides)


def test_pair_issues_attributed_per_side_and_not_overwritten(svc, log):
    svc.register("echo-api", PAIR_SPEC)
    result = svc.validate_pair(
        "echo-api", 1,
        request={"name": 42},              # 请求侧：缺必填 + 类型错
        response={"code": "500", "ok": 1},  # 响应侧：类型错两个
    )
    by_side = {s.side: s for s in result.sides}
    log.info(
        "contract=echo-api request_issues=%s response_issues=%s",
        [(i.path, i.code) for i in by_side[Side.REQUEST].issues],
        [(i.path, i.code) for i in by_side[Side.RESPONSE].issues],
    )
    # 整体不过，且两侧各自的问题都在，互不覆盖。
    assert not result.ok
    assert not by_side[Side.REQUEST].ok
    assert not by_side[Side.RESPONSE].ok

    req_codes = {(i.path, i.code) for i in by_side[Side.REQUEST].issues}
    assert ("$.id", "missing_required") in req_codes
    assert ("$.name", "type_mismatch") in req_codes

    resp_codes = {(i.path, i.code) for i in by_side[Side.RESPONSE].issues}
    assert ("$.code", "type_mismatch") in resp_codes
    assert ("$.ok", "type_mismatch") in resp_codes

    # 归属清晰：请求侧问题不出现在响应侧，反之亦然。
    assert all(i.path.startswith("$.") for i in by_side[Side.REQUEST].issues)
    assert len(by_side[Side.REQUEST].issues) == 2
    assert len(by_side[Side.RESPONSE].issues) == 2


def test_pair_one_side_fails_overall_fails(svc):
    svc.register("echo-api", PAIR_SPEC)
    only_request_bad = svc.validate_pair(
        "echo-api", 1, request={}, response={"code": 200}
    )
    by_side = {s.side: s for s in only_request_bad.sides}
    assert not only_request_bad.ok
    assert not by_side[Side.REQUEST].ok
    assert by_side[Side.RESPONSE].ok

    only_response_bad = svc.validate_pair(
        "echo-api", 1, request={"id": 1}, response={"code": "x"}
    )
    by_side = {s.side: s for s in only_response_bad.sides}
    assert not only_response_bad.ok
    assert by_side[Side.REQUEST].ok
    assert not by_side[Side.RESPONSE].ok


def test_single_sided_pair_contract_only_requires_declared_side(svc):
    svc.register("req-only", REQ_ONLY_SPEC)
    ok = svc.validate_pair("req-only", 1, request={"id": 1})
    assert ok.ok and len(ok.sides) == 1
    assert ok.sides[0].side is Side.REQUEST

    bad = svc.validate_pair("req-only", 1, request={"id": "x"})
    assert not bad.ok and bad.sides[0].issues[0].code == "type_mismatch"


def test_pair_missing_or_undeclared_side_is_invalid_usage(svc):
    svc.register("echo-api", PAIR_SPEC)
    svc.register("req-only", REQ_ONLY_SPEC)

    # 漏交已声明的响应侧
    with pytest.raises(GovernanceError) as exc:
        svc.validate_pair("echo-api", 1, request={"id": 1})
    assert exc.value.reason is Reason.INVALID_CONTRACT

    # 给只声明请求侧的契约提交响应侧
    with pytest.raises(GovernanceError) as exc:
        svc.validate_pair("req-only", 1, request={"id": 1}, response={})
    assert exc.value.reason is Reason.INVALID_CONTRACT


def test_legacy_contract_keeps_single_validate(svc):
    svc.register("legacy", LEGACY_SPEC)
    assert svc.validate("legacy", 1, {"id": 1}).ok
    bad = svc.validate("legacy", 1, {})
    assert not bad.ok and bad.issues[0].code == "missing_required"

    # 老契约不能走成对入口
    with pytest.raises(GovernanceError) as exc:
        svc.validate_pair("legacy", 1, request={"id": 1}, response={})
    assert exc.value.reason is Reason.VALIDATION_SIDE_MISMATCH


def test_pair_contract_rejects_single_validate(svc, log):
    svc.register("echo-api", PAIR_SPEC)
    with pytest.raises(GovernanceError) as exc:
        svc.validate("echo-api", 1, {"id": 1})
    log.info("contract=echo-api single-on-paired reason=%s", exc.value.reason.value)
    assert exc.value.reason is Reason.VALIDATION_SIDE_MISMATCH


def test_pair_validate_version_mismatch_and_revoked(svc):
    svc.register("echo-api", PAIR_SPEC)
    with pytest.raises(GovernanceError) as exc:
        svc.validate_pair("echo-api", 2, request={}, response={})
    assert exc.value.reason is Reason.VERSION_MISMATCH

    svc.revoke("echo-api")
    with pytest.raises(GovernanceError) as exc:
        svc.validate_pair("echo-api", 1, request={}, response={})
    assert exc.value.reason is Reason.CONTRACT_REVOKED


def test_pair_explicit_none_payload_is_checked_not_treated_as_missing(svc):
    # 显式提交 None 属于“提交了非对象载荷”，而不是漏交侧。
    svc.register("req-only", REQ_ONLY_SPEC)
    result = svc.validate_pair("req-only", 1, request=None)
    assert not result.ok
    assert result.sides[0].issues[0].code == "not_an_object"

    # 完全不传则是漏交 -> 用法错误
    with pytest.raises(GovernanceError) as exc:
        svc.validate_pair("req-only", 1)
    assert exc.value.reason is Reason.INVALID_CONTRACT
    assert MISSING is not None
