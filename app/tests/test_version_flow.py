"""非法版本流转：错误须被正确归类、原因可区分，失败不得污染已有状态。"""

from __future__ import annotations

import logging

import pytest

from app.errors import ContractError, ErrorCode
from app.registry import Registry, reset_registry


def _fields(*specs):
    return [
        {"name": name, "type": ftype, "required": required}
        for name, ftype, required in specs
    ]


def test_register_then_get_normalized(registry: Registry, caplog):
    with caplog.at_level(logging.INFO, logger="contractgov"):
        c = registry.register(
            {
                "id": "svc",
                "version": "1.0.0",
                "fields": _fields(("b", "STRING", False), ("a", "INTEGER", True)),
            }
        )
    assert c.revision == 0
    # 归一化：字段按名排序
    assert [f.name for f in c.fields] == ["a", "b"]
    fetched = registry.get("svc", "1.0.0")
    assert fetched.normalized == c.normalized
    assert "svc" in caplog.text and "normalized" in caplog.text


def test_duplicate_registration_is_distinguished(registry: Registry):
    registry.register({"id": "svc", "version": "1.0.0", "fields": []})
    with pytest.raises(ContractError) as exc:
        registry.register({"id": "svc", "version": "1.0.0", "fields": []})
    assert exc.value.code is ErrorCode.DUPLICATE_CONTRACT
    assert exc.value.details["existing_revision"] == 0


def test_regression_version_rejected(registry: Registry):
    registry.register({"id": "svc", "version": "1.2.0", "fields": []})
    for bad in ("1.1.9", "0.9.0"):
        with pytest.raises(ContractError) as exc:
            registry.register({"id": "svc", "version": bad, "fields": []})
        assert exc.value.code is ErrorCode.VERSION_NOT_NEWER
        assert exc.value.details["latest"] == "1.2.0"
    # 同版本重复注册与版本回退必须给出不同原因
    with pytest.raises(ContractError) as exc:
        registry.register({"id": "svc", "version": "1.2.0", "fields": []})
    assert exc.value.code is ErrorCode.DUPLICATE_CONTRACT


def test_newer_version_succeeds_after_rejection(registry: Registry):
    registry.register({"id": "svc", "version": "1.0.0", "fields": []})
    with pytest.raises(ContractError) as exc:
        registry.register({"id": "svc", "version": "0.9.0", "fields": []})
    assert exc.value.code is ErrorCode.VERSION_NOT_NEWER
    # 失败不得污染状态：1.1.0 仍是 1.0.0 之后的第一个新版本
    c = registry.register({"id": "svc", "version": "1.1.0", "fields": []})
    assert c.revision == 0
    assert [str(x.version) for x in registry.list_versions("svc")] == [
        "1.0.0",
        "1.1.0",
    ]


def test_malformed_version_is_invalid_contract(registry: Registry):
    with pytest.raises(ContractError) as exc:
        registry.register({"id": "svc", "version": "v1", "fields": []})
    assert exc.value.code is ErrorCode.INVALID_CONTRACT


def test_update_uses_revision_optimistic_lock(registry: Registry):
    registry.register(
        {"id": "svc", "version": "1.0.0", "fields": _fields(("a", "STRING", False))}
    )
    with pytest.raises(ContractError) as exc:
        registry.update("svc", "1.0.0", 42, _fields(("a", "STRING", False)))
    assert exc.value.code is ErrorCode.REVISION_CONFLICT
    updated = registry.update(
        "svc", "1.0.0", 0, _fields(("a", "STRING", False), ("b", "INTEGER", True))
    )
    assert updated.revision == 1
    # 旧 revision 再更新必须失败
    with pytest.raises(ContractError) as exc:
        registry.update("svc", "1.0.0", 0, _fields(("a", "STRING", False)))
    assert exc.value.code is ErrorCode.REVISION_CONFLICT
    assert exc.value.details["current"] == 1


def test_revoked_contract_excluded_from_validation_and_updates(registry: Registry):
    registry.register(
        {"id": "svc", "version": "1.0.0", "fields": _fields(("a", "STRING", True))}
    )
    registry.revoke("svc", "1.0.0", 0)
    with pytest.raises(ContractError) as exc:
        registry.validate("svc", "1.0.0", {"a": "x"}, {"a": "y"})
    assert exc.value.code is ErrorCode.REVOKED
    with pytest.raises(ContractError) as exc:
        registry.update("svc", "1.0.0", 1, [])
    assert exc.value.code is ErrorCode.REVOKED
    # 撤销可被显式查询到，但默认视图与 latest 均不包含
    assert registry.get("svc", "1.0.0", include_revoked=True).revoked is True
    with pytest.raises(ContractError) as exc:
        registry.get("svc", "1.0.0")
    assert exc.value.code is ErrorCode.REVOKED
    assert registry.list_versions("svc") == []
    # 撤销后仍可注册更新版本
    registry.register(
        {"id": "svc", "version": "2.0.0", "fields": _fields(("a", "STRING", True))}
    )
    assert str(registry.latest("svc").version) == "2.0.0"


def test_revoke_requires_current_revision(registry: Registry):
    registry.register({"id": "svc", "version": "1.0.0", "fields": []})
    with pytest.raises(ContractError) as exc:
        registry.revoke("svc", "1.0.0", 7)
    assert exc.value.code is ErrorCode.REVISION_CONFLICT
    # 撤销失败不留痕：仍能按 revision 0 正常撤销
    assert registry.get("svc", "1.0.0").revoked is False
