"""契约注册表：注册、查询、更新、撤销；并发安全。

并发语义：
- 所有写操作在同一把可重入锁内完成「校验 + 落库」，写与写之间原子，
  不会出现半更新或覆盖丢失。
- 读操作返回的是不可变 Contract 快照，读取方不会观察到未完成的写入。
- 任何校验失败都发生在落库之前，失败路径不残留部分状态。
- 每个版本的定义一旦写入即不可变并永久保留，保证兼容性判定可复现。
"""

from __future__ import annotations

import threading

from .config import GovernanceLimits
from .errors import GovernanceError, Reason
from .models import Contract, ContractSpec, ContractStatus


class ContractRegistry:
    """线程安全的契约注册表（保留全部历史版本）。"""

    def __init__(self, limits: GovernanceLimits | None = None) -> None:
        self._limits = limits or GovernanceLimits()
        self._lock = threading.RLock()
        # contract_id -> {version -> Contract}；当前版本为最大键。
        self._contracts: dict[str, dict[int, Contract]] = {}

    def _check_spec(self, spec: ContractSpec) -> None:
        if not isinstance(spec, ContractSpec):
            raise GovernanceError(Reason.INVALID_CONTRACT, "spec must be a ContractSpec")
        if len(spec.fields) > self._limits.max_fields_per_contract:
            raise GovernanceError(
                Reason.LIMIT_EXCEEDED,
                f"contract exceeds max fields per contract ({self._limits.max_fields_per_contract})",
            )
        names = [f.name for f in spec.fields]
        if len(names) != len(set(names)):
            raise GovernanceError(Reason.INVALID_CONTRACT, "duplicate field names in spec")

    def _history(self, contract_id: str) -> dict[int, Contract]:
        history = self._contracts.get(contract_id)
        if history is None:
            raise GovernanceError(Reason.CONTRACT_NOT_FOUND, f"contract '{contract_id}' not found")
        return history

    @staticmethod
    def _current(history: dict[int, Contract]) -> Contract:
        return history[max(history)]

    def register(self, contract_id: str, spec: ContractSpec) -> Contract:
        """注册新契约（初始版本为 1）。重复注册被拒绝。"""
        if not contract_id:
            raise GovernanceError(Reason.INVALID_CONTRACT, "contract_id must be non-empty")
        with self._lock:
            self._check_spec(spec)
            existing = self._contracts.get(contract_id)
            if existing is not None:
                # 已撤销与活跃状态都视为占用标识，原因可区分。
                current = self._current(existing)
                reason = (
                    Reason.CONTRACT_REVOKED
                    if current.status is ContractStatus.REVOKED
                    else Reason.CONTRACT_EXISTS
                )
                raise GovernanceError(reason, f"contract '{contract_id}' already registered")
            if len(self._contracts) >= self._limits.max_contracts:
                raise GovernanceError(
                    Reason.LIMIT_EXCEEDED,
                    f"registry capacity exceeded ({self._limits.max_contracts})",
                )
            contract = Contract(contract_id=contract_id, version=1, spec=spec)
            self._contracts[contract_id] = {1: contract}
            return contract

    def get(self, contract_id: str) -> Contract:
        """查询当前版本快照；不存在则抛 CONTRACT_NOT_FOUND。"""
        with self._lock:
            return self._current(self._history(contract_id))

    def get_version(self, contract_id: str, version: int) -> Contract:
        """查询指定历史版本快照。"""
        with self._lock:
            history = self._history(contract_id)
            contract = history.get(version)
            if contract is None:
                raise GovernanceError(
                    Reason.VERSION_MISMATCH,
                    f"contract '{contract_id}' has no version {version}",
                )
            return contract

    def update(self, contract_id: str, version: int, spec: ContractSpec) -> Contract:
        """更新契约到新版本。仅允许 version == 当前版本 + 1 的线性流转。"""
        with self._lock:
            self._check_spec(spec)
            history = self._history(contract_id)
            current = self._current(history)
            if current.status is ContractStatus.REVOKED:
                raise GovernanceError(Reason.CONTRACT_REVOKED, f"contract '{contract_id}' is revoked")
            if version != current.version + 1:
                raise GovernanceError(
                    Reason.ILLEGAL_VERSION_TRANSITION,
                    f"illegal version transition: {current.version} -> {version}",
                )
            updated = Contract(
                contract_id=contract_id,
                version=version,
                spec=spec,
                status=ContractStatus.ACTIVE,
            )
            history[version] = updated
            return updated

    def revoke(self, contract_id: str) -> Contract:
        """撤销契约。撤销后不得再更新或参与校验。"""
        with self._lock:
            history = self._history(contract_id)
            current = self._current(history)
            if current.status is ContractStatus.REVOKED:
                raise GovernanceError(Reason.CONTRACT_REVOKED, f"contract '{contract_id}' already revoked")
            revoked = Contract(
                contract_id=contract_id,
                version=current.version,
                spec=current.spec,
                status=ContractStatus.REVOKED,
            )
            history[current.version] = revoked
            return revoked
