"""成对契约发布门禁：响应侧破坏性改动同样被拦截，分侧结论可查证。"""

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

# 成对契约 v1：请求侧与响应侧各自声明形状。
PAIR_V1 = ContractSpec(
    fields=(
        FieldSpec("id", FieldType.INTEGER, required=True),
        FieldSpec("name", FieldType.STRING),
    ),
    response_fields=(
        FieldSpec("ticket", FieldType.STRING, required=True),
        FieldSpec("score", FieldType.NUMBER),
        FieldSpec("note", FieldType.STRING),
    ),
)

# 仅响应侧破坏性：删掉必填响应字段 ticket。
RESP_ONLY_REMOVED = ContractSpec(
    fields=PAIR_V1.fields,
    response_fields=(
        FieldSpec("score", FieldType.NUMBER),
        FieldSpec("note", FieldType.STRING),
    ),
)

# 仅响应侧破坏性：响应字段类型收窄 number -> integer。
RESP_ONLY_NARROWED = ContractSpec(
    fields=PAIR_V1.fields,
    response_fields=(
        FieldSpec("ticket", FieldType.STRING, required=True),
        FieldSpec("score", FieldType.INTEGER),
        FieldSpec("note", FieldType.STRING),
    ),
)

# 仅响应侧破坏性：可选响应字段 note 改为必填。
RESP_ONLY_REQUIRED = ContractSpec(
    fields=PAIR_V1.fields,
    response_fields=(
        FieldSpec("ticket", FieldType.STRING, required=True),
        FieldSpec("score", FieldType.NUMBER),
        FieldSpec("note", FieldType.STRING, required=True),
    ),
)

# 仅响应侧兼容：新增可选响应字段。
RESP_ONLY_COMPATIBLE = ContractSpec(
    fields=PAIR_V1.fields,
    response_fields=PAIR_V1.response_fields + (FieldSpec("extra", FieldType.STRING),),
)

# 双侧同时破坏性：请求侧删字段 + 响应侧删字段。
BOTH_SIDES_BREAKING = ContractSpec(
    fields=(FieldSpec("id", FieldType.INTEGER, required=True),),
    response_fields=(
        FieldSpec("ticket", FieldType.STRING, required=True),
        FieldSpec("score", FieldType.NUMBER),
    ),
)


@pytest.mark.parametrize(
    ("spec_v2", "kind", "field"),
    [
        (RESP_ONLY_REMOVED, ChangeKind.FIELD_REMOVED, "ticket"),
        (RESP_ONLY_NARROWED, ChangeKind.TYPE_NARROWED, "score"),
        (RESP_ONLY_REQUIRED, ChangeKind.REQUIRED_EXPANDED, "note"),
    ],
    ids=["removed", "narrowed", "required-expanded"],
)
def test_response_side_breaking_rejected_by_default(svc, log, spec_v2, kind, field):
    svc.register("pair-api", PAIR_V1)
    with pytest.raises(GovernanceError) as exc:
        svc.publish("pair-api", 2, spec_v2)
    log.info(
        "contract=pair-api response-breaking reason=%s msg=%s",
        exc.value.reason.value, exc.value.message,
    )
    # 与跳号/回退/重复的 illegal_version_transition 明确区分。
    assert exc.value.reason is Reason.BREAKING_CHANGE_REJECTED
    # 拒绝不落库、不留档。
    assert svc.get("pair-api").version == 1
    with pytest.raises(GovernanceError) as rec_exc:
        svc.get_release_record("pair-api", 2)
    assert rec_exc.value.reason is Reason.RELEASE_RECORD_NOT_FOUND
    # 兼容性查询能看到响应侧的变化及其侧别。
    svc.update("pair-api", 2, spec_v2)
    report = svc.check_compatibility("pair-api", 1, 2)
    assert report.verdict is Verdict.BREAKING
    assert report.request_verdict is Verdict.COMPATIBLE
    assert report.response_verdict is Verdict.BREAKING
    hit = [c for c in report.changes if c.kind is kind and c.field_name == field]
    assert len(hit) == 1
    assert hit[0].side is Side.RESPONSE


@pytest.mark.parametrize(
    ("spec_v2", "kind", "field"),
    [
        (RESP_ONLY_REMOVED, ChangeKind.FIELD_REMOVED, "ticket"),
        (RESP_ONLY_NARROWED, ChangeKind.TYPE_NARROWED, "score"),
        (RESP_ONLY_REQUIRED, ChangeKind.REQUIRED_EXPANDED, "note"),
    ],
    ids=["removed", "narrowed", "required-expanded"],
)
def test_response_side_breaking_allowed_after_explicit_acceptance(
    svc, log, spec_v2, kind, field
):
    svc.register("pair-api", PAIR_V1)
    decision = svc.publish("pair-api", 2, spec_v2, accept_breaking=True)
    log.info(
        "contract=pair-api accepted response-breaking verdict=%s req=%s resp=%s",
        decision.verdict.value,
        decision.request_verdict.value,
        decision.response_verdict.value if decision.response_verdict else None,
    )
    assert decision.allowed
    assert decision.verdict is Verdict.BREAKING
    assert decision.accepted_breaking is True
    # 分侧结论：破坏性只发生在响应侧，请求侧保持兼容，互不掩盖。
    assert decision.request_verdict is Verdict.COMPATIBLE
    assert decision.response_verdict is Verdict.BREAKING
    assert svc.get("pair-api").version == 2
    # 判定依据与接受结果可按版本重新查到，且与实际接受一致。
    record = svc.get_release_record("pair-api", 2)
    assert record == decision
    assert record.accepted_breaking is True
    assert record.request_verdict is Verdict.COMPATIBLE
    assert record.response_verdict is Verdict.BREAKING
    hit = [c for c in record.changes if c.kind is kind and c.field_name == field]
    assert len(hit) == 1
    assert hit[0].side is Side.RESPONSE


def test_response_side_compatible_change_allowed(svc):
    svc.register("pair-api", PAIR_V1)
    decision = svc.publish("pair-api", 2, RESP_ONLY_COMPATIBLE)
    assert decision.allowed
    assert decision.verdict is Verdict.COMPATIBLE
    assert decision.request_verdict is Verdict.COMPATIBLE
    assert decision.response_verdict is Verdict.COMPATIBLE
    assert decision.accepted_breaking is False
    added = [c for c in decision.changes if c.kind is ChangeKind.FIELD_ADDED_OPTIONAL]
    assert len(added) == 1 and added[0].side is Side.RESPONSE


def test_both_sides_breaking_neither_masked(svc, log):
    svc.register("pair-api", PAIR_V1)
    decision = svc.publish("pair-api", 2, BOTH_SIDES_BREAKING, accept_breaking=True)
    log.info(
        "contract=pair-api both-sides basis=%s",
        [(c.side.value if c.side else None, c.kind.value, c.field_name) for c in decision.changes],
    )
    assert decision.request_verdict is Verdict.BREAKING
    assert decision.response_verdict is Verdict.BREAKING
    removed = {(c.side, c.field_name) for c in decision.changes if c.kind is ChangeKind.FIELD_REMOVED}
    # 两侧的删除都出现在判定依据里，谁也不掩盖谁。
    assert (Side.REQUEST, "name") in removed
    assert (Side.RESPONSE, "note") in removed


def test_response_side_breaking_reason_distinct_from_illegal_transition(svc):
    svc.register("pair-api", PAIR_V1)
    # 非法流转仍然是 illegal_version_transition。
    with pytest.raises(GovernanceError) as exc:
        svc.publish("pair-api", 3, RESP_ONLY_REMOVED)
    assert exc.value.reason is Reason.ILLEGAL_VERSION_TRANSITION
    # 相邻版本但响应侧破坏性是 breaking_change_rejected。
    with pytest.raises(GovernanceError) as exc2:
        svc.publish("pair-api", 2, RESP_ONLY_REMOVED)
    assert exc2.value.reason is Reason.BREAKING_CHANGE_REJECTED


def test_concurrent_response_side_breaking_publish_exactly_one_wins(svc, log):
    svc.register("pair-api", PAIR_V1)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            d = svc.publish("pair-api", 2, RESP_ONLY_REMOVED, accept_breaking=True)
            with lock:
                outcomes.append(f"ok:{d.accepted_breaking}")
        except GovernanceError as e:
            with lock:
                outcomes.append(e.reason.value)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [o for o in outcomes if o.startswith("ok:")]
    log.info("concurrent response-side publish winners=%s", winners)
    assert winners == ["ok:True"]
    assert outcomes.count(Reason.ILLEGAL_VERSION_TRANSITION.value) == 15
    assert svc.get("pair-api").version == 2
    record = svc.get_release_record("pair-api", 2)
    assert record.accepted_breaking is True
    assert record.response_verdict is Verdict.BREAKING
