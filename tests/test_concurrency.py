"""并发写入：无半更新、无覆盖丢失；读取方只见完整快照。"""

from __future__ import annotations

import threading

from contract_governance import (
    ContractSpec,
    FieldSpec,
    FieldType,
    GovernanceError,
    Reason,
)


def _spec(tag: int) -> ContractSpec:
    return ContractSpec(fields=(FieldSpec(f"f{tag}", FieldType.STRING),))


def test_concurrent_registers_no_lost_updates(svc, log):
    errors: list[GovernanceError] = []

    def worker(i: int) -> None:
        try:
            svc.register(f"contract-{i}", _spec(i))
        except GovernanceError as e:  # pragma: no cover - 容量足够时不应发生
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    for i in range(32):
        c = svc.get(f"contract-{i}")
        assert c.version == 1 and c.spec.fields[0].name == f"f{i}"
    log.info("concurrent-register count=32 lost_updates=0")


def test_concurrent_updates_exactly_one_wins_per_version(svc, log):
    svc.register("user-api", _spec(0))
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(tag: int) -> None:
        try:
            svc.update("user-api", 2, _spec(tag))
            with lock:
                outcomes.append(f"ok:{tag}")
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
    log.info(
        "contract=user-api concurrent-update winners=%s losers=%s",
        winners, len(losers),
    )
    # 恰有一个写入成功，其余全部被归类为非法版本流转，无半更新。
    assert len(winners) == 1
    assert len(losers) == 15
    current = svc.get("user-api")
    assert current.version == 2
    expected_field = f"f{winners[0].split(':')[1]}"
    assert current.spec.fields[0].name == expected_field


def test_concurrent_readers_never_see_partial_writes(svc, log):
    svc.register("user-api", _spec(0))
    stop = threading.Event()
    anomalies: list[str] = []

    def reader() -> None:
        while not stop.is_set():
            c = svc.get("user-api")
            # 快照必须自洽：version N 的契约必须带 N 个字段。
            if len(c.spec.fields) != c.version:
                anomalies.append(f"v{c.version} fields={len(c.spec.fields)}")

    def writer() -> None:
        for v in range(2, 22):
            spec = ContractSpec(
                fields=tuple(FieldSpec(f"f{i}", FieldType.STRING) for i in range(v))
            )
            svc.update("user-api", v, spec)
        stop.set()

    readers = [threading.Thread(target=reader) for _ in range(8)]
    for r in readers:
        r.start()
    writer()
    for r in readers:
        r.join()

    log.info("contract=user-api anomalies=%s", anomalies)
    assert anomalies == []
    assert svc.get("user-api").version == 21
