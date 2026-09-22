"""基于已注册契约的运行时请求/响应校验。

校验为纯函数：只读取入参与契约快照，不写入注册表，因此失败不会污染
已注册状态，也不会影响后续请求。

违规类型（violation.reason，可区分）：

- missing_required：契约要求必填但载荷缺失或为 null
- wrong_type：字段存在但类型不符
- unknown_field：契约未声明的字段（契约 allow_extra=False 时）

上限策略：

- max_payload_nodes：载荷 JSON 节点数，超限按 REJECT 抛 LIMIT_EXCEEDED
- max_validation_ms：校验耗时预算，超限按 REJECT 抛 LIMIT_EXCEEDED
- max_error_details：违规条目输出上限，TRUNCATE 时截断违规列表
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .errors import ContractError, ErrorCode
from .limits import LimitAction, LimitConfig
from .contract import Contract

_PY_TYPE_TO_KIND = {
    str: "STRING",
    bool: "BOOLEAN",
    int: "INTEGER",
    float: "NUMBER",
    dict: "OBJECT",
    list: "ARRAY",
}


@dataclass(frozen=True)
class FieldViolation:
    path: str
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class ValidationOutcome:
    valid: bool
    violations: tuple[FieldViolation, ...] = ()
    truncated: bool = False

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "violations": [
                {"path": v.path, "reason": v.reason, "detail": v.detail}
                for v in self.violations
            ],
            "truncated": self.truncated,
        }


def _count_nodes(value: object) -> int:
    if isinstance(value, dict):
        return 1 + sum(_count_nodes(v) for v in value.values())
    if isinstance(value, list):
        return 1 + sum(_count_nodes(v) for v in value)
    return 1


def _type_matches(value: object, declared: str) -> bool:
    if value is None:
        return True  # null 由 missing_required 规则处理
    actual = _PY_TYPE_TO_KIND.get(type(value))
    if actual is None:
        return False
    if declared == "NUMBER":
        # 整数可作为 NUMBER 使用（宽类型接受），布尔不算数字
        return actual in ("NUMBER", "INTEGER")
    if declared == "INTEGER":
        return actual == "INTEGER"
    return actual == declared


def _check_section(
    section: str,
    payload: object,
    contract: Contract,
    limits: LimitConfig,
    started_at: float,
) -> list[FieldViolation]:
    if not isinstance(payload, dict):
        raise ContractError(
            ErrorCode.INVALID_CONTRACT,
            f"{section} 必须是 JSON 对象",
            {"field": section, "reason": "not_an_object"},
        )
    nodes = _count_nodes(payload)
    if nodes > limits.max_payload_nodes:
        raise ContractError(
            ErrorCode.LIMIT_EXCEEDED,
            f"{section} 节点数超过上限",
            {"section": section, "limit": limits.max_payload_nodes, "actual": nodes},
        )
    elapsed_ms = (time.perf_counter() - started_at) * 1000
    if elapsed_ms > limits.max_validation_ms:
        raise ContractError(
            ErrorCode.LIMIT_EXCEEDED,
            "校验耗时超过预算",
            {"limit_ms": limits.max_validation_ms, "elapsed_ms": round(elapsed_ms, 3)},
        )

    field_map = contract.field_map()
    violations: list[FieldViolation] = []
    for spec in contract.fields:
        present = spec.name in payload and payload[spec.name] is not None
        if not present:
            if spec.required:
                violations.append(
                    FieldViolation(
                        path=f"{section}.{spec.name}",
                        reason="missing_required",
                        detail="必填字段缺失或为 null",
                    )
                )
            continue
        value = payload[spec.name]
        if not _type_matches(value, spec.type):
            actual = _PY_TYPE_TO_KIND.get(type(value), "UNKNOWN")
            violations.append(
                FieldViolation(
                    path=f"{section}.{spec.name}",
                    reason="wrong_type",
                    detail=f"声明 {spec.type}，实际 {actual}",
                )
            )
    allow_extra = bool(contract.normalized.get("allow_extra", False))
    if not allow_extra:
        for name in payload:
            if name not in field_map:
                violations.append(
                    FieldViolation(
                        path=f"{section}.{name}",
                        reason="unknown_field",
                        detail="契约未声明该字段",
                    )
                )
    return violations


def validate_payload(
    contract: Contract,
    payload: dict,
    limits: LimitConfig,
) -> ValidationOutcome:
    """对 request/response 两个区段做校验，返回可复现的违规列表。"""
    started_at = time.perf_counter()
    if not isinstance(payload, dict):
        raise ContractError(
            ErrorCode.INVALID_CONTRACT,
            "校验载荷必须是 JSON 对象",
            {"field": "$", "reason": "not_an_object"},
        )
    request = payload.get("request", {})
    response = payload.get("response", {})
    violations = _check_section("request", request, contract, limits, started_at)
    violations += _check_section("response", response, contract, limits, started_at)
    violations.sort(key=lambda v: (v.path, v.reason))

    truncated = False
    if len(violations) > limits.max_error_details:
        if limits.on_limit_exceeded == LimitAction.REJECT:
            raise ContractError(
                ErrorCode.LIMIT_EXCEEDED,
                "校验违规条目超过上限",
                {"limit": limits.max_error_details, "actual": len(violations)},
            )
        violations = violations[: limits.max_error_details]
        truncated = True
    return ValidationOutcome(
        valid=not violations, violations=tuple(violations), truncated=truncated
    )
