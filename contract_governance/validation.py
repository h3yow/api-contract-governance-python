"""基于已注册契约的运行时校验：单侧老用法与请求/响应成对用法。

单侧语义（保持原有行为）：
- 未注册        -> 抛 CONTRACT_NOT_FOUND
- 已撤销        -> 抛 CONTRACT_REVOKED（撤销后不得参与校验）
- 版本不匹配    -> 抛 VERSION_MISMATCH
- 对成对契约调单侧入口 -> 抛 VALIDATION_SIDE_MISMATCH（用法可区分）
- 校验失败      -> 返回 ValidationResult(ok=False, issues=...)，不抛异常、
                   不修改注册表，失败不污染已注册状态。

成对语义：
- 契约通过 request_fields / response_fields 声明两侧形状（可只声明一侧），
  调用方在一次 validate_pair 里提交两侧内容，结果按侧归属（SideValidationResult），
  两侧问题互不覆盖，任一侧不通过则 PairedValidationResult.ok 为 False。
- 为未声明的侧提交内容、或漏提交已声明侧的内容 -> INVALID_CONTRACT（调用方用法错误）。

时间预算：单侧与成对校验都受可配置 validation_timeout 约束，
超预算整单拒绝（BUDGET_EXCEEDED），不返回半截结果；校验只读不可变快照，
单调用超时不影响其他并发校验，也不改变注册状态。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final

from .budget import run_with_budget
from .config import GovernanceLimits
from .errors import GovernanceError, Reason
from .models import (
    Contract,
    ContractStatus,
    FieldType,
    PairedValidationResult,
    Side,
    SideValidationResult,
    ValidationIssue,
    ValidationResult,
)
from .registry import ContractRegistry

_TYPE_CHECKS: dict[FieldType, Any] = {
    # 注意：Python 中 bool 是 int 的子类，数值类型必须先排除 bool。
    FieldType.STRING: lambda v: isinstance(v, str),
    FieldType.INTEGER: lambda v: isinstance(v, int) and not isinstance(v, bool),
    FieldType.NUMBER: lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    FieldType.BOOLEAN: lambda v: isinstance(v, bool),
    FieldType.OBJECT: lambda v: isinstance(v, dict),
    FieldType.ARRAY: lambda v: isinstance(v, list),
}

_HIDDEN_MESSAGE = "detail hidden by configuration"

MISSING: Final = object()  # 成对校验中“未提交该侧”的哨兵
# 服务门面「沿用配置默认预算」的哨兵：任何负数都视为默认。
_USE_DEFAULT_TIMEOUT: Final = -1.0

def _check_shape(
    fields: tuple[Any, ...],
    payload: Any,
    limits: GovernanceLimits,
    checkpoint: Callable[[], None] | None = None,
) -> tuple[ValidationIssue, ...]:
    """对一份字段形状与一份载荷做字段级校验，返回（截断/脱敏后的）问题元组。

    若提供 checkpoint，则每处理固定数量字段协作式检查一次时间预算，
    使超时能在大规格校验中确定性地提前终止，而不是空跑到结束。
    """
    issues: list[ValidationIssue] = []
    if not isinstance(payload, dict):
        issues.append(
            ValidationIssue(path="$", code="not_an_object", message="payload must be an object")
        )
    else:
        for index, f in enumerate(fields):
            if checkpoint is not None and index and index % 64 == 0:
                checkpoint()
            if f.name not in payload:
                if f.required:
                    issues.append(
                        ValidationIssue(
                            path=f"$.{f.name}",
                            code="missing_required",
                            message=f"required field '{f.name}' is missing",
                        )
                    )
                continue
            value = payload[f.name]
            if not _TYPE_CHECKS[f.type](value):
                issues.append(
                    ValidationIssue(
                        path=f"$.{f.name}",
                        code="type_mismatch",
                        message=(
                            f"field '{f.name}' expects {f.type.value}, "
                            f"got {type(value).__name__}"
                        ),
                    )
                )

    truncated = len(issues) > limits.max_validation_issues
    issues = issues[: limits.max_validation_issues]
    if truncated:
        issues.append(
            ValidationIssue(
                path="$",
                code="issues_truncated",
                message=f"issue list truncated at {limits.max_validation_issues}",
            )
        )
    if not limits.expose_error_details:
        issues = [
            ValidationIssue(path=i.path, code=i.code, message=_HIDDEN_MESSAGE)
            for i in issues
        ]
    return tuple(issues)


class ContractValidator:
    """运行时校验器（单侧 + 成对），所有调用受时间预算约束。"""

    def __init__(
        self,
        registry: ContractRegistry,
        limits: GovernanceLimits | None = None,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._registry = registry
        self._limits = limits or GovernanceLimits()
        self._clock = clock

    def _resolve(self, contract_id: str, version: int) -> Contract:
        contract = self._registry.get(contract_id)  # 未注册 -> CONTRACT_NOT_FOUND
        if contract.status is ContractStatus.REVOKED:
            raise GovernanceError(
                Reason.CONTRACT_REVOKED, f"contract '{contract_id}' is revoked"
            )
        if contract.version != version:
            raise GovernanceError(
                Reason.VERSION_MISMATCH,
                f"version mismatch: registered={contract.version}, requested={version}",
            )
        return contract

    def _timeout(self, override: Any) -> float | None:
        if isinstance(override, (int, float)) and not isinstance(override, bool) and override < 0:
            return self._limits.validation_timeout
        return override

    def validate(
        self,
        contract_id: str,
        version: int,
        payload: Any,
        *,
        timeout: float | None = _USE_DEFAULT_TIMEOUT,
    ) -> ValidationResult:
        budget = self._timeout(timeout)

        def work(checkpoint: Callable[[], None]) -> ValidationResult:
            contract = self._resolve(contract_id, version)
            if contract.spec.request_fields or contract.spec.response_fields:
                raise GovernanceError(
                    Reason.VALIDATION_SIDE_MISMATCH,
                    f"contract '{contract_id}' declares request/response sides; "
                    "use validate_pair",
                )
            checkpoint()
            issues = _check_shape(
                contract.spec.fields, payload, self._limits, checkpoint
            )
            return ValidationResult(
                ok=not issues,
                contract_id=contract_id,
                version=version,
                issues=issues,
            )

        return run_with_budget(work, budget, clock=self._clock)

    def validate_pair(
        self,
        contract_id: str,
        version: int,
        *,
        request: Any = MISSING,
        response: Any = MISSING,
        timeout: float | None = _USE_DEFAULT_TIMEOUT,
    ) -> PairedValidationResult:
        """一次校验同时提交请求侧与响应侧内容，结果按侧归属。"""
        budget = self._timeout(timeout)

        def work(checkpoint: Callable[[], None]) -> PairedValidationResult:
            contract = self._resolve(contract_id, version)
            declared = contract.spec.sides()
            if not declared:
                raise GovernanceError(
                    Reason.VALIDATION_SIDE_MISMATCH,
                    f"contract '{contract_id}' declares no request/response sides; "
                    "use validate",
                )

            submitted: dict[Side, Any] = {}
            if request is not MISSING:
                submitted[Side.REQUEST] = request
            if response is not MISSING:
                submitted[Side.RESPONSE] = response

            # 已声明的侧必须提交；未声明的侧不得提交——均属调用方用法错误。
            unknown = [s.value for s in submitted if s not in declared]
            if unknown:
                raise GovernanceError(
                    Reason.INVALID_CONTRACT,
                    f"contract '{contract_id}' does not declare side: {','.join(unknown)}",
                )
            missing = [s.value for s in declared if s not in submitted]
            if missing:
                raise GovernanceError(
                    Reason.INVALID_CONTRACT,
                    f"contract '{contract_id}' requires payload for side: {','.join(missing)}",
                )

            checkpoint()
            side_results: list[SideValidationResult] = []
            # 固定侧顺序（request -> response），保证结果可复现。
            for side in (Side.REQUEST, Side.RESPONSE):
                if side not in declared:
                    continue
                issues = _check_shape(
                    declared[side], submitted[side], self._limits, checkpoint
                )
                side_results.append(SideValidationResult(side=side, ok=not issues, issues=issues))
                checkpoint()  # 两侧之间也是预算检查点

            overall_ok = all(r.ok for r in side_results)
            return PairedValidationResult(
                ok=overall_ok,
                contract_id=contract_id,
                version=version,
                sides=tuple(side_results),
            )

        return run_with_budget(work, budget, clock=self._clock)
