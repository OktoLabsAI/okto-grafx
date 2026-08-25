"""Error taxonomy for Okto Grafx (CONTRACT.md section 2, SPEC-M1 TR-6, SPEC-VEC TR-5).

Every failure that leaves the engine is a :class:`GrafxError`. Adapters translate the raw
platform failure they observe into one of the concrete types declared here, so no adapter
failure can kill the host process and every caller can branch on a stable ``code`` and on an
explicit ``retryable`` flag instead of on a message.
"""

from __future__ import annotations

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


class GrafxError(Exception):
    """Base class for every error raised by Okto Grafx. Never let an adapter kill the host process."""

    code: str = "grafx_error"
    retryable: bool = False

    def __init__(self, message: str, *, retryable: bool | None = None, **details: object) -> None:
        """Build the error, refusing a non-string message or a non-boolean retry flag.

        The refusal is a plain TypeError rather than a GrafxConfigurationError on purpose.
        Raising a Grafx error from inside an error constructor would replace the failure being
        reported with a failure about reporting it, and would recurse through this same
        constructor; a TypeError is the standard signal for a wrong argument type and it fires
        at the raise site, where the mistake is. Callers of the public surface never see it:
        the engine only ever builds these errors with literal strings.
        """
        if not isinstance(message, str):
            raise TypeError(
                f"A Grafx error message must be a string; got {type(message).__name__}."
            )
        if retryable is not None and not isinstance(retryable, bool):
            raise TypeError(
                f"A Grafx retry flag must be a bool or None; got {type(retryable).__name__}."
            )
        super().__init__(message)
        self.message: str = message
        self.details: dict[str, object] = dict(details)
        if retryable is not None:
            self.retryable = retryable

    def __str__(self) -> str:
        """Return the message followed by the machine-readable code and the retry flag."""
        return f"{self.message} [code={self.code} retryable={self.retryable}]"

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-friendly view of this error: type, code, message, retryable and details."""
        return {
            "type": type(self).__name__,
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": dict(self.details),
        }


class GrafxWriteConflict(GrafxError):
    """Optimistic validation refused a commit because the partition sets intersect. Retry the transaction."""

    code: str = "write_conflict"
    retryable: bool = True


class GrafxLeaseTimeout(GrafxError):
    """The writer lease could not be acquired before the timeout elapsed. Retry or back off."""

    code: str = "lease_timeout"
    retryable: bool = True


class GrafxLeaseStolen(GrafxError):
    """Another owner took the writer lease over; this holder must stop writing immediately."""

    code: str = "lease_stolen"
    retryable: bool = False


class GrafxStaleEpoch(GrafxError):
    """A holder of a previous epoch attempted a write; the operation is refused before any byte reaches the device."""

    code: str = "stale_epoch"
    retryable: bool = False


class GrafxCorruptionDetected(GrafxError):
    """Stored bytes failed their own integrity check; the reported location pins the damage."""

    code: str = "corruption_detected"
    retryable: bool = False


class GrafxDeviceFull(GrafxError):
    """The storage device refused to grow. Free space and retry."""

    code: str = "device_full"
    retryable: bool = True


class GrafxStorageError(GrafxError):
    """A device operation failed for a reason that is neither corruption nor a full device.

    This covers the transient-but-exhausted conditions of a real file system: a sharing
    violation from an antivirus scanner or a search indexer, EACCES, EBUSY, EAGAIN, EINTR,
    winerror 5, 32 and 33. The bytes on disk are not in question, which is why this is not
    corruption: corruption drives truncation, quarantine and forensic ledger entries, so
    reporting an access failure as corruption would manufacture an integrity incident that never
    happened.

    It is retryable by default, and the default is the load-bearing part. An adapter retry
    budget is measured in milliseconds -- five attempts backing off from five to forty -- while
    an antivirus scan holds a file for seconds. So "the adapter exhausted its retries" does not
    mean "retrying is futile"; it means a short local budget ran out while the condition is still
    transient. Answering retryable=False here would forbid the one action that would have
    worked: backing off and trying again at transaction scope.

    An instance may still override the flag for a condition that really is permanent, with
    GrafxStorageError(message, retryable=False) -- an unreachable path or a revoked permission
    will not improve on the next attempt.

    Details carry errno, winerror and attempts, so a caller can tell a sharing violation from a
    revoked permission and decide how long to back off. Recovery keys its quarantine decisions on
    that reason rather than on the class alone.
    """

    code: str = "storage_error"
    retryable: bool = True


class GrafxDurabilityBarrierFailed(GrafxError):
    """A durability barrier did not complete, so no acknowledgement may be given for the pending work."""

    code: str = "durability_barrier_failed"
    retryable: bool = False


class GrafxRecoveryRefused(GrafxError):
    """Recovery stopped on purpose because the configured policy is 'refuse'; nothing on disk was touched."""

    code: str = "recovery_refused"
    retryable: bool = False


class GrafxBufferBudgetExceeded(GrafxError):
    """This database exhausted its own page budget. Only its transactions are affected; retry after releasing pages."""

    code: str = "buffer_budget_exceeded"
    retryable: bool = True


class GrafxTransactionBudgetExceeded(GrafxError):
    """A statement or transaction exceeded one of its configured resource budgets."""

    code: str = "transaction_budget_exceeded"
    retryable: bool = False


class GrafxSchemaVersionMismatch(GrafxError):
    """The stored format version is not readable by this build of the engine."""

    code: str = "schema_version_mismatch"
    retryable: bool = False


class GrafxPortNotConfigured(GrafxError):
    """A required port slot is empty, so startup is refused. There is no silent default and no no-op fallback."""

    code: str = "port_not_configured"
    retryable: bool = False


class GrafxTransactionStateError(GrafxError):
    """The transaction is not in a state that allows the requested operation."""

    code: str = "transaction_state"
    retryable: bool = False


class GrafxLedgerError(GrafxError):
    """The unapplied-work ledger refused the operation, for example reprocessing a forensic entry."""

    code: str = "ledger_error"
    retryable: bool = False


class GrafxQuarantineError(GrafxError):
    """A quarantine operation failed; the quarantined bytes and their manifest are left untouched."""

    code: str = "quarantine_error"
    retryable: bool = False


class GrafxIndexError(GrafxError):
    """A secondary index refused the operation or diverged from the heap it indexes."""

    code: str = "index_error"
    retryable: bool = False


class GrafxQueryError(GrafxError):
    """A query could not be processed. Parse and plan failures are the specialised subclasses."""

    code: str = "query_error"
    retryable: bool = False


class GrafxQueryBudgetExceeded(GrafxQueryError):
    """A query exceeded one of its configured row-admission budgets."""

    code: str = "query_budget_exceeded"
    retryable: bool = False


class GrafxParseError(GrafxQueryError):
    """The query text is not valid for the supported Cypher subset."""

    code: str = "parse_error"
    retryable: bool = False


class GrafxPlanError(GrafxQueryError):
    """The statement parsed, but no valid plan exists for it against the current catalog."""

    code: str = "plan_error"
    retryable: bool = False


class GrafxVectorValidationError(GrafxError):
    """A vector write violated its space contract: wrong dimension or a non-finite component. Nothing is persisted."""

    code: str = "vector_validation"
    retryable: bool = False


class GrafxEmbeddingSpaceMismatch(GrafxError):
    """A similarity operation would compare vectors from different embedding spaces; it is refused before any distance is computed."""

    code: str = "embedding_space_mismatch"
    retryable: bool = False


class GrafxSpaceRetired(GrafxError):
    """The embedding space is retired and therefore read-only; reads still succeed and are flagged as retired."""

    code: str = "space_retired"
    retryable: bool = False


class GrafxConfigurationError(GrafxError):
    """A configuration value or a port binding is invalid; the offending field is named in the message."""

    code: str = "configuration_error"
    retryable: bool = False


class GrafxUnsupportedOperation(GrafxError):
    """The operation is deliberately not supported by this component or by this build."""

    code: str = "unsupported_operation"
    retryable: bool = False
