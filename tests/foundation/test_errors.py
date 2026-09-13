"""The error taxonomy against the frozen table in CONTRACT.md section 2 (SPEC-M1 TR-6)."""

from __future__ import annotations

import pytest

from okto_grafx import errors as public_errors
from okto_grafx.domain import errors as domain_errors
from okto_grafx.domain.errors import (
    GrafxCorruptionDetected,
    GrafxError,
    GrafxParseError,
    GrafxPlanError,
    GrafxQueryError,
    GrafxStorageError,
    GrafxWriteConflict,
)

# CONTRACT.md section 2, transcribed as data: class name, code, retryable.
CONTRACT_TABLE: tuple[tuple[str, str, bool], ...] = (
    ("GrafxHistoryUnavailable", "history_unavailable", False),
    ("GrafxHistoryExpired", "history_expired", False),
    ("GrafxWriteConflict", "write_conflict", True),
    ("GrafxLeaseTimeout", "lease_timeout", True),
    ("GrafxLeaseStolen", "lease_stolen", False),
    ("GrafxStaleEpoch", "stale_epoch", False),
    ("GrafxCorruptionDetected", "corruption_detected", False),
    ("GrafxDeviceFull", "device_full", True),
    ("GrafxStorageError", "storage_error", True),
    ("GrafxDurabilityBarrierFailed", "durability_barrier_failed", False),
    ("GrafxRecoveryRefused", "recovery_refused", False),
    ("GrafxSnapshotReclaimed", "snapshot_reclaimed", True),
    ("GrafxBufferBudgetExceeded", "buffer_budget_exceeded", True),
    ("GrafxTransactionBudgetExceeded", "transaction_budget_exceeded", False),
    ("GrafxSchemaVersionMismatch", "schema_version_mismatch", False),
    ("GrafxPortNotConfigured", "port_not_configured", False),
    ("GrafxTransactionStateError", "transaction_state", False),
    ("GrafxLedgerError", "ledger_error", False),
    ("GrafxQuarantineError", "quarantine_error", False),
    ("GrafxIndexError", "index_error", False),
    ("GrafxQueryError", "query_error", False),
    ("GrafxQueryBudgetExceeded", "query_budget_exceeded", False),
    ("GrafxQueryCancelled", "query_cancelled", False),
    ("GrafxQueryDeadlineExceeded", "query_deadline_exceeded", False),
    ("GrafxParseError", "parse_error", False),
    ("GrafxPlanError", "plan_error", False),
    ("GrafxVectorValidationError", "vector_validation", False),
    ("GrafxEmbeddingSpaceMismatch", "embedding_space_mismatch", False),
    ("GrafxSpaceRetired", "space_retired", False),
    ("GrafxConfigurationError", "configuration_error", False),
    ("GrafxUnsupportedOperation", "unsupported_operation", False),
)


@pytest.mark.parametrize(("class_name", "code", "retryable"), CONTRACT_TABLE)
def test_class_matches_the_contract_row(class_name: str, code: str, retryable: bool) -> None:
    error_type = getattr(domain_errors, class_name)
    assert issubclass(error_type, GrafxError)
    assert error_type.code == code
    assert error_type.retryable is retryable
    instance = error_type("A failure happened.")
    assert instance.code == code
    assert instance.retryable is retryable


def test_the_taxonomy_has_no_member_outside_the_contract() -> None:
    exported = set(domain_errors.__all__) - {"GrafxError"}
    assert exported == {row[0] for row in CONTRACT_TABLE}


def test_base_class_defaults() -> None:
    assert GrafxError.code == "grafx_error"
    assert GrafxError.retryable is False


def test_every_code_is_unique() -> None:
    codes = [row[1] for row in CONTRACT_TABLE]
    assert len(codes) == len(set(codes))


def test_query_errors_are_specialisations() -> None:
    assert issubclass(GrafxParseError, GrafxQueryError)
    assert issubclass(GrafxPlanError, GrafxQueryError)
    assert not issubclass(GrafxQueryError, GrafxParseError)


def test_str_carries_code_and_retry_flag() -> None:
    error = GrafxWriteConflict("Partition sets intersect.")
    assert str(error) == "Partition sets intersect. [code=write_conflict retryable=True]"


def test_message_and_details_are_preserved() -> None:
    error = GrafxError("Something broke.", partition=7, file="wal/000000000001.wal")
    assert error.message == "Something broke."
    assert error.details == {"partition": 7, "file": "wal/000000000001.wal"}
    assert error.args == ("Something broke.",)


def test_the_details_mapping_is_copied_and_nested_values_are_shared() -> None:
    # Contract section 2 prescribes dict(details) verbatim: the copy is shallow. State the real
    # guarantee so nobody later reads more safety into it than there is.
    items = [1, 2, 3]
    payload = {"partition": 7, "items": items}
    error = GrafxError("Something broke.", **payload)

    # The top-level mapping is a copy: rebinding or adding a key outside cannot reach in.
    payload["partition"] = 9
    payload["extra"] = "ignored"
    assert error.details == {"partition": 7, "items": [1, 2, 3]}
    error.to_dict()["details"]["partition"] = 11
    assert error.details["partition"] == 7

    # Nested values are shared, by design and by the contract. Callers that need isolation copy
    # what they hand over.
    items.append(4)
    assert error.details["items"] == [1, 2, 3, 4]
    assert error.to_dict()["details"]["items"] is items


def test_a_non_string_message_is_refused() -> None:
    # Building an error is a programming act; a Grafx error raised here would replace the
    # failure being reported and would recurse through this same constructor.
    with pytest.raises(TypeError) as raised:
        GrafxError(7)  # type: ignore[arg-type]
    assert "message must be a string" in str(raised.value)
    with pytest.raises(TypeError):
        GrafxWriteConflict(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        GrafxError(b"bytes are not a message.")  # type: ignore[arg-type]


@pytest.mark.parametrize("retryable", [1, 0, "yes", "", [], 1.0])
def test_a_non_boolean_retry_flag_is_refused(retryable: object) -> None:
    with pytest.raises(TypeError) as raised:
        GrafxError("Something broke.", retryable=retryable)  # type: ignore[arg-type]
    assert "retry flag must be a bool" in str(raised.value)


def test_the_retry_flag_still_accepts_none_and_both_booleans() -> None:
    assert GrafxError("Something broke.", retryable=None).retryable is False
    assert GrafxError("Something broke.", retryable=True).retryable is True
    assert GrafxWriteConflict("Retry me.", retryable=False).retryable is False


def test_a_refused_construction_leaves_no_half_built_error() -> None:
    with pytest.raises(TypeError):
        GrafxError("Something broke.", retryable="yes")  # type: ignore[arg-type]
    # The class attribute is untouched: the refusal happens before any assignment.
    assert GrafxError.retryable is False
    assert GrafxError("Something broke.").retryable is False


def test_retryable_override_is_per_instance() -> None:
    error = GrafxError("Transient.", retryable=True)
    assert error.retryable is True
    assert GrafxError.retryable is False
    assert GrafxError("Permanent.").retryable is False


def test_retryable_override_can_force_false_on_a_retryable_class() -> None:
    error = GrafxWriteConflict("Do not retry this one.", retryable=False)
    assert error.retryable is False
    assert GrafxWriteConflict.retryable is True


def test_to_dict_round_trips_through_the_constructor() -> None:
    original = GrafxWriteConflict(
        "Partition sets intersect.", partitions=[3, 9], attempt=2
    )
    payload = original.to_dict()
    assert payload == {
        "type": "GrafxWriteConflict",
        "code": "write_conflict",
        "message": "Partition sets intersect.",
        "retryable": True,
        "details": {"partitions": [3, 9], "attempt": 2},
    }
    rebuilt = GrafxWriteConflict(
        str(payload["message"]),
        retryable=bool(payload["retryable"]),
        **dict(payload["details"]),  # type: ignore[arg-type]
    )
    assert rebuilt.to_dict() == payload


def test_to_dict_round_trips_a_non_default_retry_flag() -> None:
    original = GrafxWriteConflict("Give up.", retryable=False)
    payload = original.to_dict()
    rebuilt = GrafxWriteConflict(
        str(payload["message"]), retryable=bool(payload["retryable"])
    )
    assert rebuilt.to_dict() == payload
    assert rebuilt.retryable is False


def test_every_error_is_catchable_as_the_base_type() -> None:
    for class_name, _, _ in CONTRACT_TABLE:
        error_type = getattr(domain_errors, class_name)
        with pytest.raises(GrafxError):
            raise error_type("Raised on purpose.")


def test_public_alias_exports_the_same_objects() -> None:
    assert set(public_errors.__all__) == set(domain_errors.__all__)
    for name in domain_errors.__all__:
        assert getattr(public_errors, name) is getattr(domain_errors, name)


# --- the storage error and its retry semantics (amendment A11-revised) ------------------------


def test_the_storage_error_is_retryable_by_default() -> None:
    # The default is the load-bearing part: an adapter retry budget is milliseconds while an
    # antivirus scan holds a file for seconds, so an exhausted budget is not a futile retry.
    assert GrafxStorageError.retryable is True
    assert GrafxStorageError("The device failed a read.").retryable is True
    assert GrafxStorageError("The device failed a read.").code == "storage_error"


def test_the_storage_error_may_be_overridden_for_a_permanent_condition() -> None:
    permanent = GrafxStorageError("The path no longer exists.", retryable=False)
    assert permanent.retryable is False
    assert GrafxStorageError.retryable is True
    assert permanent.to_dict()["retryable"] is False


def test_the_storage_error_carries_the_documented_detail_keys() -> None:
    # C2 populates these and C6 keys quarantine decisions on the reason rather than the class.
    error = GrafxStorageError(
        "The device failed read_page on 'heap.dat': sharing violation.",
        errno=13,
        winerror=32,
        attempts=5,
        reason="access_failed",
    )
    assert error.details["errno"] == 13
    assert error.details["winerror"] == 32
    assert error.details["attempts"] == 5
    assert error.to_dict()["details"] == dict(error.details)


@pytest.mark.parametrize("key", ["errno", "winerror", "attempts"])
def test_the_storage_error_documents_the_detail_keys_it_carries(key: str) -> None:
    documentation = GrafxStorageError.__doc__ or ""
    assert key in documentation, f"{key} is carried in details but not documented"


def test_the_storage_error_is_not_a_corruption_report() -> None:
    # A11-revised: corruption is reserved for damaged bytes, because FR-8 and FR-10 turn it into
    # truncation, quarantine and a forensic ledger entry.
    assert not issubclass(GrafxStorageError, GrafxCorruptionDetected)
    assert not issubclass(GrafxCorruptionDetected, GrafxStorageError)
    assert GrafxCorruptionDetected.retryable is False
    assert GrafxStorageError.code != GrafxCorruptionDetected.code


def test_the_retryable_classes_are_exactly_the_ones_the_contract_marks() -> None:
    retryable = {name for name, _, flag in CONTRACT_TABLE if flag}
    assert retryable == {
        "GrafxWriteConflict",
        "GrafxLeaseTimeout",
        "GrafxDeviceFull",
        "GrafxStorageError",
        "GrafxBufferBudgetExceeded",
        "GrafxSnapshotReclaimed",
    }
    for name in retryable:
        assert getattr(domain_errors, name).retryable is True
