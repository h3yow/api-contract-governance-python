"""版本发布门禁：落库前先过相邻版本判定，破坏性变更需显式接受。

与直接 update 的区别：
- update 只做线性版本流转校验；publish 在此之上强制一道兼容性门禁。
- 判定为 breaking 且调用方未显式 accept_breaking 时，拒绝落库
  （breaking_change_rejected），该原因与跳号/回退/重复的
  illegal_version_transition 明确区分。
- 判定依据（CompatReport 内容）与是否显式接受随成功发布一起留档，
  之后可用 (contract_id, to_version) 重新查到。
- 真正写入仍走注册表的原子「校验 + 落库」，同一目标版本并发提交时
  恰有一个成功，不会覆盖丢失。
"""

from __future__ import annotations

from .compat import judge_compatibility
from .errors import GovernanceError, Reason
from .models import ContractSpec, ReleaseDecision, Verdict
from .registry import ContractRegistry


def evaluate_release(
    registry: ContractRegistry,
    contract_id: str,
    target_version: int,
    spec: ContractSpec,
) -> ReleaseDecision:
    """只做门禁判定、不落库：供 publish 与测试复用的纯评估。

    非法流转（跳号/回退/重复）抛 ILLEGAL_VERSION_TRANSITION；
    规格非法抛 INVALID_CONTRACT / LIMIT_EXCEEDED。
    """
    registry.validate_spec(spec)
    current = registry.get(contract_id)  # 未注册 -> CONTRACT_NOT_FOUND
    if target_version != current.version + 1:
        raise GovernanceError(
            Reason.ILLEGAL_VERSION_TRANSITION,
            f"illegal version transition: {current.version} -> {target_version}",
        )
    report = judge_compatibility(current, _proposed(contract_id, target_version, spec))
    breaking = report.verdict is Verdict.BREAKING
    return ReleaseDecision(
        contract_id=contract_id,
        from_version=current.version,
        to_version=target_version,
        verdict=report.verdict,
        changes=report.changes,
        accepted_breaking=False,
        allowed=not breaking,
        request_verdict=report.request_verdict,
        response_verdict=report.response_verdict,
    )


def _proposed(contract_id: str, version: int, spec: ContractSpec):
    from .models import Contract, ContractStatus

    return Contract(
        contract_id=contract_id,
        version=version,
        spec=spec,
        status=ContractStatus.ACTIVE,
    )


def apply_release(
    registry: ContractRegistry,
    contract_id: str,
    target_version: int,
    spec: ContractSpec,
    *,
    accept_breaking: bool,
    decision: ReleaseDecision,
) -> ReleaseDecision:
    """门禁通过后原子落库，并回写确认结果。

    落库冲突（并发下同一目标版本）时由注册表抛
    ILLEGAL_VERSION_TRANSITION，本函数不留任何档案。
    """
    final = ReleaseDecision(
        contract_id=decision.contract_id,
        from_version=decision.from_version,
        to_version=decision.to_version,
        verdict=decision.verdict,
        changes=decision.changes,
        accepted_breaking=accept_breaking and decision.verdict is Verdict.BREAKING,
        allowed=True,
        request_verdict=decision.request_verdict,
        response_verdict=decision.response_verdict,
    )
    # 原子落库：并发同一目标版本时只有一个能成功。
    registry.update(contract_id, target_version, spec)
    return final
