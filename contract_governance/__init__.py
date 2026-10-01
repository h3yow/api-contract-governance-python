"""API 契约注册与版本兼容治理。"""

from .config import GovernanceLimits
from .errors import GovernanceError, Reason
from .models import (
    Change,
    ChangeKind,
    CompatReport,
    Contract,
    ContractSpec,
    ContractStatus,
    FieldSpec,
    FieldType,
    PairValidationResult,
    ReleaseDecision,
    Side,
    SideCompatReport,
    SideValidationResult,
    ValidationIssue,
    ValidationResult,
    Verdict,
)
from .service import GovernanceService

__all__ = [
    "Change",
    "ChangeKind",
    "CompatReport",
    "Contract",
    "ContractSpec",
    "ContractStatus",
    "FieldSpec",
    "FieldType",
    "GovernanceError",
    "GovernanceLimits",
    "GovernanceService",
    "PairValidationResult",
    "Reason",
    "ReleaseDecision",
    "Side",
    "SideCompatReport",
    "SideValidationResult",
    "ValidationIssue",
    "ValidationResult",
    "Verdict",
]
