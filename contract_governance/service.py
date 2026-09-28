"""治理服务门面：组合注册表、校验器、兼容性判定与发布门禁。"""

from __future__ import annotations

import threading
from typing import Any

from .compat import judge_compatibility
from .config import GovernanceLimits
from .errors import GovernanceError, Reason
from .models import (
    CompatReport,
    Contract,
    ContractSpec,
    PairValidationResult,
    ReleaseDecision,
    ValidationResult,
)
from .publishing import apply_release, evaluate_release
from .registry import ContractRegistry
from .deadline import USE_DEFAULT_BUDGET
from .validation import ContractValidator


class GovernanceService:
    """对外统一入口。"""

    def __init__(
        self,
        limits: GovernanceLimits | None = None,
        *,
        registry: ContractRegistry | None = None,
        validator: ContractValidator | None = None,
    ) -> None:
        self._limits = limits or GovernanceLimits()
        self._registry = registry or ContractRegistry(self._limits)
        self._validator = validator or ContractValidator(self._registry, self._limits)
        # (contract_id, to_version) -> 成功发布的门禁判定与确认结果。
        self._releases: dict[tuple[str, int], ReleaseDecision] = {}
        self._release_lock = threading.Lock()

    def register(self, contract_id: str, spec: ContractSpec) -> Contract:
        return self._registry.register(contract_id, spec)

    def get(self, contract_id: str) -> Contract:
        return self._registry.get(contract_id)

    def update(self, contract_id: str, version: int, spec: ContractSpec) -> Contract:
        """直接线性更新（不带破坏性门禁，保持历史语义）。"""
        return self._registry.update(contract_id, version, spec)

    def revoke(self, contract_id: str) -> Contract:
        return self._registry.revoke(contract_id)

    def validate(
        self,
        contract_id: str,
        version: int,
        payload: Any,
        *,
        timeout_seconds: float | None = None,
    ) -> ValidationResult:
        return self._validator.validate(
            contract_id,
            version,
            payload,
            timeout_seconds=self._budget(timeout_seconds),
        )

    def validate_pair(
        self,
        contract_id: str,
        version: int,
        request_payload: Any,
        response_payload: Any,
        *,
        timeout_seconds: float | None = None,
    ) -> PairValidationResult:
        """成对校验：两侧一次提交，问题按侧归属，受同一时间预算约束。"""
        return self._validator.validate_pair(
            contract_id,
            version,
            request_payload,
            response_payload,
            timeout_seconds=self._budget(timeout_seconds),
        )

    @staticmethod
    def _budget(timeout_seconds: float | None) -> Any:
        # None -> 使用 limits 默认预算；显式数值（含 <=0 表示不限）透传。
        return USE_DEFAULT_BUDGET if timeout_seconds is None else timeout_seconds

    def check_compatibility(self, contract_id: str, from_version: int, to_version: int) -> CompatReport:
        old = self._registry.get_version(contract_id, from_version)
        new = self._registry.get_version(contract_id, to_version)
        return judge_compatibility(old, new)

    def publish(
        self,
        contract_id: str,
        version: int,
        spec: ContractSpec,
        *,
        accept_breaking: bool = False,
    ) -> ReleaseDecision:
        """提交新版本：先过相邻版本门禁，破坏性变更需显式接受后落库。"""
        decision = evaluate_release(self._registry, contract_id, version, spec)
        if not decision.allowed and not accept_breaking:
            # 破坏性变更默认拒绝，原因与非法版本流转区分。
            raise GovernanceError(
                Reason.BREAKING_CHANGE_REJECTED,
                f"release {contract_id} {decision.from_version} -> {version} "
                f"contains breaking changes; explicit acceptance required",
            )
        final = apply_release(
            self._registry,
            contract_id,
            version,
            spec,
            accept_breaking=accept_breaking,
            decision=decision,
        )
        with self._release_lock:
            self._releases[(contract_id, version)] = final
        return final

    def get_release_record(self, contract_id: str, version: int) -> ReleaseDecision:
        """重新查询某次成功发布的门禁判定依据与确认结果。"""
        with self._release_lock:
            record = self._releases.get((contract_id, version))
        if record is None:
            raise GovernanceError(
                Reason.RELEASE_RECORD_NOT_FOUND,
                f"no gated release record for '{contract_id}' version {version}",
            )
        return record
