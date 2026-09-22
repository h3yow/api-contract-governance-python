"""相邻版本之间的兼容性判定。

判定规则（旧版本 -> 新版本，从“旧版本调用方仍可工作”的视角）：

破坏性变更（compatible=False）：

1. FIELD_REMOVED：旧契约字段在新契约中被删除；
2. REQUIRED_SCOPE_EXPANDED：字段由可选变必填（必填范围扩大）；
3. TYPE_NARROWED：字段类型收窄（如 NUMBER -> INTEGER、STRING -> INTEGER）。

兼容变更（compatible=True）：

1. FIELD_ADDED：新增可选字段；若新增字段必填则记 REQUIRED_SCOPE_EXPANDED（破坏性）；
2. OPTIONAL_RELAXED：字段由必填变可选；
3. TYPE_WIDENED：类型放宽（如 INTEGER -> NUMBER）。

判定纯函数化：同一对 (契约id, 版本, revision) 输入必然得到同一结论。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .contract import Contract


class ChangeKind(str, Enum):
    FIELD_REMOVED = "FIELD_REMOVED"
    TYPE_NARROWED = "TYPE_NARROWED"
    REQUIRED_SCOPE_EXPANDED = "REQUIRED_SCOPE_EXPANDED"
    FIELD_ADDED = "FIELD_ADDED"
    OPTIONAL_RELAXED = "OPTIONAL_RELAXED"
    TYPE_WIDENED = "TYPE_WIDENED"
    TYPE_INCOMPATIBLE = "TYPE_INCOMPATIBLE"


BREAKING_KINDS = frozenset(
    {
        ChangeKind.FIELD_REMOVED,
        ChangeKind.TYPE_NARROWED,
        ChangeKind.REQUIRED_SCOPE_EXPANDED,
        ChangeKind.TYPE_INCOMPATIBLE,
    }
)

# 数值类型放宽方向：INTEGER 是 NUMBER 的子集
_WIDENING = {
    ("INTEGER", "NUMBER"): ChangeKind.TYPE_WIDENED,
    ("NUMBER", "INTEGER"): ChangeKind.TYPE_NARROWED,
}


@dataclass(frozen=True)
class Change:
    kind: ChangeKind
    field: str
    detail: str

    def to_dict(self) -> dict:
        return {"kind": self.kind.value, "field": self.field, "detail": self.detail}


@dataclass(frozen=True)
class CompatReport:
    contract_id: str
    from_version: str
    to_version: str
    compatible: bool
    changes: tuple[Change, ...]
    basis: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "id": self.contract_id,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "compatible": self.compatible,
            "changes": [c.to_dict() for c in self.changes],
            "basis": list(self.basis),
        }


def check_compatibility(old: Contract, new: Contract) -> CompatReport:
    """对两份相邻版本契约做字段级差异判定。"""
    if old.contract_id != new.contract_id:
        raise ValueError("仅支持同一契约标识的版本比较")

    old_fields = old.field_map()
    new_fields = new.field_map()
    changes: list[Change] = []
    basis: list[str] = []

    for name in sorted(old_fields.keys() | new_fields.keys()):
        in_old = name in old_fields
        in_new = name in new_fields
        if in_old and not in_new:
            changes.append(
                Change(ChangeKind.FIELD_REMOVED, name, "旧字段在新版本中被删除")
            )
            basis.append(f"{name}: 字段删除 => 破坏性")
            continue
        if in_new and not in_old:
            nf = new_fields[name]
            if nf.required:
                changes.append(
                    Change(
                        ChangeKind.REQUIRED_SCOPE_EXPANDED,
                        name,
                        "新增字段直接声明为必填，必填范围扩大",
                    )
                )
                basis.append(f"{name}: 新增必填字段 => 破坏性")
            else:
                changes.append(Change(ChangeKind.FIELD_ADDED, name, "新增可选字段"))
                basis.append(f"{name}: 新增可选字段 => 兼容")
            continue

        of = old_fields[name]
        nf = new_fields[name]
        if of.type != nf.type:
            kind = _WIDENING.get((of.type, nf.type), ChangeKind.TYPE_INCOMPATIBLE)
            if kind == ChangeKind.TYPE_WIDENED:
                detail = f"类型放宽 {of.type} -> {nf.type}"
                verdict = "兼容"
            elif kind == ChangeKind.TYPE_NARROWED:
                detail = f"类型收窄 {of.type} -> {nf.type}"
                verdict = "破坏性"
            else:
                detail = f"类型互不兼容 {of.type} -> {nf.type}"
                verdict = "破坏性"
            changes.append(Change(kind, name, detail))
            basis.append(f"{name}: {detail} => {verdict}")
        if of.required != nf.required:
            if not of.required and nf.required:
                changes.append(
                    Change(
                        ChangeKind.REQUIRED_SCOPE_EXPANDED,
                        name,
                        "可选字段变为必填，必填范围扩大",
                    )
                )
                basis.append(f"{name}: 可选->必填 => 破坏性")
            elif of.required and not nf.required:
                changes.append(
                    Change(ChangeKind.OPTIONAL_RELAXED, name, "必填字段变为可选")
                )
                basis.append(f"{name}: 必填->可选 => 兼容")

    changes.sort(key=lambda c: (c.field, c.kind.value))
    basis.sort()
    breaking = [c for c in changes if c.kind in BREAKING_KINDS]
    if not changes:
        basis.append("无字段差异 => 兼容")
    return CompatReport(
        contract_id=old.contract_id,
        from_version=str(old.version),
        to_version=str(new.version),
        compatible=not breaking,
        changes=tuple(changes),
        basis=tuple(basis),
    )
