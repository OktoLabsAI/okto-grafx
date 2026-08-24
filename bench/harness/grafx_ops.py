"""The three D5 operations, driven through the real Okto Grafx engine on the shipped adapters.

What is measured, exactly
-------------------------
``durable_commit``
    One write transaction through ``TransactionManager.commit``: epoch validation, the exclusive
    commit section, OCC validation, ``WalManager.append_many`` of the WRITE_PAGE and COMMIT
    records, ``WalManager.barrier`` (the real ``fsync``; BR-4 forbids acknowledging before it
    returns), the page-image application and the publication of ``control/commit.state`` by
    ``atomic_replace``. Nothing is stubbed: ``LocalStorageDevice``, ``LocalProcessCoordinator``
    and ``SystemClock`` are the adapters that ship.

``point_read``
    One ``HeapStore.read(ref)`` of a record already in the buffer pool: the warm read path of a
    single row by reference. **Disclosed bias:** the reference engine is asked for a row by its
    PRIMARY KEY, which costs it a parse, a plan and an index probe that this path does not pay,
    because Okto Grafx has no query surface yet (C10/C11 are later waves) and no index is wired
    into the measured path. The ratio therefore FAVOURS Okto Grafx and must be re-taken once the
    public API exists. It is reported with that caveat rather than presented as final.

``open_replay``
    A fresh ``WalManager`` over a log holding a known number of records: ``open()`` -- which
    discovers and indexes the segments -- followed by a full ``scan_all()`` that decodes and
    CHECKSUMS every record. That is the replay-side cost of opening a database after a crash.
    **Disclosed gap:** the recovery manager (C6) does not exist in the tree yet, so nothing is
    re-applied to the heap and no ledger entry is written. This is a LOWER BOUND on
    open-with-replay, not the whole of it, and the artefact says so.

Every operation asserts that it did what it claims -- a commit reports ``wrote=True``, a replay
returns the record count that was written -- so a measurement of an operation that quietly did
nothing cannot be reported as a fast one (A72).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from okto_grafx.adapters.clock_system import SystemClock
from okto_grafx.adapters.codec_v1 import PageCodecV1
from okto_grafx.adapters.coordination_local import LocalProcessCoordinator
from okto_grafx.adapters.metrics_noop import NoOpMetricsSink
from okto_grafx.adapters.storage_local import LocalStorageDevice
from okto_grafx.domain.model.schema import ColumnDef, TableDef, ValueType
from okto_grafx.domain.page import Page
from okto_grafx.domain.page.layout import PageType
from okto_grafx.domain.wal.record import WalRecord, WalRecordType
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.catalog_store import CatalogStore
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.txn_manager import TransactionManager
from okto_grafx.engine.wal_manager import WalManager

from bench.harness.measure import Measurement, measure

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_PARTITIONS_PER_TABLE",
    "GrafxStack",
    "build_stack",
    "measure_durable_commit",
    "measure_open_with_replay",
    "measure_point_read",
]

DEFAULT_PAGE_SIZE: int = 8192
"""The page size the contract fixes for a bench database (CONTRACT section 6.3)."""

DEFAULT_PARTITIONS_PER_TABLE: int = 64
"""The default this calibration exists to freeze (CONTRACT section 5, SPEC-M1 FR-15)."""

SEGMENT_BYTES: int = 4 * 1024 * 1024
"""Segment size of the bench log: big enough that a run stays inside a handful of segments."""

BUDGET_BYTES: int = 8 * 1024 * 1024
"""Buffer budget of the bench database, chosen so the point-read corpus stays resident."""

HEAP_FILE: str = "heap.dat"
"""The data file a staged page image is written to."""


@dataclass(frozen=True, slots=True)
class GrafxStack:
    """One assembled Okto Grafx database: the real engine over the real adapters."""

    root: Path
    """The directory the database lives in."""

    device: LocalStorageDevice
    """The shipped local storage adapter (C2)."""

    codec: PageCodecV1
    """The page codec (C1)."""

    pool: BufferPool
    """The buffer pool (C1)."""

    catalog: CatalogStore
    """The catalog store (C1)."""

    heap: HeapStore
    """The heap store (C1)."""

    coordinator: LocalProcessCoordinator
    """The process coordinator (C3)."""

    wal: WalManager
    """The write-ahead log manager (C4)."""

    manager: TransactionManager
    """The transaction manager (C5), which owns the commit protocol."""

    table: TableDef
    """A two-column node table, registered in the catalog, for the heap operations."""

    def close(self) -> None:
        """Release the lease and the reader registration, as a real caller would."""
        self.manager.close()


def build_stack(
    root: Path,
    *,
    partitions_per_table: int = DEFAULT_PARTITIONS_PER_TABLE,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> GrafxStack:
    """Assemble a working database in ``root`` from the shipped adapters and the real engine."""
    root.mkdir(parents=True, exist_ok=True)
    device = LocalStorageDevice(root, page_size=page_size)
    clock = SystemClock()
    metrics = NoOpMetricsSink()
    codec = PageCodecV1(page_size=device.page_size)
    pool = BufferPool(device, codec, metrics, budget_bytes=BUDGET_BYTES, db_label="bench")
    catalog = CatalogStore(pool)
    catalog.bootstrap()
    heap = HeapStore(pool, catalog)
    heap.bootstrap()
    coordinator = LocalProcessCoordinator(
        device,
        clock,
        metrics=metrics,
        owner_id="bench-harness",
        ttl_seconds=30.0,
        owner_stall_threshold=30.0,
        reader_stall_threshold=60.0,
        section_timeout=30.0,
        lock_directory=str(root / "control"),
    )
    descriptor = f"hash-v1;partitions_per_table={partitions_per_table}"
    wal = WalManager(
        device, clock, metrics, segment_bytes=SEGMENT_BYTES, descriptor=descriptor
    )
    wal.open()
    manager = TransactionManager(
        wal,
        pool,
        heap,
        catalog,
        coordinator,
        clock,
        metrics,
        None,
        partitions_per_table=partitions_per_table,
        commit_lock_timeout=30.0,
        descriptor=descriptor,
    )
    table = TableDef(
        table_id=catalog.catalog.next_table_id(),
        name="Bench",
        kind="node",
        columns=(
            ColumnDef(name="id", type=ValueType.INT64, nullable=False),
            ColumnDef(name="payload", type=ValueType.STRING),
        ),
        primary_key="id",
    )
    catalog.catalog.add_table(table)
    catalog.save()
    return GrafxStack(
        root=root,
        device=device,
        codec=codec,
        pool=pool,
        catalog=catalog,
        heap=heap,
        coordinator=coordinator,
        wal=wal,
        manager=manager,
        table=table,
    )


def _try_build(root: Path, name: str) -> GrafxStack | Measurement:
    """Assemble the stack, or return an UNMEASURED measurement naming why it could not be.

    The tree this harness measures is written by ten other components while it runs. When one of
    them cannot even be CONSTRUCTED, the honest result is that this ceiling was not measured, with
    the reason attached -- not a dead calibration with no artefact and no numbers for the ceilings
    that were still measurable (A75.2). The failure is deliberately reported verbatim, including
    the exception type, because a builder reading it needs the defect and not a paraphrase.
    """
    try:
        return build_stack(root)
    except Exception as error:  # noqa: BLE001 - the reason IS the result here
        return Measurement(
            name=name,
            samples=(),
            warmup=(),
            unmeasured=(
                f"the engine could not be assembled: {type(error).__name__}: {error}"
            ),
            detail="setup failed before any sample was taken",
        )


def _page_image(codec: PageCodecV1, page_index: int, payload: bytes) -> bytes:
    """Return one valid heap page image holding a single row payload."""
    page = Page(
        int(PageType.HEAP),
        page_size=codec.page_size,
        page_index=page_index,
        page_lsn=0,
        seq=2,
    )
    page.insert_slot(payload)
    return codec.encode_page(page)


def measure_durable_commit(
    root: Path, *, iterations: int, warmup: int, pages: int = 32
) -> Measurement:
    """Time one durable commit of one small row, through the whole commit protocol."""
    built = _try_build(root / "commit", "okto_grafx durable commit")
    if isinstance(built, Measurement):
        return built
    stack = built
    committed = 0

    def one(index: int) -> None:
        nonlocal committed
        payload = b"row-%08d" % index
        page_index = 2 + (index % pages)
        txn = stack.manager.begin("write")
        # This harness intentionally measures the physical commit protocol. Page-image staging is
        # a manager-owned capability (public callers must go through statements/value stores), so
        # the in-tree engine harness uses the same bound private door as QueryEngine.
        txn.owner._stage_page_image(
            txn,
            HEAP_FILE,
            page_index,
            _page_image(stack.codec, page_index, payload),
        )
        txn.note_write(stack.manager.partition_of(stack.table.table_id, payload))
        report = stack.manager.commit(txn)
        if not report.durable or not report.wrote:
            raise AssertionError(
                f"a commit reported durable={report.durable} wrote={report.wrote}; "
                "a bench that measures a commit which wrote nothing measures nothing"
            )
        committed += 1

    try:
        result = measure(
            one,
            name="okto_grafx durable commit",
            iterations=iterations,
            warmup=warmup,
            detail=(
                "TransactionManager.commit: epoch validation, exclusive section, OCC, "
                "append_many(WRITE_PAGE, COMMIT), barrier (real fsync), page image, "
                "atomic_replace of control/commit.state"
                f" | each sample is ONE commit against a database that is NOT reset between "
                f"samples, so kept sample k (0-based) is taken with {warmup} + k commits already "
                f"in the log: the series spans {warmup} to {warmup + iterations - 1} prior "
                "commits, and commit cost today rises with that count"
            ),
        )
    finally:
        stack.close()
    if result.ok and committed != iterations + warmup:
        return Measurement(
            name=result.name,
            samples=(),
            warmup=result.warmup,
            unmeasured=f"only {committed} of {iterations + warmup} commits completed",
            detail=result.detail,
        )
    return result


def measure_point_read(
    root: Path, *, iterations: int, warmup: int, rows: int = 512
) -> Measurement:
    """Time one warm point read of a stored row by reference."""
    built = _try_build(root / "read", "okto_grafx point read")
    if isinstance(built, Measurement):
        return built
    stack = built
    references = [
        stack.heap.insert(stack.table, index, (index, f"payload-{index:08d}"), xmin=1)
        for index in range(rows)
    ]
    stack.pool.flush()
    read_rows = 0

    def one(index: int) -> None:
        nonlocal read_rows
        reference = references[index % rows]
        version = stack.heap.read(reference)
        if version.record_id != index % rows:
            raise AssertionError(
                f"a point read of row {index % rows} returned {version.record_id}"
            )
        read_rows += 1

    try:
        result = measure(
            one,
            name="okto_grafx point read",
            iterations=iterations,
            warmup=warmup,
            detail=(
                f"HeapStore.read of one row by RecordRef over a {rows}-row corpus, buffer pool "
                "warm; no query parse, no plan, no index probe (C10/C11 do not exist yet)"
            ),
        )
    finally:
        stack.close()
    if result.ok and read_rows != iterations + warmup:
        return Measurement(
            name=result.name,
            samples=(),
            warmup=result.warmup,
            unmeasured=f"only {read_rows} of {iterations + warmup} reads completed",
            detail=result.detail,
        )
    return result


def _fill_log(root: Path, records: int, payload_bytes: int) -> int:
    """Write ``records`` WAL records into ``root`` and return the number written."""
    device = LocalStorageDevice(root, page_size=DEFAULT_PAGE_SIZE)
    wal = WalManager(
        device,
        SystemClock(),
        NoOpMetricsSink(),
        segment_bytes=SEGMENT_BYTES,
        descriptor="hash-v1;partitions_per_table=64",
    )
    wal.open()
    payload = bytes(payload_bytes)
    batch = [
        WalRecord(record_type=int(WalRecordType.WRITE_PAGE), payload=payload, txn_id=1)
        for _ in range(records)
    ]
    wal.append_many(batch)
    wal.barrier()
    return records


def measure_open_with_replay(
    root: Path,
    *,
    iterations: int,
    warmup: int,
    records: int = 2000,
    payload_bytes: int = 256,
) -> Measurement:
    """Time opening a log of a known size and reading every record back with its checksum."""
    directory = root / "replay"
    directory.mkdir(parents=True, exist_ok=True)
    written = _fill_log(directory, records, payload_bytes)
    seen = 0

    def one(_index: int) -> None:
        nonlocal seen
        device = LocalStorageDevice(directory, page_size=DEFAULT_PAGE_SIZE)
        wal = WalManager(
            device,
            SystemClock(),
            NoOpMetricsSink(),
            segment_bytes=SEGMENT_BYTES,
            descriptor="hash-v1;partitions_per_table=64",
        )
        wal.open()
        replayed = 0
        for item in wal.scan_all():
            if item.failure is not None:
                raise AssertionError(f"the bench log is damaged: {item.failure}")
            replayed += 1
        if replayed < written:
            raise AssertionError(
                f"a replay saw {replayed} records over a log of {written}; a replay that read "
                "nothing would time as instant"
            )
        seen = replayed

    return measure(
        one,
        name="okto_grafx open with replay",
        iterations=iterations,
        warmup=warmup,
        detail=(
            f"WalManager.open plus a full scan_all over {records} records of {payload_bytes} "
            "bytes, decoding and verifying the CRC-32C of each; the recovery manager (C6) does "
            "not exist yet, so nothing is re-applied and this is a LOWER BOUND"
        ),
    )
