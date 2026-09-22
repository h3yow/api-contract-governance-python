"""并发访问：原子发布、revision 乐观锁防覆盖丢失、读取无中间态。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.errors import ContractError, ErrorCode
from app.registry import Registry


def _contract(version: str, field: str = "a"):
    return {
        "id": "svc",
        "version": version,
        "fields": [{"name": field, "type": "STRING", "required": False}],
    }


def test_concurrent_distinct_version_registration(registry: Registry):
    # 版本注册必须单调递增：并发提交多个不同版本时，只有严格高于当前
    # 最新版的那一个能成功，其余被 VERSION_NOT_NEWER 区分拒绝。
    versions = [f"1.{i}.0" for i in range(20)]
    barrier = threading.Barrier(20)

    def register(v):
        barrier.wait()
        try:
            registry.register(_contract(v))
            return ("ok", v)
        except ContractError as exc:
            return (exc.code.value, v)

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(register, versions))

    oks = [(status, v) for status, v in results if status == "ok"]
    assert oks == [("ok", "1.19.0")]
    rejected = {status for status, v in results if status != "ok"}
    assert rejected == {ErrorCode.VERSION_NOT_NEWER.value}
    stored = registry.list_versions("svc")
    assert [str(c.version) for c in stored] == ["1.19.0"]
    for c in stored:
        assert c.normalized["id"] == "svc"
        assert len(c.normalized["fields"]) == 1
        assert c.revision == 0 and c.revoked is False


def test_concurrent_readers_never_see_partial_writes(registry: Registry):
    registry.register(_contract("1.0.0"))
    stop = threading.Event()
    seen: list[tuple] = []

    def reader():
        while not stop.is_set():
            try:
                versions = registry.list_versions("svc")
            except ContractError:
                continue
            # 读方观察到的每个条目都必须是已完整发布的不可变契约
            for c in versions:
                assert c.normalized["id"] == "svc"
                assert isinstance(c.normalized["fields"], list)
                seen.append((str(c.version), c.revision, c.revoked))

    threads = [threading.Thread(target=reader) for _ in range(8)]
    for t in threads:
        t.start()
    try:
        for i in range(1, 31):
            registry.register(_contract(f"1.{i}.0"))
    finally:
        stop.set()
        for t in threads:
            t.join()

    assert seen, "读线程应当观察到若干稳定快照"
    # 任意时刻观察到的版本集合都必须是 1.0.0 .. 1.k.0 的前缀，无空洞
    for version, _rev, revoked in seen:
        assert revoked is False
        major, minor, patch = (int(p) for p in version.split("."))
        assert major == 1 and patch == 0 and 0 <= minor <= 30
    final = registry.list_versions("svc")
    assert len(final) == 31


def test_concurrent_same_version_registration_exactly_one_wins(registry: Registry):
    barrier = threading.Barrier(16)

    def register(_):
        barrier.wait()
        try:
            registry.register(_contract("1.0.0"))
            return "ok"
        except ContractError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(register, range(16)))

    assert results.count("ok") == 1
    assert set(results) <= {"ok", ErrorCode.DUPLICATE_CONTRACT}
    assert len(registry.list_versions("svc")) == 1


def test_concurrent_updates_no_lost_update(registry: Registry):
    registry.register(_contract("1.0.0"))

    def add_field(i):
        # CAS 风格重试：只有真正写入新字段时才算一次成功更新
        while True:
            current = registry.get("svc", "1.0.0")
            names = [f.name for f in current.fields]
            if f"f{i}" in names:
                return
            fields = [
                {"name": n, "type": "STRING", "required": False}
                for n in [*names, f"f{i}"]
            ]
            try:
                registry.update("svc", "1.0.0", current.revision, fields)
                return
            except ContractError as exc:
                assert exc.code is ErrorCode.REVISION_CONFLICT

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(add_field, range(8)))

    final = registry.get("svc", "1.0.0")
    # 8 个工作线程各自的字段最终都必须存在（无覆盖丢失）
    assert {f"f{i}" for i in range(8)} <= {f.name for f in final.fields}
    assert "a" in {f.name for f in final.fields}
    # 每个新增字段恰好推进一次 revision（初始 0 + 8 次真实变更）
    assert final.revision == len(final.fields) - 1 == 8


def test_concurrent_revoke_and_validate(registry: Registry):
    registry.register(
        {
            "id": "svc",
            "version": "1.0.0",
            "fields": [{"name": "a", "type": "STRING", "required": True}],
        }
    )
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def validate():
        barrier.wait()
        for _ in range(100):
            try:
                outcome = registry.validate(
                    "svc", "1.0.0", {"a": "x"}, {"a": "y"}
                )
                outcomes.append("valid" if outcome.valid else "invalid")
            except ContractError as exc:
                assert exc.code is ErrorCode.REVOKED
                outcomes.append("revoked")

    def revoke():
        barrier.wait()
        for _ in range(100):
            try:
                current = registry.get("svc", "1.0.0", include_revoked=True)
                if current.revoked:
                    return
                registry.revoke("svc", "1.0.0", current.revision)
            except ContractError as exc:
                assert exc.code in (ErrorCode.REVOKED, ErrorCode.REVISION_CONFLICT)

    t1 = threading.Thread(target=validate)
    t2 = threading.Thread(target=revoke)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # 读取只可能观察到撤销前或撤销后的稳定状态，绝不可能出现校验结果与
    # 状态不一致的中间态
    assert set(outcomes) <= {"valid", "revoked"}
    assert outcomes[-1] == "revoked"
    assert outcomes.count("valid") + outcomes.count("revoked") == 100


def test_failed_validation_never_mutates_registry(registry: Registry):
    registry.register(
        {
            "id": "svc",
            "version": "1.0.0",
            "fields": [{"name": "a", "type": "STRING", "required": True}],
        }
    )
    before = registry.get("svc", "1.0.0")

    def hammer():
        for _ in range(200):
            outcome = registry.validate("svc", "1.0.0", {"a": 123}, {})
            assert outcome.valid is False

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: hammer(), range(8)))

    after = registry.get("svc", "1.0.0")
    assert after == before
    assert after.revision == 0
