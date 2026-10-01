"""相邻版本兼容性判定：结论可复现、依据可解释。

规则（请求/响应共用的保守契约视角）：
- 字段删除            -> 破坏性 (FIELD_REMOVED)
- 类型收窄            -> 破坏性 (TYPE_NARROWED)；仅 integer -> number 视为放宽
- 必填范围扩大        -> 破坏性 (REQUIRED_EXPANDED)：可选变必填，或新增必填字段
- 必填范围放宽收窄    -> 兼容   (REQUIRED_RELAXED)
- 新增可选字段        -> 兼容   (FIELD_ADDED_OPTIONAL)

成对契约（声明了 response_fields）请求侧与响应侧分别判定，两侧结论
都体现在报告中：每条 Change 带 side 标记，request_verdict /
response_verdict 各自给出单侧结论，整体 verdict 取两侧最严格者
—— 任一侧出现破坏性改动即整体破坏性，两侧问题不会互相掩盖。

判定是纯函数：相同输入必然得到相同报告；变更列表按侧别再按字段名
排序，保证日志与测试中的输出可复现。
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


def judge_compatibility(old: Contract, new: Contract) -> CompatReport:
    """判定同一契约相邻两个版本的兼容性。

    要求 old.contract_id == new.contract_id 且 new.version == old.version + 1，
    否则抛 ILLEGAL_VERSION_TRANSITION —— 兼容性只在相邻版本间定义。

    请求侧始终判定；任一端声明了响应侧形状（成对契约）时响应侧也判定，
    未声明的一端按无字段约束处理。两侧结论分别记入报告。
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

    changes: list[Change] = []
    request_changes = _diff_fields(
        old.spec.fields_of(Side.REQUEST), new.spec.fields_of(Side.REQUEST), Side.REQUEST
    )
    changes.extend(request_changes)
    request_verdict = _verdict_of(request_changes)

    response_verdict: Verdict | None = None
    if old.spec.is_paired or new.spec.is_paired:
        response_changes = _diff_fields(
            old.spec.fields_of(Side.RESPONSE),
            new.spec.fields_of(Side.RESPONSE),
            Side.RESPONSE,
        )
        changes.extend(response_changes)
        response_verdict = _verdict_of(response_changes)

    side_verdicts = [request_verdict] + (
        [response_verdict] if response_verdict is not None else []
    )
    verdict = (
        Verdict.BREAKING
        if any(v is Verdict.BREAKING for v in side_verdicts)
        else Verdict.COMPATIBLE
    )
    return CompatReport(
        contract_id=old.contract_id,
        from_version=old.version,
        to_version=new.version,
        verdict=verdict,
        changes=tuple(changes),
        request_verdict=request_verdict,
        response_verdict=response_verdict,
    )


def _verdict_of(changes: list[Change]) -> Verdict:
    """由单侧变更列表得出单侧结论。"""
    return (
        Verdict.BREAKING
        if any(c.kind in _BREAKING_KINDS for c in changes)
        else Verdict.COMPATIBLE
    )


def _diff_fields(
    old_fields: tuple[FieldSpec, ...],
    new_fields: tuple[FieldSpec, ...],
    side: Side,
) -> list[Change]:
    """比较单侧字段形状，产出带侧别标记、按字段名排序的变更列表。"""
    old_by_name = {f.name: f for f in old_fields}
    new_by_name = {f.name: f for f in new_fields}
    changes: list[Change] = []

    for name in sorted(old_by_name.keys() - new_by_name.keys()):
        changes.append(
            Change(ChangeKind.FIELD_REMOVED, name, f"field '{name}' was removed", side)
        )

    for name in sorted(old_by_name.keys() & new_by_name.keys()):
        o, n = old_by_name[name], new_by_name[name]
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

    for name in sorted(new_by_name.keys() - old_by_name.keys()):
        n = new_by_name[name]
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
