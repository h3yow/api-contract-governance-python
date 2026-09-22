"""相邻版本兼容性：破坏性变更必须命中，兼容变更不得误判，结论须可复现。"""

from __future__ import annotations

import logging

import pytest

from app.compat import (
    BREAKING_KINDS,
    ChangeKind,
    check_compatibility,
)
from app.contract import normalize_contract
from app.errors import ContractError, ErrorCode
from app.registry import Registry


def _c(version, specs, contract_id="svc"):
    return normalize_contract(
        {
            "id": contract_id,
            "version": version,
            "fields": [
                {"name": n, "type": t, "required": r} for n, t, r in specs
            ],
        }
    )


def test_field_removal_is_breaking():
    old = _c("1.0.0", [("a", "STRING", False)])
    new = _c("1.1.0", [])
    report = check_compatibility(old, new)
    assert report.compatible is False
    (change,) = report.changes
    assert change.kind is ChangeKind.FIELD_REMOVED
    assert "字段删除" in report.basis[0]


def test_required_scope_expansion_is_breaking():
    old = _c("1.0.0", [("a", "STRING", False)])
    new = _c("1.1.0", [("a", "STRING", True)])
    report = check_compatibility(old, new)
    assert report.compatible is False
    assert report.changes[0].kind is ChangeKind.REQUIRED_SCOPE_EXPANDED


def test_new_required_field_is_breaking_but_optional_addition_is_not():
    old = _c("1.0.0", [])
    new_req = _c("1.1.0", [("a", "STRING", True)])
    assert check_compatibility(old, new_req).compatible is False
    new_opt = _c("1.2.0", [("a", "STRING", False)])
    report = check_compatibility(old, new_opt)
    assert report.compatible is True
    assert report.changes[0].kind is ChangeKind.FIELD_ADDED


def test_type_narrowing_vs_widening():
    old = _c("1.0.0", [("x", "NUMBER", False)])
    narrowed = _c("1.1.0", [("x", "INTEGER", False)])
    assert check_compatibility(old, narrowed).compatible is False

    old2 = _c("1.0.0", [("x", "INTEGER", False)])
    widened = _c("1.1.0", [("x", "NUMBER", False)])
    report = check_compatibility(old2, widened)
    assert report.compatible is True
    assert report.changes[0].kind is ChangeKind.TYPE_WIDENED

    sideways = _c("1.2.0", [("x", "BOOLEAN", False)])
    report2 = check_compatibility(old2, sideways)
    assert report2.compatible is False
    assert report2.changes[0].kind in BREAKING_KINDS


def test_optional_relaxation_and_identical_schema_are_compatible():
    old = _c("1.0.0", [("a", "STRING", True)])
    new = _c("1.1.0", [("a", "STRING", False)])
    report = check_compatibility(old, new)
    assert report.compatible is True
    assert report.changes[0].kind is ChangeKind.OPTIONAL_RELAXED

    same = check_compatibility(old, old)
    assert same.compatible is True and same.changes == ()


def test_report_is_reproducible(registry: Registry):
    registry.register(
        {"id": "svc", "version": "1.0.0",
         "fields": [{"name": "a", "type": "STRING", "required": True}]}
    )
    registry.register(
        {"id": "svc", "version": "1.1.0",
         "fields": [{"name": "a", "type": "STRING", "required": False},
                    {"name": "b", "type": "INTEGER", "required": False}]}
    )
    first = registry.compatibility("svc", "1.0.0", "1.1.0")
    second = registry.compatibility("svc", "1.0.0", "1.1.0")
    assert first == second
    assert first.to_dict() == second.to_dict()


def test_non_adjacent_versions_rejected(registry: Registry):
    for v in ("1.0.0", "1.1.0", "1.2.0"):
        registry.register({"id": "svc", "version": v, "fields": []})
    with pytest.raises(ContractError) as exc:
        registry.compatibility("svc", "1.0.0", "1.2.0")
    assert exc.value.code is ErrorCode.VERSION_NOT_ADJACENT
    assert exc.value.details["registered_order"] == ["1.0.0", "1.1.0", "1.2.0"]
    # 方向反了同样不是相邻对
    with pytest.raises(ContractError) as exc:
        registry.compatibility("svc", "1.2.0", "1.1.0")
    assert exc.value.code is ErrorCode.VERSION_NOT_ADJACENT


def test_compat_logging_includes_id_basis(registry: Registry, caplog):
    registry.register(
        {"id": "svc", "version": "1.0.0",
         "fields": [{"name": "a", "type": "STRING"}]}
    )
    registry.register(
        {"id": "svc", "version": "2.0.0", "fields": []}
    )
    with caplog.at_level(logging.INFO, logger="contractgov"):
        report = registry.compatibility("svc", "1.0.0", "2.0.0")
    assert report.compatible is False
    assert "svc" in caplog.text
    assert "字段删除" in caplog.text
    assert "basis=" in caplog.text
