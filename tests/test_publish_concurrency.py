"""同一目标版本并发发布：无论是否确认破坏性变更，都只能有一个成功落库。"""

from __future__ import annotations

import threading

from contract_governance import (
    ContractSpec,
    FieldSpec,
    FieldType,
    GovernanceError,
    Reason,
)

V1 = ContractSpec(
    fields=(
        FieldSpec("id", FieldType.INTEGER, required=True),
        FieldSpec("name", FieldType.STRING),
    )
)


def _breaking_spec(tag: int) -> ContractSpec:
    # 删除 name -> 破坏性；保留 tag 字段以区分胜出者写入的是哪份定义。
    return ContractSpec(
        fields=(
            FieldSpec("id", FieldType.INTEGER, required=True),
            FieldSpec(f"win{tag}", FieldType.STRING),
        )
    )


def _run_concurrent_publish(svc, accept: bool, workers: int = 24) -> list[str]:
    outcomes: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(workers)

    def worker(tag: int) -> None:
        barrier.wait()  # 尽量把所有提交挤到同一时刻
        try:
            svc.publish("user-api", 2, _breaking_spec(tag), accept_breaking=accept)
            with lock:
                outcomes.append(f"ok:{tag}")
        except GovernanceError as e:
            with lock:
                outcomes.append(e.reason.value)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return outcomes


def test_concurrent_publish_with_acceptance_exactly_one_wins(svc, log):
    svc.register("user-api", V1)
    outcomes = _run_concurrent_publish(svc, accept=True)

    winners = [o for o in outcomes if o.startswith("ok:")]
    losers = [o for o in outcomes if o == Reason.ILLEGAL_VERSION_TRANSITION.value]
    log.info("accept=true winners=%s losers=%s", winners, len(losers))

    assert len(winners) == 1
    assert len(losers) == 23

    current = svc.get("user-api")
    assert current.version == 2
    # 胜出者的定义完整保留，没有被落败方覆盖。
    winner_tag = winners[0].split(":")[1]
    field_names = {f.name for f in current.spec.fields}
    assert f"win{winner_tag}" in field_names

    # 门禁记录只对应真正落库的那一次。
    decision = svc.get_publish_decision("user-api", 2)
    assert decision.breaking_accepted is True
    assert svc.get_version("user-api", 2).spec == current.spec


def test_concurrent_publish_without_acceptance_none_persists(svc, log):
    svc.register("user-api", V1)
    outcomes = _run_concurrent_publish(svc, accept=False)

    rejected = [o for o in outcomes if o == Reason.BREAKING_CHANGE_REJECTED.value]
    log.info("accept=false breaking-rejected=%s others=%s", rejected, set(outcomes))

    # 全部判定为破坏性、默认拒绝：没有任何一次落库。
    assert set(outcomes) == {Reason.BREAKING_CHANGE_REJECTED.value}
    assert len(rejected) == 24
    assert svc.get("user-api").version == 1

    from contract_governance import GovernanceError as GE
    try:
        svc.get_publish_decision("user-api", 2)
        raise AssertionError("decision should not exist")
    except GE as e:
        assert e.reason is Reason.DECISION_NOT_FOUND


def test_concurrent_publish_compatible_exactly_one_wins(svc):
    svc.register("user-api", V1)

    def compat_spec(tag: int) -> ContractSpec:
        return ContractSpec(
            fields=(
                FieldSpec("id", FieldType.NUMBER, required=True),
                FieldSpec("name", FieldType.STRING),
                FieldSpec(f"extra{tag}", FieldType.STRING),
            )
        )

    workers = 24
    outcomes: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(workers)

    def worker(tag: int) -> None:
        barrier.wait()
        try:
            svc.publish("user-api", 2, compat_spec(tag))
            with lock:
                outcomes.append(f"ok:{tag}")
        except GovernanceError as e:
            with lock:
                outcomes.append(e.reason.value)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [o for o in outcomes if o.startswith("ok:")]
    losers = [o for o in outcomes if o == Reason.ILLEGAL_VERSION_TRANSITION.value]
    assert len(winners) == 1
    assert len(losers) == 23
    assert svc.get("user-api").version == 2
