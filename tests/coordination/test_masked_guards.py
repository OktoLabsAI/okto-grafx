"""Guards that are correct but were masked by a sibling term, and the two thresholds (A31, A62).

A guard whose reversion leaves the suite green is an untested guard, however right its code is
today. Each test here is written so that exactly one term can satisfy it: the sibling term is
arranged to pass, so only the term under test can produce the refusal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import CoordinatorFactory
from coordination_support import DirectoryStorageDevice, ManualClock, owned_by
from okto_grafx.adapters.coordination_local import (
    LeaseRecord,
    decode_lease_record,
    encode_lease_record,
)
from okto_grafx.domain.errors import (
    GrafxLeaseStolen,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.ports.coordination import ReaderHandle

# --- D3: the prefix term of the reader-identity guard ------------------------------------------


def test_a_reader_of_another_participant_is_refused_by_the_prefix_alone(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    """Two participants, equal-length identifiers, both at counter 0001.

    The counter term cannot answer this: slicing a foreign identifier by a prefix of the same
    length leaves exactly ``0001``, which is a number this instance really has minted. Only the
    prefix -- and the instance nonce inside it -- distinguishes them, and what stands on it is a
    live reader's snapshot pin (BR-10): withdrawing a foreign registration releases a horizon
    somebody else is still reading under, and C4 then recycles the segments it needs.
    """
    device = DirectoryStorageDevice(database_root)
    first = make_coordinator(owner_id="p1-alpha", storage=device)
    second = make_coordinator(owner_id="p2-bravo", storage=device, monotonic_origin=90.0)
    assert len("p1-alpha") == len("p2-bravo")

    theirs = first.register_reader(5)
    mine = second.register_reader(9_000)
    assert len(theirs.reader_id) == len(mine.reader_id)
    assert theirs.reader_id.endswith("-r0001") and mine.reader_id.endswith("-r0001")

    with pytest.raises(GrafxUnsupportedOperation):
        second.unregister_reader(theirs)
    with pytest.raises(GrafxUnsupportedOperation):
        second.refresh_reader(ReaderHandle(reader_id=theirs.reader_id, snapshot_lsn=999_999))

    assert first.reader_horizon() == 5
    assert (database_root / "control" / "readers" / f"{theirs.reader_id}.reader").exists()


# --- D4: the epoch and owner terms of the compare-and-set --------------------------------------


def _observed_stall(
    make_coordinator: CoordinatorFactory, *, owner_id: str, origin: float
) -> tuple[object, ManualClock]:
    """Return a participant that has watched the current owner stall, and its clock."""
    clock = ManualClock(monotonic=origin)
    rival = make_coordinator(owner_id=owner_id, clock=clock)
    assert rival.detect_dead_owner(stall_threshold=5.0) is None
    clock.advance(6.0)
    assert rival.detect_dead_owner(stall_threshold=5.0) is not None
    return rival, clock


def test_an_owner_that_released_and_re_acquired_is_not_evicted(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    """Only the epoch changes, because every installation republishes at heartbeat 1.

    A rival holding an observation from before the round trip sees the same owner, the same
    heartbeat and the same held flag. The epoch is the single term that can tell it that the
    lease it decided was abandoned has since been taken up again by a participant that is alive.
    """
    owner = make_coordinator(owner_id="p1-owner")
    lease = owner.acquire_writer_lease(timeout=1.0)
    rival, _clock = _observed_stall(make_coordinator, owner_id="p2-rival", origin=4_000.0)

    owner.release_lease(lease)
    revived = owner.acquire_writer_lease(timeout=1.0)
    assert revived.epoch == 2
    assert revived.heartbeat_seq == 1

    with pytest.raises(GrafxLeaseStolen):
        rival.takeover()
    published = decode_lease_record((database_root / "control" / "writer.lease").read_bytes())
    assert owned_by(published.owner_id, "p1-owner")
    assert published.epoch == 2
    owner.validate_epoch(revived.epoch)


def test_a_different_owner_at_the_same_epoch_is_not_evicted(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    """Only the owner changes, which is what a restore or an operator repair can produce.

    Epoch, heartbeat and held all still match the observation, so the owner term is the only one
    that can refuse -- and without it the rival takes the lease from a participant it has never
    watched at all.
    """
    incumbent = make_coordinator(owner_id="p1-owner")
    incumbent.acquire_writer_lease(timeout=1.0)
    rival, _clock = _observed_stall(make_coordinator, owner_id="p2-rival", origin=7_000.0)

    lease_file = database_root / "control" / "writer.lease"
    observed = decode_lease_record(lease_file.read_bytes())
    lease_file.write_bytes(
        encode_lease_record(
            LeaseRecord(
                owner_id="p9-repair",
                epoch=observed.epoch,
                heartbeat_seq=observed.heartbeat_seq,
                ttl_seconds=observed.ttl_seconds,
                wall_stamp=observed.wall_stamp,
                held=observed.held,
                superseded_epoch=observed.superseded_epoch,
            )
        )
    )

    with pytest.raises(GrafxLeaseStolen):
        rival.takeover()
    assert decode_lease_record(lease_file.read_bytes()).owner_id == "p9-repair"


# --- D5: the two thresholds, asserted at the boundary itself -----------------------------------


def test_an_owner_stall_is_reported_only_past_the_threshold(
    make_coordinator: CoordinatorFactory
) -> None:
    owner = make_coordinator(owner_id="p1-owner")
    owner.acquire_writer_lease(timeout=1.0)
    clock = ManualClock(monotonic=2_000.0)
    observer = make_coordinator(owner_id="p2-watch", clock=clock)
    assert observer.detect_dead_owner(stall_threshold=5.0) is None
    clock.advance(5.0)
    assert observer.detect_dead_owner(stall_threshold=5.0) is None, "exactly the threshold is alive"
    clock.advance(0.000_001)
    report = observer.detect_dead_owner(stall_threshold=5.0)
    assert report is not None and report.observed_stall_seconds > 5.0


def test_a_reader_is_pruned_only_past_the_threshold(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    quiet = make_coordinator(owner_id="p2-quiet", monotonic_origin=11.0)
    handle = quiet.register_reader(64)
    clock = ManualClock(monotonic=3_000.0)
    observer = make_coordinator(owner_id="p1-watch", clock=clock)
    assert observer.reader_horizon() == 64
    clock.advance(15.0)
    assert observer.reader_horizon() == 64, "exactly the threshold is still a live reader"
    clock.advance(0.000_001)
    assert observer.reader_horizon() is None
    assert not (database_root / "control" / "readers" / f"{handle.reader_id}.reader").exists()
