"""Public error surface of Okto Grafx.

This module is the supported import path for callers: ``from okto_grafx.errors import
GrafxWriteConflict``. It re-exports the domain taxonomy unchanged, so the pure core keeps
owning the definitions while applications never have to import from ``okto_grafx.domain``.
"""

from __future__ import annotations

from okto_grafx.domain.errors import (
    GrafxBufferBudgetExceeded,
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxDeviceFull,
    GrafxDurabilityBarrierFailed,
    GrafxEmbeddingSpaceMismatch,
    GrafxError,
    GrafxIndexError,
    GrafxLeaseStolen,
    GrafxLeaseTimeout,
    GrafxLedgerError,
    GrafxParseError,
    GrafxPlanError,
    GrafxPortNotConfigured,
    GrafxQuarantineError,
    GrafxQueryBudgetExceeded,
    GrafxQueryError,
    GrafxRecoveryRefused,
    GrafxSchemaVersionMismatch,
    GrafxSpaceRetired,
    GrafxStaleEpoch,
    GrafxStorageError,
    GrafxTransactionBudgetExceeded,
    GrafxTransactionStateError,
    GrafxUnsupportedOperation,
    GrafxVectorValidationError,
    GrafxWriteConflict,
)

__all__ = [
    "GrafxError",
    "GrafxWriteConflict",
    "GrafxLeaseTimeout",
    "GrafxLeaseStolen",
    "GrafxStaleEpoch",
    "GrafxCorruptionDetected",
    "GrafxDeviceFull",
    "GrafxStorageError",
    "GrafxDurabilityBarrierFailed",
    "GrafxRecoveryRefused",
    "GrafxBufferBudgetExceeded",
    "GrafxTransactionBudgetExceeded",
    "GrafxSchemaVersionMismatch",
    "GrafxPortNotConfigured",
    "GrafxTransactionStateError",
    "GrafxLedgerError",
    "GrafxQuarantineError",
    "GrafxIndexError",
    "GrafxQueryError",
    "GrafxQueryBudgetExceeded",
    "GrafxParseError",
    "GrafxPlanError",
    "GrafxVectorValidationError",
    "GrafxEmbeddingSpaceMismatch",
    "GrafxSpaceRetired",
    "GrafxConfigurationError",
    "GrafxUnsupportedOperation",
]
