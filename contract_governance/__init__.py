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
    PairedValidationResult,
    PublishDecision,
    Side,
    SideValidationResult,
    ValidationIssue,
    ValidationResult,
    Verdict,
)
from .service import GovernanceService
from .validation import MISSING

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
    "MISSING",
    "PairedValidationResult",
    "PublishDecision",
    "Reason",
    "Side",
    "SideValidationResult",
    "ValidationIssue",
    "ValidationResult",
    "Verdict",
]
