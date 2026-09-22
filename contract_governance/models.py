"""契约治理领域模型：类型定义与归一化结果结构。"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class ContractStatus(enum.Enum):
    """契约生命周期状态。"""

    ACTIVE = "active"
    REVOKED = "revoked"


class FieldType(enum.Enum):
    """契约字段支持的类型。"""

    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    OBJECT = "object"
    ARRAY = "array"


class ChangeKind(enum.Enum):
    """相邻版本间变更的分类。"""

    FIELD_REMOVED = "field_removed"
    TYPE_NARROWED = "type_narrowed"
    TYPE_WIDENED = "type_widened"
    REQUIRED_EXPANDED = "required_expanded"
    REQUIRED_RELAXED = "required_relaxed"
    FIELD_ADDED_OPTIONAL = "field_added_optional"


class Verdict(enum.Enum):
    """兼容性判定结论。"""

    COMPATIBLE = "compatible"
    BREAKING = "breaking"


@dataclass(frozen=True)
class FieldSpec:
    """单个字段的契约定义。"""

    name: str
    type: FieldType
    required: bool = False


@dataclass(frozen=True)
class ContractSpec:
    """某一版本契约的完整定义（不可变，保证并发读取安全）。"""

    fields: tuple[FieldSpec, ...] = ()


@dataclass(frozen=True)
class Contract:
    """已注册契约：标识 + 版本 + 定义 + 状态。"""

    contract_id: str
    version: int
    spec: ContractSpec
    status: ContractStatus = ContractStatus.ACTIVE


@dataclass(frozen=True)
class Change:
    """一次版本间变更的归一化描述。"""

    kind: ChangeKind
    field_name: str
    detail: str


@dataclass(frozen=True)
class CompatReport:
    """兼容性判定报告：结论 + 判定依据，可复现。"""

    contract_id: str
    from_version: int
    to_version: int
    verdict: Verdict
    changes: tuple[Change, ...] = ()


@dataclass(frozen=True)
class ValidationIssue:
    """运行时校验发现的单个问题。"""

    path: str
    code: str
    message: str


@dataclass(frozen=True)
class ValidationResult:
    """运行时校验的归一化结果。"""

    ok: bool
    contract_id: str
    version: int
    issues: tuple[ValidationIssue, ...] = ()
