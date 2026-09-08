"""GX-CAP-1A: admission/identity semantics, not durable commit-catalog certification."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from math import inf, nan
import struct

import pytest

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxTransactionBudgetExceeded
from okto_grafx.domain.ids import PROVISIONAL_CSN
from okto_grafx.domain.model import Timestamp
from okto_grafx.domain.txn.commit_identity import CommitId, CommitTime, assign_commit_time
from okto_grafx.domain.txn.commit_metadata import CommitMetadata, MetadataLimits

STORE = bytes(range(16))


@pytest.mark.parametrize("sequence", [1, 17, PROVISIONAL_CSN - 1])
def test_qualified_commit_identity_round_trip(sequence: int) -> None:
    identity = CommitId(STORE, sequence)
    assert CommitId.parse(identity.to_token()) == identity
    assert len(identity.to_token()) == 49
    assert hash(CommitId.parse(identity.to_token())) == hash(identity)


@pytest.mark.parametrize("sequence", [True, False, 0, -1, PROVISIONAL_CSN, 1.0, "1", None])
def test_commit_identity_refuses_noncommitted_values(sequence: object) -> None:
    with pytest.raises(GrafxConfigurationError):
        CommitId(STORE, sequence)


@pytest.mark.parametrize("store", [b"", b"x" * 15, b"x" * 17, bytearray(16), "x" * 16, None])
def test_commit_identity_requires_exact_store_bytes(store: object) -> None:
    with pytest.raises(GrafxConfigurationError):
        CommitId(store, 1)


def test_order_is_store_local_not_uuid_lexicographic() -> None:
    first, last = CommitId(STORE, 1), CommitId(STORE, 91)
    assert first < last and first <= last and last > first and last >= first
    assert first <= first and first >= first
    foreign = CommitId(b"x" * 16, 1)
    assert first != foreign
    for operation in (lambda: first < foreign, lambda: first <= foreign,
                      lambda: first > foreign, lambda: first >= foreign):
        with pytest.raises(GrafxConfigurationError) as error:
            operation()
        assert error.value.details["field"] == "database_uuid"


@pytest.mark.parametrize("token", [None, 1, "", "A" * 32 + ":0000000000000001",
                                  "0" * 32 + ":1", "0" * 32 + ":0000000000000000",
                                  "0" * 32 + ":ffffffffffffffff", "g" * 49,
                                  "0" * 32 + "-0000000000000001"])
def test_token_requires_canonical_exact_spelling(token: object) -> None:
    with pytest.raises(GrafxConfigurationError):
        CommitId.parse(token)


@pytest.mark.parametrize("observed,previous,ordered,adjusted", [
    (10, None, 10, False), (11, 10, 11, False), (10, 10, 11, True),
    (-100, 10, 11, True), (-99, -100, -99, False), (-100, -100, -99, True),
    ((1 << 63) - 1, None, (1 << 63) - 1, False),
])
def test_logical_time_is_monotone_not_wall_order(
    observed: int, previous: int | None, ordered: int, adjusted: bool,
) -> None:
    result = assign_commit_time(Timestamp(observed), None if previous is None else Timestamp(previous))
    assert result.observed_at.micros == observed
    assert result.ordered_at.micros == ordered
    assert result.clock_adjusted is adjusted
    with pytest.raises(FrozenInstanceError):
        result.clock_adjusted = False


def test_clock_overflow_never_wraps() -> None:
    with pytest.raises(GrafxConfigurationError) as error:
        assign_commit_time(Timestamp(0), Timestamp((1 << 63) - 1))
    assert error.value.details["field"] == "ordered_at"


@pytest.mark.parametrize("ordered,adjusted", [(0, False), (1, True), (2, False), (1, 0)])
def test_commit_time_cannot_claim_inconsistent_coordinates(ordered: int, adjusted: object) -> None:
    with pytest.raises(GrafxConfigurationError):
        CommitTime(Timestamp(1), Timestamp(ordered), adjusted)


@pytest.mark.parametrize("observed,previous", [(1, None), (None, None), (Timestamp(1), 0)])
def test_clock_requires_typed_instants(observed: object, previous: object) -> None:
    with pytest.raises(GrafxConfigurationError):
        assign_commit_time(observed, previous)


def test_metadata_snapshots_nested_input_and_never_exposes_mutable_children() -> None:
    source = {"board": "42", "values": [1, {"x": True}], "empty": None}
    metadata = CommitMetadata(actor="pulse:indexer", attributes=source)
    encoded = metadata.canonical_bytes
    source["values"][1]["x"] = False
    source["values"].append(99)
    source["board"] = "changed"
    assert metadata.attributes["values"] == (1, {"x": True})
    assert metadata.attributes["board"] == "42"
    assert metadata.canonical_bytes == encoded
    with pytest.raises(TypeError):
        metadata.attributes["board"] = "changed"
    with pytest.raises(TypeError):
        metadata.attributes["values"][1]["x"] = False
    with pytest.raises(FrozenInstanceError):
        metadata.actor = "changed"


def test_canonical_bytes_ignore_mapping_order_and_normalize_lists_to_tuples() -> None:
    first = CommitMetadata(attributes={"é": [1, 2.0, None], "a": {"z": True, "b": "x"}})
    second = CommitMetadata(attributes={"a": {"b": "x", "z": True}, "é": (1, 2.0, None)})
    assert first == second and hash(first) == hash(second)
    assert first.canonical_bytes == second.canonical_bytes
    assert tuple(first.attributes) == ("a", "é")
    assert CommitMetadata(attributes={"n": True}) != CommitMetadata(attributes={"n": 1})
    assert CommitMetadata(attributes={"n": -0.0}) != CommitMetadata(attributes={"n": 0.0})
    assert CommitMetadata(actor="") != CommitMetadata()


def test_canonical_admission_bytes_are_typed_and_length_delimited() -> None:
    def text(value: str) -> bytes:
        raw = value.encode("utf-8")
        return b"s" + len(raw).to_bytes(4, "little") + raw

    minimum, maximum = -(1 << 63), (1 << 63) - 1
    metadata = CommitMetadata(actor="a\0b", attributes={
        "array": [None, False, True, minimum, maximum, -0.0, "é"],
        "map": {"": ""},
    })
    expected = (
        b"GXCM\x01" + text("a\0b") + b"nnn" + b"m\x02\0\0\0"
        + text("array") + b"a\x07\0\0\0nft"
        + b"i" + struct.pack("<q", minimum) + b"i" + struct.pack("<q", maximum)
        + b"d" + struct.pack("<d", -0.0) + text("é")
        + text("map") + b"m\x01\0\0\0" + text("") + text("")
    )
    assert metadata.canonical_bytes == expected
    # No normalization merges distinct source strings or tagged value types.
    candidates = [None, False, True, 0, 1, 0.0, -0.0, "", "é", "e\u0301", [], {}]
    encoded = {CommitMetadata(attributes={"x": item}).canonical_bytes for item in candidates}
    assert len(encoded) == len(candidates)


@pytest.mark.parametrize("attributes", [{"\ud800": "x"}, {"x": "\udfff"}])
def test_attribute_surrogates_are_refused_without_content_in_diagnostics(attributes: dict) -> None:
    with pytest.raises(GrafxConfigurationError) as error:
        CommitMetadata(attributes=attributes)
    assert error.value.details == {"field": "attributes", "reason": "invalid_utf8"}


def test_full_depth_limit_remains_bounded_for_a_deep_host_tree() -> None:
    value = 1
    for _ in range(100):
        value = [value]
    with pytest.raises(GrafxTransactionBudgetExceeded) as error:
        CommitMetadata(attributes={"x": value}, limits=MetadataLimits(max_depth=16))
    assert error.value.details["budget"] == "commit_metadata_depth"


def test_large_sequence_refuses_before_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    from okto_grafx.domain.txn import commit_metadata

    class ForbiddenTuple(tuple):
        def __new__(cls, *args, **kwargs):
            raise AssertionError("reached copying")

    # Keep the runtime cast/discriminator type-shaped, and prove the sentinel is
    # reached by a small list before asserting the oversized list never reaches it.
    monkeypatch.setattr(commit_metadata, "tuple", ForbiddenTuple, raising=False)
    with pytest.raises(AssertionError, match="reached copying"):
        CommitMetadata(attributes={"x": [1]})
    with pytest.raises(GrafxTransactionBudgetExceeded) as error:
        CommitMetadata(attributes={"x": list(range(1000))})
    assert error.value.details["budget"] == "commit_metadata_values"


@pytest.mark.parametrize("value", [nan, inf, -inf, 1 << 63, -(1 << 63) - 1,
                                  {1: "bad"}, b"bytes", bytearray(b"x"), {1, 2}, object()])
def test_metadata_rejects_noncanonical_values(value: object) -> None:
    with pytest.raises(GrafxConfigurationError):
        CommitMetadata(attributes={"secret-key": value})


def test_cycles_refuse_but_shared_acyclic_values_are_legal() -> None:
    cycle = []
    cycle.append(cycle)
    with pytest.raises(GrafxConfigurationError):
        CommitMetadata(attributes={"cycle": cycle})
    child = [1, 2]
    metadata = CommitMetadata(attributes={"a": child, "b": child})
    assert metadata.attributes["a"] == metadata.attributes["b"] == (1, 2)


@pytest.mark.parametrize("field", ["actor", "origin", "correlation_id", "reason"])
def test_metadata_fields_validate_and_do_not_leak(field: str) -> None:
    with pytest.raises(GrafxConfigurationError) as error:
        CommitMetadata(**{field: "DO-NOT-LOG\ud800"})
    assert "DO-NOT-LOG" not in str(error.value.to_dict())
    with pytest.raises(GrafxConfigurationError):
        CommitMetadata(**{field: 1})
    assert "DO-NOT-LOG" not in repr(CommitMetadata(**{field: "DO-NOT-LOG"}))


def test_metadata_repr_and_budget_errors_are_payload_free() -> None:
    assert "DO-NOT-LOG" not in repr(CommitMetadata(attributes={"DO-NOT-LOG": "value"}))
    with pytest.raises(GrafxTransactionBudgetExceeded) as error:
        CommitMetadata(attributes={"DO-NOT-LOG": "a" * 5}, limits=MetadataLimits(max_string_bytes=4))
    assert "DO-NOT-LOG" not in str(error.value.to_dict())


def test_exact_encoded_budget_and_utf8_budget_not_character_budget() -> None:
    candidate = CommitMetadata(actor="é", attributes={"🙂": "á"})
    exact = len(candidate.canonical_bytes)
    assert CommitMetadata(actor="é", attributes={"🙂": "á"},
                          limits=MetadataLimits(max_bytes=exact)) == candidate
    with pytest.raises(GrafxTransactionBudgetExceeded):
        CommitMetadata(actor="é", attributes={"🙂": "á"}, limits=MetadataLimits(max_bytes=exact - 1))
    with pytest.raises(GrafxTransactionBudgetExceeded):
        CommitMetadata(attributes={"🙂": "á"}, limits=MetadataLimits(max_key_bytes=3))
    with pytest.raises(GrafxTransactionBudgetExceeded):
        CommitMetadata(actor="é", limits=MetadataLimits(max_string_bytes=1))
    assert len(CommitMetadata(limits=MetadataLimits(max_bytes=14)).canonical_bytes) == 14


@pytest.mark.parametrize("limits,attributes", [
    (MetadataLimits(max_attributes=0), {"x": 1}),
    (MetadataLimits(max_attributes=1), {"x": {"a": 1, "b": 2}}),
    (MetadataLimits(max_depth=0), {"x": []}),
    (MetadataLimits(max_values=2), {"x": [1]}),
])
def test_container_budgets_refuse(limits: MetadataLimits, attributes: dict) -> None:
    with pytest.raises(GrafxTransactionBudgetExceeded):
        CommitMetadata(attributes=attributes, limits=limits)


def test_oversized_keys_refuse_before_sorting(monkeypatch: pytest.MonkeyPatch) -> None:
    from okto_grafx.domain.txn import commit_metadata

    def forbidden_sort(*args, **kwargs):
        raise AssertionError("unbounded keys reached sorting")

    monkeypatch.setattr(commit_metadata, "sorted", forbidden_sort, raising=False)
    with pytest.raises(GrafxTransactionBudgetExceeded):
        CommitMetadata(attributes={"x" * 10_000: 1, "x" * 10_000 + "y": 2})


def test_mutated_limits_cannot_bypass_hard_ceiling() -> None:
    limits = MetadataLimits()
    object.__setattr__(limits, "max_bytes", 1 << 60)
    with pytest.raises(GrafxConfigurationError):
        CommitMetadata(limits=limits)


def test_exact_container_budgets_allow_scalar_children() -> None:
    assert CommitMetadata(attributes={"x": 1}, limits=MetadataLimits(max_depth=0, max_values=2))
    assert CommitMetadata(attributes={"x": [1]}, limits=MetadataLimits(max_depth=1, max_values=3))


@pytest.mark.parametrize("field,value", [("max_bytes", 13), ("max_bytes", 65_537),
    ("max_attributes", -1), ("max_attributes", 257), ("max_key_bytes", 0),
    ("max_key_bytes", 1025), ("max_string_bytes", 16_385), ("max_depth", 17),
    ("max_values", 0), ("max_values", 4097), ("max_bytes", True), ("max_depth", 1.0)])
def test_limits_are_bounded_exact_integers(field: str, value: object) -> None:
    with pytest.raises(GrafxConfigurationError):
        replace(MetadataLimits(), **{field: value})


def test_unsupported_subclasses_do_not_run_host_hooks() -> None:
    class HostileDict(dict):
        def items(self):
            raise AssertionError("must not execute host iteration")

        def __repr__(self):
            raise AssertionError("must not print host input")

    class HostileInt(int):
        def __int__(self):
            raise AssertionError("must not coerce host input")

        def __repr__(self):
            raise AssertionError("must not print host input")

    class HostileList(list):
        def __iter__(self):
            raise AssertionError("must not run host iteration")

    for value in (HostileDict(), HostileInt(1), HostileList([1])):
        with pytest.raises(GrafxConfigurationError):
            CommitMetadata(attributes={"x": value})
    with pytest.raises(GrafxConfigurationError):
        CommitMetadata(attributes=HostileDict())
    with pytest.raises(GrafxConfigurationError):
        CommitId(STORE, HostileInt(1))


def test_admission_slice_is_not_an_inert_public_capability() -> None:
    import okto_grafx

    assert not hasattr(okto_grafx, "CommitMetadata")
    assert not hasattr(okto_grafx, "CommitId")
