"""治理服务门面：组合注册表、校验器与兼容性判定。"""

from __future__ import annotations

from typing import Any

from .compat import judge_compatibility
from .config import GovernanceLimits
from .models import CompatReport, Contract, ContractSpec, ValidationResult
from .registry import ContractRegistry
from .validation import ContractValidator


class GovernanceService:
    """对外统一入口（骨架）。"""

    def __init__(self, limits: GovernanceLimits | None = None) -> None:
        self._limits = limits or GovernanceLimits()
        self._registry = ContractRegistry(self._limits)
        self._validator = ContractValidator(self._registry, self._limits)

    def register(self, contract_id: str, spec: ContractSpec) -> Contract:
        return self._registry.register(contract_id, spec)

    def get(self, contract_id: str) -> Contract:
        return self._registry.get(contract_id)

    def update(self, contract_id: str, version: int, spec: ContractSpec) -> Contract:
        return self._registry.update(contract_id, version, spec)

    def revoke(self, contract_id: str) -> Contract:
        return self._registry.revoke(contract_id)

    def validate(self, contract_id: str, version: int, payload: Any) -> ValidationResult:
        return self._validator.validate(contract_id, version, payload)

    def check_compatibility(self, contract_id: str, from_version: int, to_version: int) -> CompatReport:
        old = self._registry.get_version(contract_id, from_version)
        new = self._registry.get_version(contract_id, to_version)
        return judge_compatibility(old, new)
