"""基于已注册契约的运行时请求/响应校验。

支持两种用法：
- 单侧 validate：老契约（只声明 fields）与新契约都可使用，校验请求侧形状，
  行为与历史完全一致。
- 成对 validate_pair：仅适用于成对声明（fields + response_fields）的契约，
  一次提交两侧内容，问题按侧归属、互不覆盖，任一侧不过则整体不过。

语义：
- 未注册        -> 抛 CONTRACT_NOT_FOUND
- 已撤销        -> 抛 CONTRACT_REVOKED（撤销后不得参与校验）
- 版本不匹配    -> 抛 VERSION_MISMATCH
- 成对校验用于非成对契约 -> 抛 INVALID_CONTRACT
- 超过时间预算  -> 抛 VALIDATION_TIMEOUT，不返回半截结果、不泄漏内部细节
- 字段校验失败  -> 返回 ok=False 的结果对象，不抛异常、不改注册表。

校验为纯读取操作：先取不可变快照再校验，单次调用在独立线程的预算内执行，
并发写入与其他调用的超时都不会影响本次校验或已注册契约的状态。
"""

from __future__ import annotations

from time import monotonic
from typing import Any

from .config import GovernanceLimits
from .deadline import USE_DEFAULT_BUDGET, run_with_budget
from .errors import GovernanceError, Reason
from .models import (
    Contract,
    ContractSpec,
    ContractStatus,
    FieldType,
    PairValidationResult,
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
_TIMEOUT_MESSAGE = "validation exceeded its time budget"


class _Deadline:
    """协作式时间预算：字段遍历过程中周期性检查，超长提前终止。"""

    def __init__(self, timeout_seconds: float) -> None:
        self._timeout = timeout_seconds
        self._start = monotonic()

    def tick(self) -> None:
        if self._timeout and self._timeout > 0 and monotonic() - self._start >= self._timeout:
            raise GovernanceError(Reason.VALIDATION_TIMEOUT, _TIMEOUT_MESSAGE)


class ContractValidator:
    """运行时校验器：单侧 / 成对两种用法共用同一时间预算机制。"""

    def __init__(self, registry: ContractRegistry, limits: GovernanceLimits | None = None) -> None:
        self._registry = registry
        self._limits = limits or GovernanceLimits()

    def _resolve_timeout(self, override: Any) -> float:
        return self._limits.validation_timeout_seconds if override is USE_DEFAULT_BUDGET else override

    def _snapshot(self, contract_id: str, version: int) -> Contract:
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

    def _check_fields(
        self,
        fields: tuple[Any, ...],
        payload: Any,
        deadline: _Deadline,
    ) -> list[ValidationIssue]:
        """对一侧字段形状做纯检查，返回原始问题列表。"""
        issues: list[ValidationIssue] = []
        if not isinstance(payload, dict):
            issues.append(
                ValidationIssue(path="$", code="not_an_object", message="payload must be an object")
            )
            return issues
        for f in fields:
            deadline.tick()
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
        return issues

    def _finalize(self, issues: list[ValidationIssue]) -> tuple[ValidationIssue, ...]:
        """应用截断与详情隐藏配置。"""
        truncated = len(issues) > self._limits.max_validation_issues
        issues = issues[: self._limits.max_validation_issues]
        if truncated:
            issues.append(
                ValidationIssue(
                    path="$",
                    code="issues_truncated",
                    message=f"issue list truncated at {self._limits.max_validation_issues}",
                )
            )
        if not self._limits.expose_error_details:
            issues = [
                ValidationIssue(path=i.path, code=i.code, message=_HIDDEN_MESSAGE)
                for i in issues
            ]
        return tuple(issues)

    def _validate_once(
        self, contract_id: str, version: int, payload: Any, timeout: float
    ) -> ValidationResult:
        deadline = _Deadline(timeout)
        contract = self._snapshot(contract_id, version)
        issues = self._finalize(self._check_fields(contract.spec.fields, payload, deadline))
        return ValidationResult(
            ok=not issues,
            contract_id=contract_id,
            version=version,
            issues=issues,
        )

    def validate(
        self,
        contract_id: str,
        version: int,
        payload: Any,
        *,
        timeout_seconds: Any = USE_DEFAULT_BUDGET,
    ) -> ValidationResult:
        """单侧校验：校验契约的请求侧形状（老契约沿用原方式）。"""
        timeout = self._resolve_timeout(timeout_seconds)
        return run_with_budget(
            timeout, lambda: self._validate_once(contract_id, version, payload, timeout)
        )

    def _check_side(
        self,
        spec: ContractSpec,
        side: Side,
        payload: Any,
        deadline: _Deadline,
    ) -> SideValidationResult:
        """单侧（请求/响应）的归一化结论。"""
        issues = self._finalize(self._check_fields(spec.fields_of(side), payload, deadline))
        return SideValidationResult(side=side, ok=not issues, issues=issues)

    def _validate_pair_once(
        self,
        contract_id: str,
        version: int,
        request_payload: Any,
        response_payload: Any,
        timeout: float,
    ) -> PairValidationResult:
        deadline = _Deadline(timeout)
        contract = self._snapshot(contract_id, version)
        if not contract.spec.is_paired:
            raise GovernanceError(
                Reason.INVALID_CONTRACT,
                f"contract '{contract_id}' does not declare a response side",
            )
        # 两侧在同一预算内顺序检查：任一侧超时都整体拒绝，不产生半截结果。
        request_result = self._check_side(
            contract.spec, Side.REQUEST, request_payload, deadline
        )
        response_result = self._check_side(
            contract.spec, Side.RESPONSE, response_payload, deadline
        )
        return PairValidationResult(
            ok=request_result.ok and response_result.ok,
            contract_id=contract_id,
            version=version,
            request=request_result,
            response=response_result,
        )

    def validate_pair(
        self,
        contract_id: str,
        version: int,
        request_payload: Any,
        response_payload: Any,
        *,
        timeout_seconds: Any = USE_DEFAULT_BUDGET,
    ) -> PairValidationResult:
        """成对校验：一次提交请求侧与响应侧内容，问题按侧归属。"""
        timeout = self._resolve_timeout(timeout_seconds)
        return run_with_budget(
            timeout,
            lambda: self._validate_pair_once(
                contract_id, version, request_payload, response_payload, timeout
            ),
        )
