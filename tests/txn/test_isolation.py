"""Snapshot isolation across participants (SPEC-M1 FR-2, BR-9, AC-3).

The interesting question is not whether a reader sees a commit it should not -- the visibility
predicate answers that on its own. It is whether a reader can ever be handed a snapshot number
whose pages are only half in place. That is a property of the ORDER a commit does its work in,
so it is probed after every single device call a commit makes.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from okto_grafx.domain.ids import NO_LSN
from okto_grafx.domain.txn import Snapshot
from shared_device import SharedDirectoryDevice
from txn_support import (
    DEFAULT_PAGE_SIZE,
    Stack,
    build_stack,
    make_page_image,
    page_lsn_of,
    read_page_payloads,
)

HEAP = "heap.dat"
PAGES: tuple[int, ...] = (3, 4, 5, 6)
FULL_PROBE_METHODS = frozenset({"atomic_replace", "durable_barrier"})


class ProbeDevice:
    """A device that runs a callback after every call that changes anything.

    Re-entrancy is guarded because the probe itself reads and writes through the device: a probe
    that fired inside its own reads would recurse forever, and a probe that fired inside its own
    writes would report a state nobody could reach. The probe is armed by the test AFTER both
    participants exist, so nothing fires while the fixture is still being assembled.
    """

    WATCHED = frozenset(
        {
            "create",
            "remove",
            "atomic_replace",
            "recycle",
            "allocate",
            "write_page",
            "append_log",
            "truncate_log",
            "durable_barrier",
        }
    )

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self._probe: Callable[[str], None] | None = None
        self._busy = False

    def arm(self, probe: Callable[[str], None]) -> None:
        """Start running the probe after every watched call."""
        self._probe = probe

    def disarm(self) -> None:
        """Stop running the probe."""
        self._probe = None

    def __getattr__(self, name: str) -> object:
        attribute = getattr(self._inner, name)
        if name not in self.WATCHED or not callable(attribute):
            return attribute

        def _call(*args: object, **kwargs: object) -> object:
            result = attribute(*args, **kwargs)
            probe = self._probe
            if probe is not None and not self._busy:
                self._busy = True
                try:
                    probe(name)
                finally:
                    self._busy = False
            return result

        return _call

    @property
    def name(self) -> str:
        """Return the label of the device underneath."""
        return self._inner.name  # type: ignore[attr-defined]

    @property
    def page_size(self) -> int:
        """Return the page size of the device underneath."""
        return self._inner.page_size  # type: ignore[attr-defined]


def test_no_instant_of_a_commit_offers_a_snapshot_of_half_of_it(
    database_root: Path,
) -> None:
    """AC-3: no interleaving produces a mixed or partial read.

    A second participant asks for a snapshot after EVERY device call the commit makes. Two
    outcomes are allowed and nothing else: a snapshot below the new commit, which sees none of
    it, or a snapshot at the new commit, and then every page of that commit is already on the
    device where any process can read it.
    """
    inner = SharedDirectoryDevice(database_root, page_size=DEFAULT_PAGE_SIZE)
    device = ProbeDevice(inner)
    writer_side = build_stack(database_root, storage=device, owner_id="writer")
    reader_side = build_stack(database_root, storage=inner, owner_id="reader")
    observations: list[tuple[int, tuple[int, ...]]] = []
    opened: list[int] = []

    def probe(method: str) -> None:
        observations.append((reader_side.manager.published_lsn(), _page_lsns(reader_side)))
        if method in FULL_PROBE_METHODS:
            # The cheap probe reads the number a reader would be given; this one goes through
            # the real door as well, so the property is not only proved about an accessor.
            txn = reader_side.manager.begin("read")
            try:
                opened.append(txn.snapshot.read_lsn)
                observations.append((txn.snapshot.read_lsn, _page_lsns(reader_side)))
            finally:
                reader_side.manager.rollback(txn)

    txn = writer_side.manager.begin("write")
    for page in PAGES:
        txn.owner._stage_page_image(txn,
            HEAP, page, make_page_image(writer_side.codec, [b"batch"], page_index=page)
        )
    txn.note_write(writer_side.manager.partition_of(1, b"batch"))
    device.arm(probe)
    try:
        report = writer_side.manager.commit(txn)
    finally:
        device.disarm()

    assert observations, "the probe never ran, so this test proved nothing"
    assert opened, "the real begin() door was never exercised by the probe"
    before = 0
    after = 0
    for read_lsn, page_numbers in observations:
        assert read_lsn in {NO_LSN, report.csn}
        if read_lsn == report.csn:
            after += 1
            assert set(page_numbers) == {report.csn}, (
                "a snapshot at the commit must find every page of that commit in place"
            )
        else:
            before += 1
            assert Snapshot(read_lsn).visible(report.csn, 0) is False
    assert before > 0 and after > 0, (
        "the probe has to cover both sides of the publication to prove anything"
    )


def _page_lsns(stack: Stack) -> tuple[int, ...]:
    """Return the page LSN of every page of the batch that the device currently holds."""
    return tuple(
        page_lsn_of(stack.pool, HEAP, page) if _has_page(stack, page) else NO_LSN
        for page in PAGES
    )


def _has_page(stack: Stack, page_index: int) -> bool:
    """Return True when the heap file is long enough to hold that page."""
    return stack.storage.exists(HEAP) and stack.storage.page_count(HEAP) > page_index


def test_a_reader_keeps_its_view_across_a_parallel_commit(make_stack) -> None:
    """BR-9: the view is fixed at the moment the transaction opened."""
    reader_side = make_stack()
    writer_side = make_stack()
    reader = reader_side.manager.begin("read")
    opened_at = reader.snapshot.read_lsn
    txn = writer_side.manager.begin("write")
    txn.owner._stage_page_image(txn, HEAP, 3, make_page_image(writer_side.codec, [b"later"], page_index=3))
    txn.note_write(writer_side.manager.partition_of(1, b"later"))
    report = writer_side.manager.commit(txn)
    assert reader.snapshot.read_lsn == opened_at
    assert reader.snapshot.visible(report.csn, 0) is False
    reader_side.manager.commit(reader)
    fresh = reader_side.manager.begin("read")
    assert fresh.snapshot.visible(report.csn, 0) is True


def test_a_second_snapshot_sees_everything_the_first_one_could(make_stack) -> None:
    """Snapshots move forward only: a later view never loses a commit an earlier one had."""
    reader_side = make_stack()
    writer_side = make_stack()
    numbers: list[int] = []
    for page in PAGES:
        txn = writer_side.manager.begin("write")
        txn.owner._stage_page_image(txn,
            HEAP, page, make_page_image(writer_side.codec, [bytes([page])], page_index=page)
        )
        txn.note_write(writer_side.manager.partition_of(1, bytes([page])))
        numbers.append(writer_side.manager.commit(txn).csn)
        reader = reader_side.manager.begin("read")
        visible = [csn for csn in numbers if reader.snapshot.visible(csn, 0)]
        reader_side.manager.rollback(reader)
        assert visible == numbers


def test_a_committed_page_is_readable_by_another_participant(make_stack) -> None:
    """A page that only exists in the committing process's memory is a commit nobody else has."""
    writer_side = make_stack()
    reader_side = make_stack()
    txn = writer_side.manager.begin("write")
    txn.owner._stage_page_image(txn, HEAP, 3, make_page_image(writer_side.codec, [b"shared"], page_index=3))
    txn.note_write(writer_side.manager.partition_of(1, b"shared"))
    report = writer_side.manager.commit(txn)
    assert reader_side.manager.published_lsn() == report.csn
    assert read_page_payloads(reader_side.pool, HEAP, 3) == (b"shared",)
