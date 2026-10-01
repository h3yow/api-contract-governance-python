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
    """成对校验中的数据侧别。"""

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

    - 仅声明 fields 时为老的单侧（请求形状）契约，沿用原有校验方式。
    - 额外声明 response_fields 时为成对契约：请求侧与响应侧形状分开描述。
      response_fields=() 表示成对但响应侧无字段约束，与 None（老契约）区分。
    """

    fields: tuple[FieldSpec, ...] = ()
    response_fields: tuple[FieldSpec, ...] | None = None

    @property
    def is_paired(self) -> bool:
        """是否成对声明了请求侧与响应侧。"""
        return self.response_fields is not None

    def fields_of(self, side: "Side") -> tuple[FieldSpec, ...]:
        """取指定侧的字段形状。"""
        return self.fields if side is Side.REQUEST else (self.response_fields or ())


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

    side 标记变化发生在哪一侧（请求/响应），单侧契约的变更归为请求侧；
    缺省 None 仅为兼容旧的构造方式。
    """

    kind: ChangeKind
    field_name: str
    detail: str
    side: "Side | None" = None


@dataclass(frozen=True)
class CompatReport:
    """兼容性判定报告：结论 + 判定依据，可复现。

    成对契约请求侧与响应侧分别判定：request_verdict / response_verdict
    各自给出单侧结论，整体 verdict 为两侧中最严格者（任一侧破坏性则整体
    破坏性）。response_verdict 为 None 表示该契约未声明响应侧形状。
    """

    contract_id: str
    from_version: int
    to_version: int
    verdict: Verdict
    changes: tuple[Change, ...] = ()
    request_verdict: Verdict = Verdict.COMPATIBLE
    response_verdict: Verdict | None = None


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
    """成对校验中单侧的校验结论。"""

    side: Side
    ok: bool
    issues: tuple[ValidationIssue, ...] = ()


@dataclass(frozen=True)
class PairValidationResult:
    """成对校验的合并结论：两侧问题分别归属，任一侧不过则整体不过。"""

    ok: bool
    contract_id: str
    version: int
    request: SideValidationResult
    response: SideValidationResult

    def side(self, side: Side) -> SideValidationResult:
        """按侧别取单侧结论。"""
        return self.request if side is Side.REQUEST else self.response


@dataclass(frozen=True)
class ReleaseDecision:
    """一次版本发布门禁的判定依据与确认结果，落库后可重新查到。

    成对契约的分侧结论（request_verdict / response_verdict）随判定一起
    留档：任一侧为破坏性时整体 verdict 即为破坏性，需显式接受才放行。
    """

    contract_id: str
    from_version: int
    to_version: int
    verdict: Verdict
    changes: tuple[Change, ...]
    accepted_breaking: bool
    allowed: bool
    request_verdict: Verdict = Verdict.COMPATIBLE
    response_verdict: Verdict | None = None
