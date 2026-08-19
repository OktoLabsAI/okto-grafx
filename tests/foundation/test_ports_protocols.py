"""Every port is a runtime checkable Protocol satisfied by an ordinary object (CONTRACT.md 4)."""

from __future__ import annotations

import dataclasses
import inspect
from typing import Protocol

import pytest

from okto_grafx.domain import ports
from okto_grafx.domain.ports import (
    Clock,
    DeadOwnerReport,
    EventSink,
    Lease,
    MetricsSink,
    PageCodec,
    ProcessCoordinator,
    ReaderHandle,
    StorageDevice,
    VectorMath,
)

PORTS: tuple[type, ...] = (
    StorageDevice,
    Clock,
    ProcessCoordinator,
    PageCodec,
    MetricsSink,
    VectorMath,
    EventSink,
)

# The frozen member list of every port, transcribed from CONTRACT.md section 4.
EXPECTED_MEMBERS: dict[str, frozenset[str]] = {
    "StorageDevice": frozenset(
        {
            "name",
            "page_size",
            "exists",
            "create",
            "remove",
            "list_files",
            "file_size",
            "atomic_replace",
            "recycle",
            "page_count",
            "allocate",
            "read_page",
            "write_page",
            "append_log",
            "read_log",
            "log_size",
            "truncate_log",
            "durable_barrier",
        }
    ),
    "Clock": frozenset({"monotonic", "wall"}),
    "ProcessCoordinator": frozenset(
        {
            "owner_id",
            "current_epoch",
            "acquire_writer_lease",
            "renew_lease",
            "release_lease",
            "validate_epoch",
            "detect_dead_owner",
            "takeover",
            "register_reader",
            "refresh_reader",
            "unregister_reader",
            "reader_horizon",
            "exclusive",
        }
    ),
    "PageCodec": frozenset({"format_version", "checksum", "encode_page", "decode_page"}),
    "MetricsSink": frozenset(
        {"enabled", "register", "increment", "set_gauge", "observe", "time", "snapshot"}
    ),
    "VectorMath": frozenset(
        {"name", "dot", "cosine", "euclidean", "norm", "normalize", "score", "top_k"}
    ),
    "EventSink": frozenset({"emit"}),
}


def _members(protocol: type) -> frozenset[str]:
    return frozenset(
        name
        for name in vars(protocol)
        if not name.startswith("_") and name not in {"mro"}
    )


@pytest.mark.parametrize("protocol", PORTS, ids=lambda protocol: protocol.__name__)
def test_port_is_a_protocol(protocol: type) -> None:
    assert Protocol in protocol.__mro__
    assert getattr(protocol, "_is_protocol", False) is True


@pytest.mark.parametrize("protocol", PORTS, ids=lambda protocol: protocol.__name__)
def test_port_is_runtime_checkable(protocol: type) -> None:
    # isinstance against a protocol that is not runtime checkable raises TypeError; the call
    # below therefore proves the decorator is present without reading a private attribute.
    assert isinstance(object(), protocol) is False


@pytest.mark.parametrize("protocol", PORTS, ids=lambda protocol: protocol.__name__)
def test_port_exposes_exactly_the_contract_members(protocol: type) -> None:
    assert _members(protocol) == EXPECTED_MEMBERS[protocol.__name__]


@pytest.mark.parametrize("protocol", PORTS, ids=lambda protocol: protocol.__name__)
def test_every_port_member_is_documented_and_annotated(protocol: type) -> None:
    assert protocol.__doc__
    for name in _members(protocol):
        member = vars(protocol)[name]
        function = member.fget if isinstance(member, property) else member
        assert function.__doc__, f"{protocol.__name__}.{name} has no docstring"
        signature = inspect.signature(function)
        assert signature.return_annotation is not inspect.Signature.empty
        for parameter in signature.parameters.values():
            if parameter.name == "self":
                continue
            assert parameter.annotation is not inspect.Signature.empty, (
                f"{protocol.__name__}.{name} parameter {parameter.name} is not annotated"
            )


def test_fakes_satisfy_every_port(fake_ports: dict[str, object]) -> None:
    expected = {
        "storage": StorageDevice,
        "clock": Clock,
        "coordinator": ProcessCoordinator,
        "codec": PageCodec,
        "metrics": MetricsSink,
        "vector_math": VectorMath,
        "events": EventSink,
    }
    for slot, protocol in expected.items():
        assert isinstance(fake_ports[slot], protocol), f"{slot} does not satisfy {protocol.__name__}"


def test_a_partial_implementation_does_not_satisfy_a_port() -> None:
    class HalfClock:
        def monotonic(self) -> float:
            return 0.0

    assert isinstance(HalfClock(), Clock) is False


def test_storage_port_has_no_positional_write_primitive() -> None:
    # SPEC-M1 FR-5: no code path can write beyond the end of a file, because the port never
    # offers the primitive that would allow it.
    forbidden = {"write_at", "pwrite", "seek", "write", "write_bytes"}
    assert forbidden.isdisjoint(_members(StorageDevice))


def test_clock_keeps_liveness_and_wall_time_apart() -> None:
    assert _members(Clock) == {"monotonic", "wall"}


def test_coordination_value_objects_are_frozen() -> None:
    lease = Lease(
        owner_id="owner", epoch=1, acquired_monotonic=0.5, heartbeat_seq=3, ttl_seconds=5.0
    )
    handle = ReaderHandle(reader_id="reader-1", snapshot_lsn=42)
    report = DeadOwnerReport(owner_id="owner", last_heartbeat_seq=3, observed_stall_seconds=17.5)
    for value, field in ((lease, "owner_id"), (handle, "reader_id"), (report, "owner_id")):
        assert not hasattr(value, "__dict__")
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(value, field, "other")
    assert lease.epoch == 1
    assert handle.snapshot_lsn == 42
    assert report.observed_stall_seconds == 17.5


def test_ports_package_reexports_every_public_symbol() -> None:
    for name in ports.__all__:
        assert hasattr(ports, name), name
    for protocol in PORTS:
        assert getattr(ports, protocol.__name__) is protocol
