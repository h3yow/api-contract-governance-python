"""相邻版本兼容性判定：结论可复现、依据可解释。

规则（请求/响应共用的保守契约视角）：
- 字段删除            -> 破坏性 (FIELD_REMOVED)
- 类型收窄            -> 破坏性 (TYPE_NARROWED)；仅 integer -> number 视为放宽
- 必填范围扩大        -> 破坏性 (REQUIRED_EXPANDED)：可选变必填，或新增必填字段
- 必填范围放宽收窄    -> 兼容   (REQUIRED_RELAXED)
- 新增可选字段        -> 兼容   (FIELD_ADDED_OPTIONAL)

老契约（只声明 fields）与成对契约（request_fields/response_fields）不混用：
形状模式必须一致，否则报 INVALID_CONTRACT。成对契约逐侧比较，
每条 Change 带 side 归属。

判定是纯函数：相同输入必然得到相同报告；变更列表按（侧、字段名）排序，
保证日志与测试中的输出可复现。
"""

from __future__ import annotations

from .errors import GovernanceError, Reason
from .models import (
    Change,
    ChangeKind,
    CompatReport,
    Contract,
    FieldSpec,
    FieldType,
    Side,
    Verdict,
)

# 允许的类型放宽边：integer 可放宽为 number，其余类型变化一律视为收窄。
_WIDENING_EDGES: frozenset[tuple[FieldType, FieldType]] = frozenset(
    {(FieldType.INTEGER, FieldType.NUMBER)}
)

_BREAKING_KINDS: frozenset[ChangeKind] = frozenset(
    {
        ChangeKind.FIELD_REMOVED,
        ChangeKind.TYPE_NARROWED,
        ChangeKind.REQUIRED_EXPANDED,
    }
)


def _is_paired(spec: object) -> bool:
    return bool(spec.request_fields or spec.response_fields)


def _diff_fields(
    old_fields: tuple[FieldSpec, ...],
    new_fields: tuple[FieldSpec, ...],
    side: Side | None,
) -> list[Change]:
    """比较一侧（或单侧老契约）的字段集合，产出变更列表。"""
    old_map = {f.name: f for f in old_fields}
    new_map = {f.name: f for f in new_fields}
    changes: list[Change] = []

    for name in sorted(old_map.keys() - new_map.keys()):
        changes.append(
            Change(
                ChangeKind.FIELD_REMOVED,
                name,
                f"field '{name}' was removed",
                side,
            )
        )

    for name in sorted(old_map.keys() & new_map.keys()):
        o, n = old_map[name], new_map[name]
        if o.type is not n.type:
            if (o.type, n.type) in _WIDENING_EDGES:
                changes.append(
                    Change(
                        ChangeKind.TYPE_WIDENED,
                        name,
                        f"field '{name}' type widened {o.type.value} -> {n.type.value}",
                        side,
                    )
                )
            else:
                changes.append(
                    Change(
                        ChangeKind.TYPE_NARROWED,
                        name,
                        f"field '{name}' type changed {o.type.value} -> {n.type.value}",
                        side,
                    )
                )
        if n.required and not o.required:
            changes.append(
                Change(
                    ChangeKind.REQUIRED_EXPANDED,
                    name,
                    f"field '{name}' became required",
                    side,
                )
            )
        elif o.required and not n.required:
            changes.append(
                Change(
                    ChangeKind.REQUIRED_RELAXED,
                    name,
                    f"field '{name}' became optional",
                    side,
                )
            )

    for name in sorted(new_map.keys() - old_map.keys()):
        n = new_map[name]
        if n.required:
            changes.append(
                Change(
                    ChangeKind.REQUIRED_EXPANDED,
                    name,
                    f"required field '{name}' was added",
                    side,
                )
            )
        else:
            changes.append(
                Change(
                    ChangeKind.FIELD_ADDED_OPTIONAL,
                    name,
                    f"optional field '{name}' was added",
                    side,
                )
            )

    return changes


def judge_compatibility(old: Contract, new: Contract) -> CompatReport:
    """判定同一契约相邻两个版本的兼容性。

    要求 old.contract_id == new.contract_id 且 new.version == old.version + 1，
    否则抛 ILLEGAL_VERSION_TRANSITION —— 兼容性只在相邻版本间定义。
    """
    if old.contract_id != new.contract_id:
        raise GovernanceError(
            Reason.INVALID_CONTRACT,
            "compatibility is only defined within one contract id",
        )
    if new.version != old.version + 1:
        raise GovernanceError(
            Reason.ILLEGAL_VERSION_TRANSITION,
            f"compatibility requires adjacent versions, got {old.version} -> {new.version}",
        )
    if _is_paired(old.spec) is not _is_paired(new.spec):
        raise GovernanceError(
            Reason.INVALID_CONTRACT,
            "cannot compare a single-shape spec with a paired request/response spec",
        )

    if _is_paired(old.spec):
        changes = (
            _diff_fields(old.spec.request_fields, new.spec.request_fields, Side.REQUEST)
            + _diff_fields(old.spec.response_fields, new.spec.response_fields, Side.RESPONSE)
        )
        # 按侧再按字段名排序，保证输出可复现。
        changes.sort(key=lambda c: (c.side.value, c.field_name, c.kind.value))
    else:
        changes = _diff_fields(old.spec.fields, new.spec.fields, None)

    verdict = (
        Verdict.BREAKING
        if any(c.kind in _BREAKING_KINDS for c in changes)
        else Verdict.COMPATIBLE
    )
    return CompatReport(
        contract_id=old.contract_id,
        from_version=old.version,
        to_version=new.version,
        verdict=verdict,
        changes=tuple(changes),
    )
