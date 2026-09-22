"""基于已注册契约的运行时请求/响应校验。

语义：
- 未注册        -> 抛 CONTRACT_NOT_FOUND
- 已撤销        -> 抛 CONTRACT_REVOKED（撤销后不得参与校验）
- 版本不匹配    -> 抛 VERSION_MISMATCH
- 校验失败      -> 返回 ValidationResult(ok=False, issues=...)，不抛异常、
                   不修改注册表，失败不污染已注册状态。

校验为纯读取操作：先取不可变快照再校验，并发写入不会影响到进行中的校验。
"""

from __future__ import annotations

from typing import Any

from .config import GovernanceLimits
from .errors import GovernanceError, Reason
from .models import (
    ContractStatus,
    FieldType,
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


class ContractValidator:
    """运行时校验器。"""

    def __init__(self, registry: ContractRegistry, limits: GovernanceLimits | None = None) -> None:
        self._registry = registry
        self._limits = limits or GovernanceLimits()

    def validate(self, contract_id: str, version: int, payload: Any) -> ValidationResult:
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

        issues: list[ValidationIssue] = []
        if not isinstance(payload, dict):
            issues.append(
                ValidationIssue(path="$", code="not_an_object", message="payload must be an object")
            )
        else:
            for f in contract.spec.fields:
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

        return ValidationResult(
            ok=not issues,
            contract_id=contract_id,
            version=version,
            issues=tuple(issues),
        )
