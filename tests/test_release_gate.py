"""版本发布门禁：破坏性默认拒绝 / 显式接受，留档可查，并发仅一成。"""

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
    Verdict,
)

V1 = ContractSpec(
    fields=(
        FieldSpec("id", FieldType.INTEGER, required=True),
        FieldSpec("name", FieldType.STRING),
    )
)

# 删除字段 -> 破坏性。
V2_BREAKING = ContractSpec(fields=(FieldSpec("id", FieldType.INTEGER, required=True),))

# 新增可选字段 -> 兼容。
V2_COMPATIBLE = ContractSpec(
    fields=(
        FieldSpec("id", FieldType.INTEGER, required=True),
        FieldSpec("name", FieldType.STRING),
        FieldSpec("nick", FieldType.STRING),
    )
)


def test_compatible_release_allowed_and_recorded(svc, log):
    svc.register("api", V1)
    decision = svc.publish("api", 2, V2_COMPATIBLE)
    log.info(
        "contract=api release=1->2 verdict=%s allowed=%s",
        decision.verdict.value, decision.allowed,
    )
    assert decision.allowed
    assert decision.verdict is Verdict.COMPATIBLE
    assert decision.accepted_breaking is False
    assert svc.get("api").version == 2
    record = svc.get_release_record("api", 2)
    assert record == decision
    assert any(
        c.kind is ChangeKind.FIELD_ADDED_OPTIONAL and c.field_name == "nick"
        for c in record.changes
    )


def test_breaking_release_rejected_by_default(svc, log):
    svc.register("api", V1)
    with pytest.raises(GovernanceError) as exc:
        svc.publish("api", 2, V2_BREAKING)
    log.info(
        "contract=api breaking-default reason=%s msg=%s",
        exc.value.reason.value, exc.value.message,
    )
    assert exc.value.reason is Reason.BREAKING_CHANGE_REJECTED
    # 拒绝不落库、不留档。
    assert svc.get("api").version == 1
    with pytest.raises(GovernanceError) as rec_exc:
        svc.get_release_record("api", 2)
    assert rec_exc.value.reason is Reason.RELEASE_RECORD_NOT_FOUND


def test_breaking_reason_distinct_from_illegal_transitions(svc, log):
    svc.register("api", V1)
    for bad in (1, 3, 10, 0, -1):
        with pytest.raises(GovernanceError) as exc:
            svc.publish("api", bad, V2_BREAKING)
        log.info("contract=api transition=1->%s reason=%s", bad, exc.value.reason.value)
        assert exc.value.reason is Reason.ILLEGAL_VERSION_TRANSITION


def test_breaking_release_allowed_after_explicit_acceptance(svc, log):
    svc.register("api", V1)
    decision = svc.publish("api", 2, V2_BREAKING, accept_breaking=True)
    log.info(
        "contract=api accepted-release verdict=%s accepted=%s basis=%s",
        decision.verdict.value,
        decision.accepted_breaking,
        [(c.kind.value, c.field_name) for c in decision.changes],
    )
    assert decision.allowed
    assert decision.verdict is Verdict.BREAKING
    assert decision.accepted_breaking is True
    assert svc.get("api").version == 2
    record = svc.get_release_record("api", 2)
    assert record.verdict is Verdict.BREAKING
    assert record.accepted_breaking is True
    removed = {c.field_name for c in record.changes if c.kind is ChangeKind.FIELD_REMOVED}
    assert removed == {"name"}


def test_acceptance_flag_ignored_for_compatible_release(svc):
    svc.register("api", V1)
    decision = svc.publish("api", 2, V2_COMPATIBLE, accept_breaking=True)
    assert decision.verdict is Verdict.COMPATIBLE
    # 不是破坏性发布，确认结果不得记为接受过破坏性变更。
    assert decision.accepted_breaking is False


def test_rejected_breaking_release_leaves_no_state(svc):
    svc.register("api", V1)
    with pytest.raises(GovernanceError):
        svc.publish("api", 2, V2_BREAKING)
    # 拒绝后仍可提交一个兼容版本。
    ok = svc.publish("api", 2, V2_COMPATIBLE)
    assert ok.allowed and svc.get("api").version == 2


def test_concurrent_publish_same_target_exactly_one_wins_with_acceptance(svc, log):
    svc.register("api", V1)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        try:
            d = svc.publish("api", 2, V2_BREAKING, accept_breaking=True)
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
    log.info("concurrent-accepted-publish winners=%s losers=%s", winners, len(losers))
    assert len(winners) == 1
    assert len(losers) == 15
    assert svc.get("api").version == 2
    # 唯一成功者的确认与判定依据留档一次。
    record = svc.get_release_record("api", 2)
    assert record.accepted_breaking is True


def test_concurrent_publish_same_target_without_acceptance_none_persists(svc, log):
    svc.register("api", V1)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            svc.publish("api", 2, V2_BREAKING)
            with lock:
                outcomes.append("ok")
        except GovernanceError as e:
            with lock:
                outcomes.append(e.reason.value)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    log.info("concurrent-unaccepted-publish outcomes=%s", sorted(set(outcomes)))
    assert "ok" not in outcomes
    assert all(o == Reason.BREAKING_CHANGE_REJECTED.value for o in outcomes)
    assert svc.get("api").version == 1
    with pytest.raises(GovernanceError):
        svc.get_release_record("api", 2)
