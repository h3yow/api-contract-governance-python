"""相邻版本兼容性判定：结论可复现、依据可解释，请求/响应两侧分别判定。

单侧（每一侧各自适用）的规则（保守契约视角）：
- 字段删除            -> 破坏性 (FIELD_REMOVED)
- 类型收窄            -> 破坏性 (TYPE_NARROWED)；仅 integer -> number 视为放宽
- 必填范围扩大        -> 破坏性 (REQUIRED_EXPANDED)：可选变必填，或新增必填字段
- 必填范围放宽        -> 兼容   (REQUIRED_RELAXED)
- 新增可选字段        -> 兼容   (FIELD_ADDED_OPTIONAL)

成对契约两侧分别判定：请求侧与响应侧各自独立给出结论，
任一侧存在破坏性变更则整体 verdict 为 breaking；每条变更带 side 标记，
两侧问题互不掩盖，判定依据可逐条看出变化发生在哪一侧。

判定是纯函数：相同输入必然得到相同报告；每侧变更按字段名排序，
合并依据按 (字段名, 侧别) 排序，保证日志与测试中的输出可复现。
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
    SideCompatReport,
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


def _judge_side_fields(
    old_fields: tuple[FieldSpec, ...],
    new_fields: tuple[FieldSpec, ...],
    side: Side,
) -> list[Change]:
    """判定单侧字段形状的相邻版本变化，变更按字段名排序。"""
    old_by_name = {f.name: f for f in old_fields}
    new_by_name = {f.name: f for f in new_fields}
    changes: list[Change] = []

    for name in sorted(old_by_name.keys() - new_by_name.keys()):
        changes.append(
            Change(
                ChangeKind.FIELD_REMOVED,
                name,
                f"{side.value} field '{name}' was removed",
                side,
            )
        )

    for name in sorted(old_by_name.keys() & new_by_name.keys()):
        o, n = old_by_name[name], new_by_name[name]
        if o.type is not n.type:
            if (o.type, n.type) in _WIDENING_EDGES:
                changes.append(
                    Change(
                        ChangeKind.TYPE_WIDENED,
                        name,
                        f"{side.value} field '{name}' type widened "
                        f"{o.type.value} -> {n.type.value}",
                        side,
                    )
                )
            else:
                changes.append(
                    Change(
                        ChangeKind.TYPE_NARROWED,
                        name,
                        f"{side.value} field '{name}' type changed "
                        f"{o.type.value} -> {n.type.value}",
                        side,
                    )
                )
        if n.required and not o.required:
            changes.append(
                Change(
                    ChangeKind.REQUIRED_EXPANDED,
                    name,
                    f"{side.value} field '{name}' became required",
                    side,
                )
            )
        elif o.required and not n.required:
            changes.append(
                Change(
                    ChangeKind.REQUIRED_RELAXED,
                    name,
                    f"{side.value} field '{name}' became optional",
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
                    f"required {side.value} field '{name}' was added",
                    side,
                )
            )
        else:
            changes.append(
                Change(
                    ChangeKind.FIELD_ADDED_OPTIONAL,
                    name,
                    f"optional {side.value} field '{name}' was added",
                    side,
                )
            )

    return changes


def _side_report(changes: list[Change], side: Side) -> SideCompatReport:
    verdict = (
        Verdict.BREAKING
        if any(c.kind in _BREAKING_KINDS for c in changes)
        else Verdict.COMPATIBLE
    )
    return SideCompatReport(side=side, verdict=verdict, changes=tuple(changes))


def judge_compatibility(old: Contract, new: Contract) -> CompatReport:
    """判定同一契约相邻两个版本的兼容性，请求侧与响应侧分别判定。

    要求 old.contract_id == new.contract_id 且 new.version == old.version + 1，
    否则抛 ILLEGAL_VERSION_TRANSITION —— 兼容性只在相邻版本间定义。

    - 老的单侧契约（两侧版本都未声明响应侧）：只给请求侧结论，
      response 为 None，行为与历史完全一致。
    - 成对契约（任一版本声明了响应侧）：两侧分别判定，
      未声明的一侧按空形状处理；单侧→成对 / 成对→单侧的迁移也能被识别。
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

    request_report = _side_report(
        _judge_side_fields(old.spec.fields, new.spec.fields, Side.REQUEST),
        Side.REQUEST,
    )

    response_report: SideCompatReport | None = None
    if old.spec.is_paired or new.spec.is_paired:
        response_report = _side_report(
            _judge_side_fields(
                old.spec.response_fields or (),
                new.spec.response_fields or (),
                Side.RESPONSE,
            ),
            Side.RESPONSE,
        )

    # 合并两侧依据：按 (字段名, 侧别) 排序，保证输出可复现、两侧依据都可见。
    all_changes = sorted(
        request_report.changes
        + (response_report.changes if response_report is not None else ()),
        key=lambda c: (c.field_name, c.side.value),
    )
    verdict = (
        Verdict.BREAKING
        if request_report.verdict is Verdict.BREAKING
        or (response_report is not None and response_report.verdict is Verdict.BREAKING)
        else Verdict.COMPATIBLE
    )
    return CompatReport(
        contract_id=old.contract_id,
        from_version=old.version,
        to_version=new.version,
        verdict=verdict,
        request=request_report,
        response=response_report,
        changes=tuple(all_changes),
    )
