"""成对契约发布门禁：请求侧/响应侧分别判定，任一侧破坏性即拦住发布。

覆盖：
- 只在响应侧发生破坏性改动（删必填字段 / 类型收窄 / 可选变必填）默认拒绝；
- 显式 accept_breaking 后放行，落库留档且两侧结论可按版本复查；
- 两侧分别判定，请求侧结论不被响应侧问题掩盖，反之亦然；
- 兼容性查询能反映响应侧变化（含侧别归属）；
- 破坏性拒绝原因与跳号/回退/重复等非法流转原因区分；
- 同一目标版本并发提交（含响应侧破坏性）恰有一个成功。
"""

from __future__ import annotations

import threading

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

V1 = ContractSpec(
    fields=(
        FieldSpec("id", FieldType.INTEGER, required=True),
        FieldSpec("name", FieldType.STRING),
    ),
    response_fields=(
        FieldSpec("code", FieldType.INTEGER, required=True),
        FieldSpec("ok", FieldType.BOOLEAN),
    ),
)

# 请求侧完全不变；响应侧删掉必填字段 code -> 破坏性（只在响应侧）。
V2_RESPONSE_REMOVED = ContractSpec(
    fields=V1.fields,
    response_fields=(FieldSpec("ok", FieldType.BOOLEAN),),
)

# 请求侧完全不变；响应侧 code 类型收窄 integer -> string -> 破坏性（只在响应侧）。
V2_RESPONSE_NARROWED = ContractSpec(
    fields=V1.fields,
    response_fields=(
        FieldSpec("code", FieldType.STRING, required=True),
        FieldSpec("ok", FieldType.BOOLEAN),
    ),
)

# 请求侧完全不变；响应侧可选字段 ok 改成必填 -> 破坏性（只在响应侧）。
V2_RESPONSE_REQUIRED = ContractSpec(
    fields=V1.fields,
    response_fields=(
        FieldSpec("code", FieldType.INTEGER, required=True),
        FieldSpec("ok", FieldType.BOOLEAN, required=True),
    ),
)

# 两侧都兼容：请求侧新增可选字段，响应侧新增可选字段。
V2_BOTH_COMPATIBLE = ContractSpec(
    fields=V1.fields + (FieldSpec("nick", FieldType.STRING),),
    response_fields=V1.response_fields + (FieldSpec("trace", FieldType.STRING),),
)


def _assert_change_side(changes, kind, field_name, side):
    assert any(
        c.kind is kind and c.field_name == field_name and c.side is side
        for c in changes
    ), [(c.kind.value, c.side.value, c.field_name) for c in changes]


@pytest.mark.parametrize(
    "spec_v2, kind, field_name",
    [
        (V2_RESPONSE_REMOVED, ChangeKind.FIELD_REMOVED, "code"),
        (V2_RESPONSE_NARROWED, ChangeKind.TYPE_NARROWED, "code"),
        (V2_RESPONSE_REQUIRED, ChangeKind.REQUIRED_EXPANDED, "ok"),
    ],
)
def test_response_only_breaking_rejected_by_default(svc, log, spec_v2, kind, field_name):
    svc.register("pair-api", V1)
    with pytest.raises(GovernanceError) as exc:
        svc.publish("pair-api", 2, spec_v2)
    log.info(
        "contract=pair-api response-breaking kind=%s reason=%s msg=%s",
        kind.value, exc.value.reason.value, exc.value.message,
    )
    assert exc.value.reason is Reason.BREAKING_CHANGE_REJECTED
    # 拒绝原因要点名是响应侧，且与非法版本流转原因区分。
    assert Side.RESPONSE.value in exc.value.message
    assert Side.REQUEST.value not in exc.value.message
    # 默认拒绝不落库、不留档。
    assert svc.get("pair-api").version == 1
    with pytest.raises(GovernanceError) as rec_exc:
        svc.get_release_record("pair-api", 2)
    assert rec_exc.value.reason is Reason.RELEASE_RECORD_NOT_FOUND


def test_response_only_breaking_visible_in_compatibility_query(svc, log):
    svc.register("pair-api", V1)
    # 直接 update 落库 v2（不经门禁），再查相邻版本兼容性。
    svc.update("pair-api", 2, V2_RESPONSE_REMOVED)
    report = svc.check_compatibility("pair-api", 1, 2)
    log.info(
        "contract=pair-api verdict=%s request=%s response=%s basis=%s",
        report.verdict.value,
        report.request.verdict.value,
        report.response.verdict.value,
        [(c.kind.value, c.side.value, c.field_name) for c in report.changes],
    )
    assert report.verdict is Verdict.BREAKING
    # 两侧分别给出结论：请求侧无变化 -> 兼容；响应侧删必填 -> 破坏性。
    assert report.request.verdict is Verdict.COMPATIBLE
    assert report.response.verdict is Verdict.BREAKING
    # 兼容性查询能查出响应侧的变化，且按侧归属、不混到请求侧。
    _assert_change_side(report.changes, ChangeKind.FIELD_REMOVED, "code", Side.RESPONSE)
    assert all(c.side is Side.RESPONSE for c in report.changes)
    assert report.response.changes and report.request.changes == ()


def test_response_only_breaking_allowed_after_explicit_acceptance(svc, log):
    svc.register("pair-api", V1)
    decision = svc.publish("pair-api", 2, V2_RESPONSE_REMOVED, accept_breaking=True)
    log.info(
        "contract=pair-api accepted request_verdict=%s response_verdict=%s accepted=%s basis=%s",
        decision.request_verdict.value,
        decision.response_verdict.value,
        decision.accepted_breaking,
        [(c.kind.value, c.side.value, c.field_name) for c in decision.changes],
    )
    assert decision.allowed
    assert decision.verdict is Verdict.BREAKING
    assert decision.request_verdict is Verdict.COMPATIBLE
    assert decision.response_verdict is Verdict.BREAKING
    assert decision.accepted_breaking is True
    assert svc.get("pair-api").version == 2

    # 判定依据与接受结果可按目标版本重新查到，且与实际是否接受一致。
    record = svc.get_release_record("pair-api", 2)
    assert record == decision
    assert record.verdict is Verdict.BREAKING
    assert record.response_verdict is Verdict.BREAKING
    assert record.request_verdict is Verdict.COMPATIBLE
    assert record.accepted_breaking is True
    _assert_change_side(record.changes, ChangeKind.FIELD_REMOVED, "code", Side.RESPONSE)


def test_two_sides_judged_independently_request_breaking_not_masked(svc):
    # 请求侧删必填字段（破坏性），响应侧新增可选字段（兼容）。
    spec_v2 = ContractSpec(
        fields=(FieldSpec("name", FieldType.STRING),),
        response_fields=V1.response_fields + (FieldSpec("trace", FieldType.STRING),),
    )
    svc.register("pair-api", V1)
    with pytest.raises(GovernanceError) as exc:
        svc.publish("pair-api", 2, spec_v2)
    assert exc.value.reason is Reason.BREAKING_CHANGE_REJECTED
    assert Side.REQUEST.value in exc.value.message
    assert Side.RESPONSE.value not in exc.value.message
    # 不经门禁落库，两侧结论都应可查、互不掩盖。
    svc.update("pair-api", 2, spec_v2)
    report = svc.check_compatibility("pair-api", 1, 2)
    assert report.request.verdict is Verdict.BREAKING
    assert report.response.verdict is Verdict.COMPATIBLE
    _assert_change_side(report.changes, ChangeKind.FIELD_REMOVED, "id", Side.REQUEST)
    _assert_change_side(
        report.changes, ChangeKind.FIELD_ADDED_OPTIONAL, "trace", Side.RESPONSE
    )


def test_both_sides_breaking_each_side_recorded(svc):
    # 两侧各删一个必填字段：整体拒绝，两侧都各自被认定为破坏性。
    spec_v2 = ContractSpec(
        fields=(FieldSpec("name", FieldType.STRING),),
        response_fields=(FieldSpec("ok", FieldType.BOOLEAN),),
    )
    svc.register("pair-api", V1)
    with pytest.raises(GovernanceError) as exc:
        svc.publish("pair-api", 2, spec_v2)
    assert exc.value.reason is Reason.BREAKING_CHANGE_REJECTED
    assert Side.REQUEST.value in exc.value.message and Side.RESPONSE.value in exc.value.message
    svc.update("pair-api", 2, spec_v2)
    report = svc.check_compatibility("pair-api", 1, 2)
    assert report.request.verdict is Verdict.BREAKING
    assert report.response.verdict is Verdict.BREAKING
    _assert_change_side(report.changes, ChangeKind.FIELD_REMOVED, "id", Side.REQUEST)
    _assert_change_side(report.changes, ChangeKind.FIELD_REMOVED, "code", Side.RESPONSE)


def test_both_sides_compatible_release_allowed(svc):
    svc.register("pair-api", V1)
    decision = svc.publish("pair-api", 2, V2_BOTH_COMPATIBLE)
    assert decision.allowed
    assert decision.verdict is Verdict.COMPATIBLE
    assert decision.request_verdict is Verdict.COMPATIBLE
    assert decision.response_verdict is Verdict.COMPATIBLE
    assert decision.accepted_breaking is False
    # 即使带了 accept_breaking，兼容发布也不得记为接受过破坏性变更。
    from contract_governance import GovernanceService

    other = GovernanceService()
    other.register("pair-api", V1)
    other_decision = other.publish("pair-api", 2, V2_BOTH_COMPATIBLE, accept_breaking=True)
    assert other_decision.accepted_breaking is False
    _assert_change_side(
        decision.changes, ChangeKind.FIELD_ADDED_OPTIONAL, "nick", Side.REQUEST
    )
    _assert_change_side(
        decision.changes, ChangeKind.FIELD_ADDED_OPTIONAL, "trace", Side.RESPONSE
    )


def test_response_breaking_illegal_transition_still_distinct(svc):
    svc.register("pair-api", V1)
    # 即使新版本是响应侧破坏性，跳号/回退/重复仍报非法流转，而非门禁拒绝。
    for bad in (1, 3, 10, 0, -1):
        with pytest.raises(GovernanceError) as exc:
            svc.publish("pair-api", bad, V2_RESPONSE_REMOVED)
        assert exc.value.reason is Reason.ILLEGAL_VERSION_TRANSITION
    assert svc.get("pair-api").version == 1


def test_legacy_to_paired_with_required_response_field_is_breaking(svc):
    # 老的单侧契约 -> 成对契约且新增必填响应字段：响应侧按破坏性拦住发布。
    legacy = ContractSpec(fields=(FieldSpec("id", FieldType.INTEGER, required=True),))
    paired = ContractSpec(
        fields=legacy.fields,
        response_fields=(FieldSpec("ok", FieldType.BOOLEAN, required=True),),
    )
    svc.register("legacy-api", legacy)
    with pytest.raises(GovernanceError) as exc:
        svc.publish("legacy-api", 2, paired)
    assert exc.value.reason is Reason.BREAKING_CHANGE_REJECTED
    assert Side.RESPONSE.value in exc.value.message
    decision = svc.publish("legacy-api", 2, paired, accept_breaking=True)
    assert decision.request_verdict is Verdict.COMPATIBLE
    assert decision.response_verdict is Verdict.BREAKING


def test_concurrent_response_breaking_publish_exactly_one_wins(svc, log):
    svc.register("pair-api", V1)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        try:
            d = svc.publish("pair-api", 2, V2_RESPONSE_REMOVED, accept_breaking=True)
            with lock:
                outcomes.append(f"ok:{i}:{d.accepted_breaking}")
        except GovernanceError as e:
            with lock:
                outcomes.append(e.reason.value)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [o for o in outcomes if o.startswith("ok:")]
    losers = [o for o in outcomes if o == Reason.ILLEGAL_VERSION_TRANSITION.value]
    log.info("pair-concurrent-accepted winners=%s losers=%s", winners, len(losers))
    assert len(winners) == 1
    assert len(losers) == 15
    assert svc.get("pair-api").version == 2
    record = svc.get_release_record("pair-api", 2)
    assert record.accepted_breaking is True
    assert record.response_verdict is Verdict.BREAKING
