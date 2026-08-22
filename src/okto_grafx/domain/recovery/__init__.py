"""Recovery: the decision, the report, and the manifest that keeps the evidence readable.

CONTRACT.md section 8.6 freezes the algorithm and the report; SPEC-M1 FR-8 and FR-10 state what
recovery and quarantine owe an operator. Nothing here touches a device -- the state machine
decides from a scan, and the engine acts on the decision.
"""

from __future__ import annotations

from okto_grafx.domain.recovery.decision import (
    DiscardedRange,
    DiscardedRecord,
    RecoveryPlan,
    plan_recovery,
    redo_order,
)
from okto_grafx.domain.recovery.manifest import (
    MANIFEST_FILE_NAME,
    MANIFEST_SCHEMA_VERSION,
    MAX_NAME_CHARACTERS,
    RESTORE_RECEIPT_PREFIX,
    STAMP_DIGITS,
    QuarantineManifest,
    RestoreReceipt,
    entry_suffix,
    sanitize_name,
    stamp_of,
)
from okto_grafx.domain.recovery.report import (
    OUTCOME_CLEAN,
    OUTCOME_QUARANTINED,
    OUTCOME_REFUSED,
    OUTCOME_TRUNCATED,
    POLICY_REFUSE,
    POLICY_REPLAY,
    RECOVERY_OUTCOMES,
    RECOVERY_POLICIES,
    FindingKind,
    RecoveryFinding,
    RecoveryReport,
    stronger_outcome,
)
from okto_grafx.domain.recovery.retry import RETRYABLE_KEY, is_retryable

__all__ = [
    "MANIFEST_FILE_NAME",
    "MANIFEST_SCHEMA_VERSION",
    "MAX_NAME_CHARACTERS",
    "OUTCOME_CLEAN",
    "OUTCOME_QUARANTINED",
    "OUTCOME_REFUSED",
    "OUTCOME_TRUNCATED",
    "POLICY_REFUSE",
    "POLICY_REPLAY",
    "RECOVERY_OUTCOMES",
    "RECOVERY_POLICIES",
    "RESTORE_RECEIPT_PREFIX",
    "RETRYABLE_KEY",
    "STAMP_DIGITS",
    "DiscardedRange",
    "DiscardedRecord",
    "FindingKind",
    "QuarantineManifest",
    "RecoveryFinding",
    "RecoveryPlan",
    "RecoveryReport",
    "RestoreReceipt",
    "entry_suffix",
    "is_retryable",
    "plan_recovery",
    "redo_order",
    "sanitize_name",
    "stamp_of",
    "stronger_outcome",
]
