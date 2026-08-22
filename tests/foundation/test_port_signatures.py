"""The FROZEN port signatures of CONTRACT.md section 4, transcribed and compared (DoD item 1).

Section 4 is the widest contract in the project: every other component reads its ports here, and
definition-of-done item 1 is "no signature drift". Until now the suite pinned member *names*
only, so ``create(..., exclusive=True)`` could become ``exclusive=False``, ``write_page`` could
rename ``page_index`` to ``index``, and ``acquire_writer_lease(*, timeout)`` could quietly stop
being keyword-only -- three changes that compile, pass every other test, and break a caller in
another component at wave time.

The expectation below is transcribed from the contract by hand, parameter by parameter: name,
kind and default. It is deliberately verbose rather than derived, because a table generated from
the code under test would agree with the code under test whatever the contract says (A68).
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from okto_grafx.domain.ports import (
    Clock,
    EventSink,
    MetricsSink,
    PageCodec,
    ProcessCoordinator,
    StorageDevice,
    VectorMath,
)

EMPTY = inspect.Parameter.empty
POSITIONAL = inspect.Parameter.POSITIONAL_OR_KEYWORD
KEYWORD_ONLY = inspect.Parameter.KEYWORD_ONLY

# (parameter name, kind, default) after self, transcribed from CONTRACT.md section 4.
Signature = tuple[tuple[str, Any, Any], ...]

STORAGE_DEVICE: dict[str, Signature] = {
    "name": (),
    "page_size": (),
    "exists": (("file", POSITIONAL, EMPTY),),
    "create": (("file", POSITIONAL, EMPTY), ("exclusive", KEYWORD_ONLY, True)),
    "remove": (("file", POSITIONAL, EMPTY),),
    "list_files": (("prefix", POSITIONAL, ""),),
    "file_size": (("file", POSITIONAL, EMPTY),),
    "atomic_replace": (("source", POSITIONAL, EMPTY), ("target", POSITIONAL, EMPTY)),
    "recycle": (("file", POSITIONAL, EMPTY),),
    "page_count": (("file", POSITIONAL, EMPTY),),
    "allocate": (("file", POSITIONAL, EMPTY), ("count", POSITIONAL, 1)),
    "read_page": (("file", POSITIONAL, EMPTY), ("page_index", POSITIONAL, EMPTY)),
    "write_page": (
        ("file", POSITIONAL, EMPTY),
        ("page_index", POSITIONAL, EMPTY),
        ("data", POSITIONAL, EMPTY),
    ),
    "append_log": (("file", POSITIONAL, EMPTY), ("payload", POSITIONAL, EMPTY)),
    "read_log": (
        ("file", POSITIONAL, EMPTY),
        ("offset", POSITIONAL, EMPTY),
        ("length", POSITIONAL, EMPTY),
    ),
    "log_size": (("file", POSITIONAL, EMPTY),),
    "truncate_log": (("file", POSITIONAL, EMPTY), ("size", POSITIONAL, EMPTY)),
    "durable_barrier": (("file", POSITIONAL, None),),
}

CLOCK: dict[str, Signature] = {"monotonic": (), "wall": ()}

PROCESS_COORDINATOR: dict[str, Signature] = {
    "owner_id": (),
    "current_epoch": (),
    "acquire_writer_lease": (("timeout", KEYWORD_ONLY, EMPTY),),
    "renew_lease": (("lease", POSITIONAL, EMPTY),),
    "release_lease": (("lease", POSITIONAL, EMPTY),),
    "validate_epoch": (("epoch", POSITIONAL, EMPTY),),
    "detect_dead_owner": (("stall_threshold", KEYWORD_ONLY, EMPTY),),
    "takeover": (),
    "register_reader": (("snapshot_lsn", POSITIONAL, EMPTY),),
    "refresh_reader": (("handle", POSITIONAL, EMPTY),),
    "unregister_reader": (("handle", POSITIONAL, EMPTY),),
    "reader_horizon": (),
    "exclusive": (("name", POSITIONAL, EMPTY), ("timeout", KEYWORD_ONLY, EMPTY)),
}

PAGE_CODEC: dict[str, Signature] = {
    "format_version": (),
    "checksum": (("payload", POSITIONAL, EMPTY),),
    "encode_page": (("page", POSITIONAL, EMPTY),),
    "decode_page": (("raw", POSITIONAL, EMPTY), ("verify", KEYWORD_ONLY, True)),
}

METRICS_SINK: dict[str, Signature] = {
    "enabled": (),
    "register": (("descriptor", POSITIONAL, EMPTY),),
    "increment": (
        ("name", POSITIONAL, EMPTY),
        ("value", POSITIONAL, 1.0),
        ("labels", POSITIONAL, None),
    ),
    "set_gauge": (
        ("name", POSITIONAL, EMPTY),
        ("value", POSITIONAL, EMPTY),
        ("labels", POSITIONAL, None),
    ),
    "observe": (
        ("name", POSITIONAL, EMPTY),
        ("value", POSITIONAL, EMPTY),
        ("labels", POSITIONAL, None),
    ),
    "time": (("name", POSITIONAL, EMPTY), ("labels", POSITIONAL, None)),
    "snapshot": (),
}

VECTOR_MATH: dict[str, Signature] = {
    "name": (),
    "dot": (("a", POSITIONAL, EMPTY), ("b", POSITIONAL, EMPTY)),
    "cosine": (("a", POSITIONAL, EMPTY), ("b", POSITIONAL, EMPTY)),
    "euclidean": (("a", POSITIONAL, EMPTY), ("b", POSITIONAL, EMPTY)),
    "norm": (("a", POSITIONAL, EMPTY),),
    "normalize": (("a", POSITIONAL, EMPTY),),
    "score": (
        ("a", POSITIONAL, EMPTY),
        ("b", POSITIONAL, EMPTY),
        ("metric", POSITIONAL, EMPTY),
    ),
    "top_k": (
        ("query", POSITIONAL, EMPTY),
        ("candidates", POSITIONAL, EMPTY),
        ("k", POSITIONAL, EMPTY),
        ("metric", POSITIONAL, EMPTY),
    ),
}

EVENT_SINK: dict[str, Signature] = {
    "emit": (("event", POSITIONAL, EMPTY), ("payload", POSITIONAL, EMPTY))
}

FROZEN: tuple[tuple[type, dict[str, Signature]], ...] = (
    (StorageDevice, STORAGE_DEVICE),
    (Clock, CLOCK),
    (ProcessCoordinator, PROCESS_COORDINATOR),
    (PageCodec, PAGE_CODEC),
    (MetricsSink, METRICS_SINK),
    (VectorMath, VECTOR_MATH),
    (EventSink, EVENT_SINK),
)

CASES: list[tuple[type, str, Signature]] = [
    (protocol, member, expected)
    for protocol, table in FROZEN
    for member, expected in table.items()
]


def _callable_of(protocol: type, member: str) -> Any:
    """Return the function behind a member, unwrapping a property."""
    declared = vars(protocol)[member]
    return declared.fget if isinstance(declared, property) else declared


def _actual(protocol: type, member: str) -> Signature:
    """Return the parameters a port member actually declares, after self."""
    signature = inspect.signature(_callable_of(protocol, member))
    return tuple(
        (parameter.name, parameter.kind, parameter.default)
        for parameter in signature.parameters.values()
        if parameter.name != "self"
    )


def test_the_transcription_covers_every_member_of_every_port() -> None:
    # The table is only a contract if it is complete: a member missing from it is a member
    # nothing pins.
    for protocol, table in FROZEN:
        declared = {name for name in vars(protocol) if not name.startswith("_")}
        assert declared == set(table), protocol.__name__
    # 18 + 2 + 13 + 4 + 7 + 8 + 1, counted from section 4 rather than from the code.
    assert [len(table) for _, table in FROZEN] == [18, 2, 13, 4, 7, 8, 1]
    assert len(CASES) == 53


@pytest.mark.parametrize(
    ("protocol", "member", "expected"),
    CASES,
    ids=[f"{protocol.__name__}.{member}" for protocol, member, _ in CASES],
)
def test_the_signature_matches_the_frozen_contract(
    protocol: type, member: str, expected: Signature
) -> None:
    assert _actual(protocol, member) == expected


@pytest.mark.parametrize(
    ("protocol", "member"),
    [(protocol, member) for protocol, member, _ in CASES],
    ids=[f"{protocol.__name__}.{member}" for protocol, member, _ in CASES],
)
def test_every_port_member_is_annotated_and_returns_something(
    protocol: type, member: str
) -> None:
    signature = inspect.signature(_callable_of(protocol, member))
    assert signature.return_annotation is not inspect.Signature.empty
    for parameter in signature.parameters.values():
        if parameter.name == "self":
            continue
        assert parameter.annotation is not inspect.Signature.empty, parameter.name


KEYWORD_ONLY_MEMBERS: tuple[tuple[type, str, str], ...] = (
    (StorageDevice, "create", "exclusive"),
    (ProcessCoordinator, "acquire_writer_lease", "timeout"),
    (ProcessCoordinator, "detect_dead_owner", "stall_threshold"),
    (ProcessCoordinator, "exclusive", "timeout"),
    (PageCodec, "decode_page", "verify"),
)


@pytest.mark.parametrize(
    ("protocol", "member", "parameter"),
    KEYWORD_ONLY_MEMBERS,
    ids=[f"{p.__name__}.{m}.{a}" for p, m, a in KEYWORD_ONLY_MEMBERS],
)
def test_a_keyword_only_parameter_stays_keyword_only(
    protocol: type, member: str, parameter: str
) -> None:
    # The drift that compiles and breaks a caller: a keyword-only parameter quietly becoming
    # positional lets an adapter accept a call the contract forbids, and a mistyped positional
    # argument then lands in it silently.
    signature = inspect.signature(_callable_of(protocol, member))
    assert signature.parameters[parameter].kind is inspect.Parameter.KEYWORD_ONLY


DEFAULTS: tuple[tuple[type, str, str, Any], ...] = (
    (StorageDevice, "create", "exclusive", True),
    (StorageDevice, "list_files", "prefix", ""),
    (StorageDevice, "allocate", "count", 1),
    (StorageDevice, "durable_barrier", "file", None),
    (PageCodec, "decode_page", "verify", True),
    (MetricsSink, "increment", "value", 1.0),
    (MetricsSink, "increment", "labels", None),
    (MetricsSink, "set_gauge", "labels", None),
    (MetricsSink, "observe", "labels", None),
    (MetricsSink, "time", "labels", None),
)


@pytest.mark.parametrize(
    ("protocol", "member", "parameter", "default"),
    DEFAULTS,
    ids=[f"{p.__name__}.{m}.{a}" for p, m, a, _ in DEFAULTS],
)
def test_a_default_is_the_one_the_contract_names(
    protocol: type, member: str, parameter: str, default: Any
) -> None:
    # Asserted against the literal from the contract, not against the code: a default read out
    # of the module under test would agree with it whatever it said (A68).
    signature = inspect.signature(_callable_of(protocol, member))
    actual = signature.parameters[parameter].default
    assert actual == default
    assert type(actual) is type(default)


def test_the_properties_of_the_contract_are_properties() -> None:
    # name, page_size, format_version and enabled are properties in section 4; a method would
    # read as a bound method wherever a value is expected.
    for protocol, member in (
        (StorageDevice, "name"),
        (StorageDevice, "page_size"),
        (PageCodec, "format_version"),
        (MetricsSink, "enabled"),
        (VectorMath, "name"),
    ):
        assert isinstance(vars(protocol)[member], property), f"{protocol.__name__}.{member}"


def test_the_methods_of_the_contract_are_not_properties() -> None:
    for protocol, member in (
        (StorageDevice, "read_page"),
        (Clock, "monotonic"),
        (ProcessCoordinator, "takeover"),
        (EventSink, "emit"),
    ):
        assert not isinstance(vars(protocol)[member], property)
