"""治理服务门面：组合注册表、校验器与兼容性判定。

版本发布有两条入口：
- update：原有无门禁更新（语义保持不变）。
- publish：新版本落库前先过破坏性变更门禁 —— 破坏性变更默认拒绝
  （BREAKING_CHANGE_REJECTED），与跳号/回退/重复的 ILLEGAL_VERSION_TRANSITION
  可区分；仅当 accept_breaking=True 显式确认时才落库。落库后该版本的
  判定依据与确认结果可通过 get_publish_decision 重新查到。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .compat import judge_compatibility
from .config import GovernanceLimits
from .models import (
    CompatReport,
    Contract,
    ContractSpec,
    PairedValidationResult,
    PublishDecision,
    ValidationResult,
    Verdict,
)
from .registry import ContractRegistry
from .validation import MISSING, ContractValidator
from .errors import GovernanceError, Reason


class GovernanceService:
    """对外统一入口。"""

    def __init__(
        self,
        limits: GovernanceLimits | None = None,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._limits = limits or GovernanceLimits()
        self._registry = ContractRegistry(self._limits)
        self._validator = ContractValidator(self._registry, self._limits, clock=clock)

    def register(self, contract_id: str, spec: ContractSpec) -> Contract:
        return self._registry.register(contract_id, spec)

    def get(self, contract_id: str) -> Contract:
        return self._registry.get(contract_id)

    def get_version(self, contract_id: str, version: int) -> Contract:
        return self._registry.get_version(contract_id, version)

    def update(self, contract_id: str, version: int, spec: ContractSpec) -> Contract:
        return self._registry.update(contract_id, version, spec)

    def revoke(self, contract_id: str) -> Contract:
        return self._registry.revoke(contract_id)

    def validate(
        self,
        contract_id: str,
        version: int,
        payload: Any,
        timeout: float | None = -1.0,
    ) -> ValidationResult:
        """time budget：负数（默认）沿用配置；None 显式禁用预算；正数为本次覆盖。"""
        return self._validator.validate(contract_id, version, payload, timeout=timeout)

    def validate_pair(
        self,
        contract_id: str,
        version: int,
        *,
        request: Any = MISSING,
        response: Any = MISSING,
        timeout: float | None = -1.0,
    ) -> PairedValidationResult:
        """time budget：负数（默认）沿用配置；None 显式禁用预算。"""
        return self._validator.validate_pair(
            contract_id, version, request=request, response=response, timeout=timeout
        )

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
    ) -> Contract:
        """带破坏性变更门禁的版本发布。

        流程（失败均发生在落库之前）：
        1. 非法规格先被拒绝（INVALID_CONTRACT / LIMIT_EXCEEDED）。
        2. 读取当前版本快照并做相邻版本兼容性判定；跳号/回退/重复由提交时的
           线性流转校验拒绝（ILLEGAL_VERSION_TRANSITION）。
        3. 判定为 breaking 且未显式确认 -> BREAKING_CHANGE_REJECTED，不落库。
        4. compatible 或显式接受 breaking -> 原子提交版本并落库门禁记录；
           并发提交同一目标版本时恰有一个成功，其余为非法流转。
        """
        self._registry.validate_spec(spec)
        current = self._registry.get(contract_id)  # 未注册 -> CONTRACT_NOT_FOUND
        # 用未入库的候选契约参与纯函数判定；相邻关系不满足时由判定抛非法流转。
        candidate = Contract(
            contract_id=contract_id,
            version=version,
            spec=spec,
            status=current.status,
        )
        report = judge_compatibility(current, candidate)
        if report.verdict is Verdict.BREAKING and not accept_breaking:
            raise GovernanceError(
                Reason.BREAKING_CHANGE_REJECTED,
                f"publishing version {version} of '{contract_id}' introduces breaking "
                "changes; retry with accept_breaking=True to confirm",
            )
        return self._registry.commit_version(
            contract_id,
            version,
            spec,
            report.verdict,
            report.changes,
            breaking_accepted=report.verdict is Verdict.BREAKING and accept_breaking,
        )

    def get_publish_decision(self, contract_id: str, version: int) -> PublishDecision:
        """重新查到某次已发布版本的门禁判定依据与确认结果。"""
        return self._registry.get_publish_decision(contract_id, version)
