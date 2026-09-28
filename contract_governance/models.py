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


class Side(enum.Enum):
    """成对校验中可区分的两侧。"""

    REQUEST = "request"
    RESPONSE = "response"


@dataclass(frozen=True)
class FieldSpec:
    """单个字段的契约定义。"""

    name: str
    type: FieldType
    required: bool = False


@dataclass(frozen=True)
class ContractSpec:
    """某一版本契约的完整定义（不可变，保证并发读取安全）。

    两种写法：
    - 单侧老契约：只声明 fields，按原有方式校验。
    - 成对新契约：声明 request_fields / response_fields（可只声明一侧），
      通过成对校验入口提交两侧内容。
    """

    fields: tuple[FieldSpec, ...] = ()
    request_fields: tuple[FieldSpec, ...] = ()
    response_fields: tuple[FieldSpec, ...] = ()

    def sides(self) -> dict[Side, tuple[FieldSpec, ...]]:
        """返回声明过的侧 -> 字段形状，未声明的侧不出现在结果中。"""
        declared: dict[Side, tuple[FieldSpec, ...]] = {}
        if self.request_fields:
            declared[Side.REQUEST] = self.request_fields
        if self.response_fields:
            declared[Side.RESPONSE] = self.response_fields
        return declared


@dataclass(frozen=True)
class Contract:
    """已注册契约：标识 + 版本 + 定义 + 状态。"""

    contract_id: str
    version: int
    spec: ContractSpec
    status: ContractStatus = ContractStatus.ACTIVE


@dataclass(frozen=True)
class Change:
    """一次版本间变更的归一化描述。

    side 为 None 表示来自单侧老契约的形状；成对契约按请求/响应侧归属。
    """

    kind: ChangeKind
    field_name: str
    detail: str
    side: Side | None = None


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


@dataclass(frozen=True)
class SideValidationResult:
    """成对校验中单侧的结果：问题归属明确，两侧结果互不覆盖。"""

    side: Side
    ok: bool
    issues: tuple[ValidationIssue, ...] = ()


@dataclass(frozen=True)
class PairedValidationResult:
    """成对校验的归一化结果：按侧区分问题，任一侧不过则整体不过。"""

    ok: bool
    contract_id: str
    version: int
    sides: tuple[SideValidationResult, ...] = ()


@dataclass(frozen=True)
class PublishDecision:
    """一次版本发布门禁的判定与确认记录，落库后可重新查到。"""

    contract_id: str
    version: int
    verdict: Verdict
    changes: tuple[Change, ...] = ()
    breaking_accepted: bool = False
