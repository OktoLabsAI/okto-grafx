"""CE-1 crash matrix for in-place two-slot control publications.

These tests exercise the physical publication primitive independently of the logical lease and
commit-state codecs.  The integration suites prove those codecs keep their existing meaning;
this matrix proves that a power loss can expose only the previous or the next whole payload.
"""

from __future__ import annotations

import pytest

from okto_grafx.adapters.storage_fault import (
    FaultInjectingStorageDevice,
    FaultPlan,
    SimulatedCrash,
)
from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.domain.control_record import (
    CONTROL_FILE_PAGES,
    ControlRecordKind,
    TwoSlotControlRecordStore,
)
from okto_grafx.domain.errors import GrafxCorruptionDetected

FILE = "control/commit.state"
TEMP = "control/commit.state.ce1.tmp"
DATABASE_UUID = bytes.fromhex("d41d8cd98f004204e9800998ecf8427e")


def _store(storage: object) -> TwoSlotControlRecordStore:
    """Bind a fresh store object to one physical record on ``storage``."""
    return TwoSlotControlRecordStore(
        storage,  # type: ignore[arg-type]
        file=FILE,
        record_kind=ControlRecordKind.COMMIT_STATE,
        database_uuid=DATABASE_UUID,
        file_nonce=20260831,
        temporary=TEMP,
    )


def _payload(storage: object) -> tuple[bytes, int]:
    """Read through a new participant so its generation cache starts at zero."""
    observed = _store(storage).read()
    assert observed is not None
    return observed.payload, observed.generation


def _bootstrapped(*, page_size: int = 8192) -> tuple[
    MemoryStorageDevice, FaultInjectingStorageDevice
]:
    """Return a slot file whose generation one is already durable."""
    inner = MemoryStorageDevice(page_size=page_size)
    bench = FaultInjectingStorageDevice(inner, seed=20260831)
    assert _store(bench).publish(b"one") == 1
    bench.clear_trail()
    return inner, bench


def test_crash_before_write_page_leaves_the_previous_generation() -> None:
    inner, bench = _bootstrapped()
    try:
        bench.crash_on("write_page", moment="before")
        with pytest.raises(SimulatedCrash):
            _store(bench).publish(b"two")
        bench.disarm()
        assert _payload(bench) == (b"one", 1)
    finally:
        inner.close()


@pytest.mark.parametrize("lose_volatile_write", [False, True])
def test_crash_between_write_and_barrier_serves_either_generation_never_a_torn_one(
    lose_volatile_write: bool,
) -> None:
    inner, bench = _bootstrapped()
    try:
        bench.arm(
            FaultPlan(
                crash_method="durable_barrier",
                crash_moment="before",
                lying_barrier=lose_volatile_write,
            )
        )
        with pytest.raises(SimulatedCrash):
            _store(bench).publish(b"two")
        bench.disarm()
        expected = (b"one", 1) if lose_volatile_write else (b"two", 2)
        assert _payload(bench) == expected
    finally:
        inner.close()


def test_crash_after_barrier_serves_the_new_generation() -> None:
    inner, bench = _bootstrapped()
    try:
        bench.crash_on("durable_barrier", moment="after")
        with pytest.raises(SimulatedCrash):
            _store(bench).publish(b"two")
        bench.disarm()
        assert _payload(bench) == (b"two", 2)
    finally:
        inner.close()


_TORN_WRITES = [
    *(pytest.param(8192, offset, id=f"8k-sector-{offset}") for offset in range(512, 8192, 512)),
    pytest.param(8192, 32, id="8k-after-page-checksum-header"),
    pytest.param(8192, 96, id="8k-inside-slot-record"),
    pytest.param(512, 1, id="minimum-page-first-byte"),
    pytest.param(512, 32, id="minimum-page-after-header"),
    pytest.param(512, 256, id="minimum-page-middle"),
    pytest.param(512, 511, id="minimum-page-last-byte"),
]


@pytest.mark.parametrize(("page_size", "stored_bytes"), _TORN_WRITES)
def test_partial_page_write_serves_only_a_whole_generation_and_the_next_publish_repairs(
    page_size: int, stored_bytes: int
) -> None:
    inner, bench = _bootstrapped(page_size=page_size)
    try:
        bench.write_partially(stored_bytes, method="write_page")
        assert _store(bench).publish(b"torn") == 2
        bench.disarm()

        # Control payloads fit in the first sector.  A sector-boundary partial write can
        # therefore be byte-identical to the complete new page (checksum included), which is a
        # valid new generation rather than corruption.  A cut inside the record falls back to
        # the old slot.  No hybrid payload is ever accepted in either case.
        before_repair = _payload(bench)
        assert before_repair in {(b"one", 1), (b"torn", 2)}
        expected_generation = before_repair[1] + 1
        assert _store(bench).publish(b"repaired") == expected_generation
        assert _payload(bench) == (b"repaired", expected_generation)
    finally:
        inner.close()


def test_the_newest_slot_is_never_the_one_being_rewritten() -> None:
    inner, bench = _bootstrapped()
    try:
        assert _store(bench).publish(b"two") == 2
        newest_before = inner.read_page(FILE, 2)
        bench.clear_trail()
        bench.write_partially(64, method="write_page")

        assert _store(bench).publish(b"three-but-torn") == 3
        bench.disarm()
        assert inner.read_page(FILE, 2) == newest_before
        assert _payload(bench) == (b"two", 2)
    finally:
        inner.close()


def test_double_corruption_is_fail_closed() -> None:
    inner, bench = _bootstrapped()
    try:
        assert _store(bench).publish(b"two") == 2
        for page in (1, 2):
            damaged = bytearray(inner.read_page(FILE, page))
            damaged[-page] ^= 0xFF
            inner.write_page(FILE, page, bytes(damaged))
        with pytest.raises(GrafxCorruptionDetected, match="Both slots"):
            _store(bench).read()
    finally:
        inner.close()


@pytest.mark.parametrize("moment", ["before", "after"])
def test_crash_during_bootstrap_is_repeated_on_the_next_open(moment: str) -> None:
    survey = FaultInjectingStorageDevice(MemoryStorageDevice(), seed=20260831)
    try:
        points = survey.enumerate_write_points(lambda _device: _store(survey).publish(b"one"))
        call_indices = tuple(point.call_index for point in points)
        assert call_indices
    finally:
        survey.close()

    for call_index in call_indices:
        inner = MemoryStorageDevice()
        bench = FaultInjectingStorageDevice(inner, seed=20260831)
        try:
            bench.crash_at(call_index, moment=moment)
            with pytest.raises(SimulatedCrash):
                _store(bench).publish(b"one")
            bench.disarm()

            _store(bench).publish(b"settled")
            assert _payload(bench)[0] == b"settled"
            assert inner.file_size(FILE) == CONTROL_FILE_PAGES * inner.page_size
            assert not inner.exists(TEMP)
        finally:
            inner.close()


def test_ten_thousand_publications_never_regress_or_grow_the_file() -> None:
    """A deterministic property run covers long alternating sequences without a new dependency."""
    inner = MemoryStorageDevice(page_size=512)
    try:
        writer = _store(inner)
        reader = _store(inner)
        for generation in range(1, 10_001):
            payload = generation.to_bytes(8, "little")
            assert writer.publish(payload) == generation
            observed = reader.read()
            assert observed is not None
            assert (observed.payload, observed.generation) == (payload, generation)
            assert inner.file_size(FILE) == CONTROL_FILE_PAGES * inner.page_size
    finally:
        inner.close()
