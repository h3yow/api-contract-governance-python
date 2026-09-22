"""编排门面：解析 HTTP 语义的入参，委托注册表完成治理动作。

该层不引入新的状态，所有并发与原子性保证都来自 Registry。
"""

from __future__ import annotations

from .errors import ContractError, ErrorCode
from .registry import Registry, get_registry


class GovernanceService:
    def __init__(self, registry: Registry | None = None) -> None:
        self._registry = registry or get_registry()

    def register_contract(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ContractError(
                ErrorCode.INVALID_CONTRACT,
                "请求体必须是 JSON 对象",
                {"field": "$", "reason": "not_an_object"},
            )
        contract = self._registry.register(payload)
        return contract.to_public_dict()

    def get_contract(self, contract_id: str, version: str) -> dict:
        contract = self._registry.get(contract_id, version)
        return contract.to_public_dict()

    def update_contract(self, contract_id: str, version: str, body: dict) -> dict:
        fields, expected_revision = self._fields_and_revision(body)
        contract = self._registry.update(
            contract_id, version, expected_revision, fields
        )
        return contract.to_public_dict()

    def revoke_contract(self, contract_id: str, version: str, body: dict) -> dict:
        _fields, expected_revision = self._fields_and_revision(body)
        contract = self._registry.revoke(contract_id, version, expected_revision)
        return contract.to_public_dict()

    def validate_call(self, contract_id: str, version: str, body: dict) -> dict:
        if not isinstance(body, dict):
            raise ContractError(
                ErrorCode.INVALID_CONTRACT,
                "校验请求体必须是 JSON 对象",
                {"field": "$", "reason": "not_an_object"},
            )
        declared_version = body.get("declared_version")
        if declared_version is not None and not isinstance(declared_version, str):
            raise ContractError(
                ErrorCode.INVALID_CONTRACT,
                "declared_version 必须是字符串",
                {"field": "declared_version", "reason": "not_a_string"},
            )
        outcome = self._registry.validate(
            contract_id,
            version,
            body.get("request", {}),
            body.get("response", {}),
            declared_version=declared_version,
        )
        return {"id": contract_id, "version": version, **outcome.to_dict()}

    def check_compatibility(
        self, contract_id: str, from_version: str, to_version: str
    ) -> dict:
        report = self._registry.compatibility(contract_id, from_version, to_version)
        return report.to_dict()

    @staticmethod
    def _fields_and_revision(body: dict) -> tuple[list, int]:
        if not isinstance(body, dict):
            raise ContractError(
                ErrorCode.INVALID_CONTRACT,
                "请求体必须是 JSON 对象",
                {"field": "$", "reason": "not_an_object"},
            )
        expected_revision = body.get("expected_revision")
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
            raise ContractError(
                ErrorCode.INVALID_CONTRACT,
                "expected_revision 必须是非负整数（乐观并发凭证）",
                {"field": "expected_revision", "reason": "invalid_revision"},
            )
        if expected_revision < 0:
            raise ContractError(
                ErrorCode.INVALID_CONTRACT,
                "expected_revision 必须是非负整数",
                {"field": "expected_revision", "reason": "negative_revision"},
            )
        fields = body.get("fields", [])
        if not isinstance(fields, list):
            raise ContractError(
                ErrorCode.INVALID_CONTRACT,
                "fields 必须是数组",
                {"field": "fields", "reason": "not_an_array"},
            )
        return fields, expected_revision
