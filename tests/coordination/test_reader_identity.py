"""The reader registry under a shared owner identifier and under concurrent housekeeping.

Three defects live here, and all three end in the same place: a live reader loses the snapshot it
pinned, silently, and C4 recycles WAL segments that reader still needs (BR-10, AC-8). None of
them needs a hostile caller -- two coordinators built with one ``owner_id`` is enough, and that is
a configuration mistake, not an attack.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import CoordinatorFactory
from coordination_support import DirectoryStorageDevice, ManualClock, owned_by
from okto_grafx.adapters.coordination_local import decode_reader_record
from okto_grafx.domain.errors import GrafxUnsupportedOperation
from okto_grafx.engine.coordination import recyclable_horizon

# --- M1: housekeeping must never touch a publication in flight --------------------------------


def test_a_concurrent_horizon_pass_does_not_destroy_a_first_registration(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    # The stray sweep decided by "is this reader live", and a FIRST registration has no record
    # yet, so its in-flight temporary was always eligible for deletion by anybody else.
    device = DirectoryStorageDevice(database_root)
    housekeeper = make_coordinator(owner_id="p1-keeper", storage=device)
    reader_side = make_coordinator(
        owner_id="p2-reader", storage=device, monotonic_origin=4_000.0
    )
    housekeeper.reader_horizon()

    class Interfering(DirectoryStorageDevice):
        """A device that lets the housekeeper run at the worst possible moment of a publish."""

        def atomic_replace(self, source: str, target: str) -> None:
            """Sweep from another participant between the temporary and its replacement."""
            if target.endswith(".reader"):
                housekeeper.reader_horizon()
            super().atomic_replace(source, target)

    interfering = Interfering(database_root)
    reader_side = make_coordinator(
        owner_id="p2-reader", storage=interfering, monotonic_origin=4_000.0
    )
    handle = reader_side.register_reader(7)
    assert handle.snapshot_lsn == 7
    assert reader_side.reader_horizon() == 7
    assert housekeeper.reader_horizon() == 7


def test_a_stray_is_only_swept_once_it_has_outlived_the_stall_threshold(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    # Evidence over time, the same rule the rest of this component uses: a temporary that has
    # been sitting there longer than a reader may stay silent belongs to a process that is gone.
    clock = ManualClock(monotonic=1_000.0)
    coordinator = make_coordinator(owner_id="p1-keeper", clock=clock)
    coordinator.register_reader(5)
    readers = database_root / "control" / "readers"
    stray = readers / "p9-crashed-r0001.reader.p9-crashed.tmp"
    stray.write_bytes(b"half a record")

    assert coordinator.reader_horizon() == 5
    assert stray.exists(), "a temporary of unknown age is not evidence of anything"
    clock.advance(16.0)
    assert coordinator.reader_horizon() == 5
    assert not stray.exists()


def test_a_publish_whose_temporary_vanishes_starts_again(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    # The publish sequence builds its own temporary, so a missing source is the one failure a
    # retry is guaranteed to fix. Reporting it as permanent forbids the only action that works.
    device = DirectoryStorageDevice(database_root)
    state = {"stolen": False}

    class Vanishing(DirectoryStorageDevice):
        """A device where somebody removes the temporary just before it is replaced."""

        def atomic_replace(self, source: str, target: str) -> None:
            """Delete the source once, the way a sweep from another participant would."""
            if not state["stolen"] and target.endswith(".reader"):
                state["stolen"] = True
                self.remove(source)
            super().atomic_replace(source, target)

    coordinator = make_coordinator(owner_id="p1-reader", storage=Vanishing(database_root))
    handle = coordinator.register_reader(11)
    assert state["stolen"] is True
    assert coordinator.reader_horizon() == 11
    assert handle.snapshot_lsn == 11
    assert device.exists(f"control/readers/{handle.reader_id}.reader")


def test_a_refresh_that_races_the_sweep_still_resurrects(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    # BR-10 leans on refresh re-creating a pruned registration; the same vanishing temporary
    # would otherwise turn a quiet reader into an evicted one.
    state = {"stolen": False}

    class Vanishing(DirectoryStorageDevice):
        """A device where the temporary of a refresh disappears once."""

        def atomic_replace(self, source: str, target: str) -> None:
            """Delete the source of the second publication only."""
            if not state["stolen"] and target.endswith(".reader") and self.exists(target):
                state["stolen"] = True
                self.remove(source)
            super().atomic_replace(source, target)

    coordinator = make_coordinator(owner_id="p1-reader", storage=Vanishing(database_root))
    handle = coordinator.register_reader(3)
    coordinator.refresh_reader(handle)
    assert state["stolen"] is True
    assert coordinator.reader_horizon() == 3


# --- M2: two coordinators sharing an owner identifier mint different reader identifiers --------


def test_two_coordinators_sharing_an_owner_never_mint_the_same_reader(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    # A per-instance counter under a shared owner identifier produced -r0001 twice, so the second
    # registration overwrote the first and the horizon jumped from 5 to 9000 with nothing raised.
    device = DirectoryStorageDevice(database_root)
    first = make_coordinator(owner_id="p1-same", storage=device)
    second = make_coordinator(owner_id="p1-same", storage=device, monotonic_origin=6_000.0)

    early = first.register_reader(5)
    assert first.reader_horizon() == 5
    late = second.register_reader(9_000)
    assert late.reader_id != early.reader_id

    assert first.reader_horizon() == 5, "the older snapshot must still pin the horizon"
    assert second.reader_horizon() == 5
    assert recyclable_horizon(first.reader_horizon(), 10_000) == 5

    files = sorted(
        path.name for path in (database_root / "control" / "readers").iterdir()
        if path.name.endswith(".reader")
    )
    assert len(files) == 2, files
    records = [
        decode_reader_record((database_root / "control" / "readers" / name).read_bytes())
        for name in files
    ]
    assert sorted(record.snapshot_lsn for record in records) == [5, 9_000]


def test_closing_one_registration_does_not_close_the_other(
    make_coordinator: CoordinatorFactory
) -> None:
    device_holder = make_coordinator(owner_id="p1-same")
    first = make_coordinator(owner_id="p1-same", storage=device_holder._storage)
    second = make_coordinator(
        owner_id="p1-same", storage=device_holder._storage, monotonic_origin=6_000.0
    )
    early = first.register_reader(5)
    late = second.register_reader(9_000)
    second.unregister_reader(late)
    assert first.reader_horizon() == 5
    assert recyclable_horizon(first.reader_horizon(), 10_000) == 5
    first.unregister_reader(early)
    assert first.reader_horizon() is None


def test_the_reader_identifier_carries_an_instance_nonce(
    make_coordinator: CoordinatorFactory
) -> None:
    first = make_coordinator(owner_id="p1-same")
    second = make_coordinator(owner_id="p1-same", monotonic_origin=6_000.0)
    ours = first.register_reader(1).reader_id
    theirs = second.register_reader(1).reader_id
    assert ours.startswith("p1-same-")
    assert theirs.startswith("p1-same-")
    assert ours != theirs
    assert ours.endswith("-r0001") and theirs.endswith("-r0001")


# --- M3: a registration is refreshed and withdrawn only by the coordinator that issued it ------


def test_a_foreign_registration_cannot_be_withdrawn(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    device = DirectoryStorageDevice(database_root)
    owner = make_coordinator(owner_id="p1-same", storage=device)
    other = make_coordinator(owner_id="p1-same", storage=device, monotonic_origin=6_000.0)
    handle = owner.register_reader(5)
    with pytest.raises(GrafxUnsupportedOperation):
        other.unregister_reader(handle)
    assert owner.reader_horizon() == 5
    assert (database_root / "control" / "readers" / f"{handle.reader_id}.reader").exists()


def test_a_foreign_registration_cannot_be_rewritten(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    from okto_grafx.domain.ports.coordination import ReaderHandle

    device = DirectoryStorageDevice(database_root)
    owner = make_coordinator(owner_id="p1-same", storage=device)
    other = make_coordinator(owner_id="p1-same", storage=device, monotonic_origin=6_000.0)
    handle = owner.register_reader(5)
    with pytest.raises(GrafxUnsupportedOperation):
        other.refresh_reader(ReaderHandle(reader_id=handle.reader_id, snapshot_lsn=999_999))
    record = decode_reader_record(
        (database_root / "control" / "readers" / f"{handle.reader_id}.reader").read_bytes()
    )
    assert record.snapshot_lsn == 5
    assert owner.reader_horizon() == 5


def test_withdrawing_a_registration_twice_is_still_quiet(
    make_coordinator: CoordinatorFactory
) -> None:
    coordinator = make_coordinator(owner_id="p1-aaaa")
    handle = coordinator.register_reader(3)
    coordinator.unregister_reader(handle)
    coordinator.unregister_reader(handle)
    assert coordinator.reader_horizon() is None


def test_refreshing_a_withdrawn_registration_is_refused(
    make_coordinator: CoordinatorFactory
) -> None:
    coordinator = make_coordinator(owner_id="p1-aaaa")
    handle = coordinator.register_reader(3)
    coordinator.unregister_reader(handle)
    with pytest.raises(GrafxUnsupportedOperation):
        coordinator.refresh_reader(handle)


# --- the identity that decides a renewal is this participant own, not the argument -------------


def test_renewing_a_foreign_lease_while_holding_one_of_our_own_is_refused(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    # The dangerous shape is not an impostor holding nothing -- that fails on the second guard.
    # It is a participant that DOES hold this epoch being handed somebody else lease object: it
    # would renew its own lease, advance its own heartbeat and report success for a lease it was
    # never asked about.
    from okto_grafx.adapters.coordination_local import decode_lease_record
    from okto_grafx.domain.errors import GrafxLeaseStolen
    from okto_grafx.domain.ports.coordination import Lease

    holder = make_coordinator(owner_id="p1-holder")
    ours = holder.acquire_writer_lease(timeout=1.0)
    foreign = Lease(
        owner_id="p9-other",
        epoch=ours.epoch,
        acquired_monotonic=0.0,
        heartbeat_seq=ours.heartbeat_seq,
        ttl_seconds=ours.ttl_seconds,
    )
    with pytest.raises(GrafxLeaseStolen) as failure:
        holder.renew_lease(foreign)
    assert failure.value.details["lease_owner"] == "p9-other"
    published = decode_lease_record((database_root / "control" / "writer.lease").read_bytes())
    assert published.heartbeat_seq == ours.heartbeat_seq, "our own heartbeat moved"
    assert owned_by(published.owner_id, "p1-holder")


def test_releasing_a_foreign_lease_while_holding_one_of_our_own_is_refused(
    make_coordinator: CoordinatorFactory, database_root: Path
) -> None:
    from okto_grafx.adapters.coordination_local import decode_lease_record
    from okto_grafx.domain.errors import GrafxLeaseStolen
    from okto_grafx.domain.ports.coordination import Lease

    holder = make_coordinator(owner_id="p1-holder")
    ours = holder.acquire_writer_lease(timeout=1.0)
    foreign = Lease(
        owner_id="p9-other",
        epoch=ours.epoch,
        acquired_monotonic=0.0,
        heartbeat_seq=ours.heartbeat_seq,
        ttl_seconds=ours.ttl_seconds,
    )
    with pytest.raises(GrafxLeaseStolen) as failure:
        holder.release_lease(foreign)
    assert failure.value.details["lease_owner"] == "p9-other"
    published = decode_lease_record((database_root / "control" / "writer.lease").read_bytes())
    assert published.held is True, "our own lease was vacated on somebody else request"
    holder.validate_epoch(ours.epoch)


def test_an_identifier_this_instance_never_minted_is_refused_by_both_doors(
    make_coordinator: CoordinatorFactory
) -> None:
    # The two doors of the registry agree: "issued here" means this instance minted THIS one, not
    # that the name looks like something it could have minted.
    from okto_grafx.domain.ports.coordination import ReaderHandle

    coordinator = make_coordinator(owner_id="p1-aaaa")
    real = coordinator.register_reader(5)
    prefix = real.reader_id[: -len("0001")]
    forged = ReaderHandle(reader_id=f"{prefix}0009", snapshot_lsn=5)
    with pytest.raises(GrafxUnsupportedOperation):
        coordinator.unregister_reader(forged)
    with pytest.raises(GrafxUnsupportedOperation):
        coordinator.refresh_reader(forged)
    assert coordinator.reader_horizon() == 5
