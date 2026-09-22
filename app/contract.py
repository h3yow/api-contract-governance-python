"""契约数据模型与归一化。

契约外部格式（JSON）::

    {
      "id": "user-service",            # 字母开头，字母/数字/_/-，长度 1..64
      "version": "1.0.0",              # 语义版本 X.Y.Z
      "fields": [
        {"name": "user_id", "type": "STRING", "required": true},
        {"name": "age", "type": "INTEGER", "required": false}
      ]
    }

归一化结果按字段名排序、去重，保证同一输入的规范化表示可复现。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .errors import ContractError, ErrorCode
from .limits import LimitConfig
from .version import Version, parse_version

_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")

SUPPORTED_TYPES = frozenset(
    {"STRING", "INTEGER", "NUMBER", "BOOLEAN", "OBJECT", "ARRAY"}
)


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: str
    required: bool = False

    def to_dict(self) -> dict:
        return {"name": self.name, "type": self.type, "required": self.required}


@dataclass(frozen=True)
class Contract:
    contract_id: str
    version: Version
    fields: tuple[FieldSpec, ...]
    revision: int = 0
    revoked: bool = False
    normalized: dict = field(default_factory=dict)

    def field_map(self) -> dict[str, FieldSpec]:
        return {f.name: f for f in self.fields}

    def to_public_dict(self) -> dict:
        return {
            "id": self.contract_id,
            "version": str(self.version),
            "revision": self.revision,
            "revoked": self.revoked,
            "fields": [f.to_dict() for f in self.fields],
            "normalized": self.normalized,
        }

    def with_fields(self, fields: tuple[FieldSpec, ...], normalized: dict) -> "Contract":
        """返回字段集合更新后的新契约（revision +1）。"""
        return Contract(
            contract_id=self.contract_id,
            version=self.version,
            fields=fields,
            revision=self.revision + 1,
            revoked=self.revoked,
            normalized=normalized,
        )

    def as_revoked(self) -> "Contract":
        return Contract(
            contract_id=self.contract_id,
            version=self.version,
            fields=self.fields,
            revision=self.revision + 1,
            revoked=True,
            normalized=self.normalized,
        )


def _invalid(field_name: str, reason: str, value: object = None) -> ContractError:
    details = {"field": field_name, "reason": reason}
    if value is not None:
        details["value"] = value
    return ContractError(ErrorCode.INVALID_CONTRACT, "契约格式不合法", details)


def normalize_contract(raw: dict, limits: LimitConfig | None = None) -> Contract:
    """从外部 dict 构造归一化契约。

    所有非法输入抛 INVALID_CONTRACT（details.reason 可区分具体原因）；
    字段数超 max_fields 抛 LIMIT_EXCEEDED。
    """
    limits = limits or LimitConfig()
    if not isinstance(raw, dict):
        raise _invalid("$", "not_an_object")

    contract_id = raw.get("id")
    if not isinstance(contract_id, str) or not _ID_RE.match(contract_id):
        raise _invalid("id", "invalid_id", contract_id)

    version = parse_version(raw.get("version"))

    raw_fields = raw.get("fields")
    if not isinstance(raw_fields, list):
        raise _invalid("fields", "not_an_array")

    if len(raw_fields) > limits.max_fields:
        raise ContractError(
            ErrorCode.LIMIT_EXCEEDED,
            "契约字段数超过上限",
            {"limit": limits.max_fields, "actual": len(raw_fields)},
        )

    specs: list[FieldSpec] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_fields):
        path = f"fields[{index}]"
        if not isinstance(item, dict):
            raise _invalid(path, "field_not_an_object")
        name = item.get("name")
        if not isinstance(name, str) or not re.match(r"^[A-Za-z][A-Za-z0-9_]{0,63}$", name):
            raise _invalid(f"{path}.name", "invalid_field_name", name)
        if name in seen:
            raise _invalid(f"{path}.name", "duplicate_field", name)
        seen.add(name)
        ftype = item.get("type")
        if not isinstance(ftype, str) or ftype not in SUPPORTED_TYPES:
            raise _invalid(f"{path}.type", "unsupported_type", ftype)
        required = item.get("required", False)
        if not isinstance(required, bool):
            raise _invalid(f"{path}.required", "not_a_boolean", required)
        specs.append(FieldSpec(name=name, type=ftype, required=required))

    specs.sort(key=lambda f: f.name)
    fields = tuple(specs)
    normalized = {
        "id": contract_id,
        "version": str(version),
        "fields": [
            {"name": f.name, "type": f.type, "required": f.required} for f in fields
        ],
    }
    return Contract(
        contract_id=contract_id,
        version=version,
        fields=fields,
        revision=0,
        revoked=False,
        normalized=normalized,
    )
