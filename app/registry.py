"""线程安全的契约注册表。

并发模型：

- 所有状态由单一 ``threading.RLock`` 守护；写操作在锁内完成“构造新不可变
  对象 -> 一次性发布”，不会出现半更新；
- 读操作在锁内取得不可变快照（``Contract`` 为 frozen dataclass），随后在
  锁外执行耗时的校验，读取方不可能观察到未完成写入或过期快照；
- update/revoke 采用 revision 乐观并发控制，并发写入冲突时抛
  REVISION_CONFLICT，杜绝覆盖丢失；
- 兼容性结论按 (id, from, to, 双方 revision) 缓存，判定可复现。
"""

from __future__ import annotations

import logging
import threading

from .compat import CompatReport, check_compatibility
from .contract import Contract, normalize_contract
from .errors import ContractError, ErrorCode
from .limits import LimitConfig
from .validation import ValidationOutcome, validate_payload
from .version import Version, parse_version

logger = logging.getLogger("contractgov")


class Registry:
    def __init__(self, limits: LimitConfig | None = None) -> None:
        self._lock = threading.RLock()
        self._limits = limits or LimitConfig()
        self._contracts: dict[str, dict[str, Contract]] = {}
        self._compat_cache: dict[tuple, CompatReport] = {}

    # ------------------------------------------------------------------ #
    # 内部辅助（调用方持锁）
    # ------------------------------------------------------------------ #
    def _snapshot(self, contract_id: str) -> list[Contract]:
        versions = self._contracts.get(contract_id)
        if not versions:
            raise ContractError(
                ErrorCode.NOT_FOUND,
                "契约标识未注册",
                {"id": contract_id},
            )
        return sorted(versions.values(), key=lambda c: c.version)

    def _locate(self, contract_id: str, version: str) -> Contract:
        versions = self._contracts.get(contract_id)
        if not versions:
            raise ContractError(
                ErrorCode.NOT_FOUND, "契约标识未注册", {"id": contract_id}
            )
        contract = versions.get(version)
        if contract is None:
            raise ContractError(
                ErrorCode.NOT_FOUND,
                "该契约版本未注册",
                {"id": contract_id, "version": version},
            )
        return contract

    def _require_active(self, contract: Contract) -> None:
        if contract.revoked:
            raise ContractError(
                ErrorCode.REVOKED,
                "契约版本已撤销，不得参与注册态变更与校验",
                {"id": contract.contract_id, "version": str(contract.version)},
            )

    # ------------------------------------------------------------------ #
    # 注册 / 查询 / 更新 / 撤销
    # ------------------------------------------------------------------ #
    def register(self, raw: dict) -> Contract:
        candidate = normalize_contract(raw, self._limits)
        with self._lock:
            versions = self._contracts.setdefault(candidate.contract_id, {})
            if str(candidate.version) in versions:
                existing = versions[str(candidate.version)]
                raise ContractError(
                    ErrorCode.DUPLICATE_CONTRACT,
                    "同一契约标识的该版本已存在，重复注册被拒绝",
                    {
                        "id": candidate.contract_id,
                        "version": str(candidate.version),
                        "existing_revision": existing.revision,
                    },
                )
            if versions:
                newest = max(versions.values(), key=lambda c: c.version)
                if candidate.version <= newest.version:
                    raise ContractError(
                        ErrorCode.VERSION_NOT_NEWER,
                        "新版本号必须严格大于当前最新版本",
                        {
                            "id": candidate.contract_id,
                            "attempted": str(candidate.version),
                            "latest": str(newest.version),
                        },
                    )
            if len(versions) >= self._limits.max_versions:
                raise ContractError(
                    ErrorCode.LIMIT_EXCEEDED,
                    "契约版本数量超过上限",
                    {"id": candidate.contract_id, "limit": self._limits.max_versions},
                )
            # 一次性发布完整的不可变对象
            versions[str(candidate.version)] = candidate
            logger.info(
                "契约注册 id=%s version=%s normalized=%s",
                candidate.contract_id,
                candidate.version,
                candidate.normalized,
            )
            return candidate

    def get(
        self, contract_id: str, version: str, *, include_revoked: bool = False
    ) -> Contract:
        with self._lock:
            contract = self._locate(contract_id, version)
        if contract.revoked and not include_revoked:
            self._require_active(contract)
        return contract

    def latest(self, contract_id: str) -> Contract:
        with self._lock:
            ordered = self._snapshot(contract_id)
        for contract in reversed(ordered):
            if not contract.revoked:
                return contract
        newest = ordered[-1]
        self._require_active(newest)
        return newest  # pragma: no cover - _require_active 必抛异常

    def list_versions(
        self, contract_id: str, *, include_revoked: bool = False
    ) -> list[Contract]:
        with self._lock:
            ordered = self._snapshot(contract_id)
        if not include_revoked:
            ordered = [c for c in ordered if not c.revoked]
        return ordered

    def update(
        self,
        contract_id: str,
        version: str,
        expected_revision: int,
        raw_fields: dict,
    ) -> Contract:
        with self._lock:
            contract = self._locate(contract_id, version)
            self._require_active(contract)
            if expected_revision != contract.revision:
                raise ContractError(
                    ErrorCode.REVISION_CONFLICT,
                    "revision 与当前注册态不一致，更新被拒绝以防止覆盖丢失",
                    {
                        "id": contract_id,
                        "version": version,
                        "expected": expected_revision,
                        "current": contract.revision,
                    },
                )
            rebuilt = normalize_contract(
                {"id": contract_id, "version": version, "fields": raw_fields},
                self._limits,
            )
            updated = contract.with_fields(rebuilt.fields, rebuilt.normalized)
            self._contracts[contract_id][version] = updated
            # 字段集合变更后，该标识下旧判定结论全部失效
            self._compat_cache = {
                k: v for k, v in self._compat_cache.items() if k[0] != contract_id
            }
            logger.info(
                "契约更新 id=%s version=%s revision=%s normalized=%s",
                contract_id,
                version,
                updated.revision,
                updated.normalized,
            )
            return updated

    def revoke(self, contract_id: str, version: str, expected_revision: int) -> Contract:
        with self._lock:
            contract = self._locate(contract_id, version)
            self._require_active(contract)
            if expected_revision != contract.revision:
                raise ContractError(
                    ErrorCode.REVISION_CONFLICT,
                    "revision 与当前注册态不一致，撤销被拒绝",
                    {
                        "id": contract_id,
                        "version": version,
                        "expected": expected_revision,
                        "current": contract.revision,
                    },
                )
            revoked = contract.as_revoked()
            self._contracts[contract_id][version] = revoked
            logger.info(
                "契约撤销 id=%s version=%s revision=%s",
                contract_id,
                version,
                revoked.revision,
            )
            return revoked

    # ------------------------------------------------------------------ #
    # 运行时校验
    # ------------------------------------------------------------------ #
    def validate(
        self,
        contract_id: str,
        version: str,
        request: dict,
        response: dict,
        *,
        declared_version: str | None = None,
    ) -> ValidationOutcome:
        with self._lock:
            contract = self._locate(contract_id, version)
            if contract.revoked:
                self._require_active(contract)
        # 调用方声明的版本与注册态不一致时，按版本不匹配语义区分处理
        if declared_version is not None and declared_version != str(contract.version):
            raise ContractError(
                ErrorCode.VERSION_MISMATCH,
                "调用声明的契约版本与目标版本不一致",
                {
                    "id": contract_id,
                    "declared": declared_version,
                    "target": str(contract.version),
                },
            )
        payload = {"request": request or {}, "response": response or {}}
        outcome = validate_payload(contract, payload, self._limits)
        logger.info(
            "运行时校验 id=%s version=%s valid=%s violations=%s",
            contract_id,
            version,
            outcome.valid,
            outcome.to_dict()["violations"],
        )
        return outcome

    # ------------------------------------------------------------------ #
    # 相邻版本兼容性
    # ------------------------------------------------------------------ #
    def compatibility(
        self, contract_id: str, from_version: str, to_version: str
    ) -> CompatReport:
        with self._lock:
            ordered = self._snapshot(contract_id)
            old = self._locate(contract_id, from_version)
            new = self._locate(contract_id, to_version)
            index = {str(c.version): i for i, c in enumerate(ordered)}
            if index[to_version] != index[from_version] + 1:
                raise ContractError(
                    ErrorCode.VERSION_NOT_ADJACENT,
                    "仅支持相邻注册版本之间的兼容性判定",
                    {
                        "id": contract_id,
                        "from": from_version,
                        "to": to_version,
                        "registered_order": [str(c.version) for c in ordered],
                    },
                )
            cache_key = (
                contract_id,
                from_version,
                to_version,
                old.revision,
                new.revision,
            )
            report = self._compat_cache.get(cache_key)
            if report is None:
                report = check_compatibility(old, new)
                self._compat_cache[cache_key] = report
        logger.info(
            "兼容性判定 id=%s %s->%s compatible=%s basis=%s",
            contract_id,
            from_version,
            to_version,
            report.compatible,
            list(report.basis),
        )
        return report


_registry: Registry | None = None
_registry_lock = threading.Lock()


def get_registry() -> Registry:
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = Registry()
    return _registry


def reset_registry(limits: LimitConfig | None = None) -> Registry:
    global _registry
    with _registry_lock:
        _registry = Registry(limits)
        return _registry
