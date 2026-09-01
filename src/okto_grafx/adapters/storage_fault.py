"""The deterministic fault injection bench (SPEC-M1 FR-16, AC-4, AC-10, AC-11, TS-4, TS-5).

This device wraps any StorageDevice and turns the failure modes a real disk only shows once a
year into an ordinary, repeatable test fixture. Given the same seed and the same plan it makes
exactly the same decisions, so a failing scenario is replayed rather than hunted.

What it can do to the engine:

* **crash at any call**, before or after the effect reaches the device;
* **write partially**, storing only the first bytes of an append and then refusing it with
  GrafxDeviceFull, or tearing a page in half the way a power loss does;
* **reorder**, so that a crash keeps a seeded subset of what was written since the last barrier;
* **fill up**, raising GrafxDeviceFull at a chosen call;
* **lie on fsync**, acknowledging a barrier that never reached stable storage and dropping
  everything written to that file since the previous honest barrier when the next crash fires;
* **corrupt the interior of a log**, writing a page sized run of zeros between valid records,
  which is the documented signature of the reference engine.

The twin implements the PORT, so it speaks port semantics (A18):

* a barrier that names one file pins only that file; a barrier that names none pins everything;
* an honest barrier flushes the reorder buffer, because real hardware flushes its write cache
  on fsync -- a device that ignores a barrier is lying, not reordering;
* reordering is read-coherent: every write applies immediately and the seeded permutation only
  decides which of them survive a crash;
* a partial append still raises GrafxDeviceFull, because a short write belongs below the port;
* the trail records the real outcome of every call, failures included;
* atomic_replace, remove and recycle are tracked write points with an undo;
* the crash path always raises SimulatedCrash, whatever the rollback ran into.

``SimulatedCrash`` derives from ``BaseException`` on purpose. A crash is not a database error
and must not be absorbed by the ``except Exception`` of a retry loop, a context manager or a
metrics decorator: it has to unwind the whole stack exactly as a process death would, otherwise
the test would prove nothing about recovery.

Cost note: tracking a namespace operation copies the affected files, and inject_interior_zeros
rewrites the tail of the file it punches, so both are linear in the size of those files. That is
deliberate for a bench that runs against small, deterministic workloads.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass, replace
from types import TracebackType
from typing import TypeVar

from okto_grafx.adapters.storage_local import barrier_failure_from, refuse_missing_file
from okto_grafx.domain.errors import (
    GrafxDeviceFull,
    GrafxDurabilityBarrierFailed,
    GrafxError,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.ids import PageIndex
from okto_grafx.domain.ports.storage import StorageDevice

__all__ = [
    "PORT_METHODS",
    "WRITE_POINT_METHODS",
    "SimulatedCrash",
    "CallRecord",
    "WritePoint",
    "FaultPlan",
    "FaultInjectingStorageDevice",
]

PORT_METHODS: frozenset[str] = frozenset(
    {
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
)
"""Every method of the storage port that the trail records. Properties are not calls."""

WRITE_POINT_METHODS: frozenset[str] = frozenset(
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
"""The calls that change what survives a power loss; a crash bench walks exactly these."""

_MOMENTS: frozenset[str] = frozenset({"before", "after"})
"""When a crash fires relative to the effect of the call it interrupts."""

_T = TypeVar("_T")


class SimulatedCrash(BaseException):
    """Process death simulated by the fault bench; it derives from BaseException on purpose."""

    def __init__(self, message: str, *, sequence: int, method: str, file: str | None, moment: str) -> None:
        """Record where the bench cut the process off, so a report can name the exact call."""
        super().__init__(message)
        self.message: str = message
        self.sequence: int = sequence
        self.method: str = method
        self.file: str | None = file
        self.moment: str = moment


@dataclass(frozen=True, slots=True)
class CallRecord:
    """One entry of the call trail: what was asked, of which file, and how it really ended."""

    sequence: int
    method: str
    file: str | None
    args_summary: str
    outcome: str = "ok"


@dataclass(frozen=True, slots=True)
class WritePoint:
    """One place where a workload changed the device, addressable by a later crash run."""

    call_index: int
    method: str
    file: str | None
    args_summary: str


@dataclass(frozen=True, slots=True)
class FaultPlan:
    """What the bench does, stated up front so a scenario is data rather than code.

    A fault is selected either by absolute call index, which the trail of a previous run gives,
    or by the nth call of one method, which survives a change in the surrounding workload.
    """

    crash_call_index: int | None = None
    crash_method: str | None = None
    crash_occurrence: int = 1
    crash_moment: str = "before"
    device_full_call_index: int | None = None
    device_full_method: str | None = None
    device_full_occurrence: int = 1
    partial_write_call_index: int | None = None
    partial_write_method: str | None = None
    partial_write_occurrence: int = 1
    partial_write_bytes: int = 0
    lying_barrier: bool = False
    reorder_from_call_index: int | None = None
    reorder_flush_call_index: int | None = None

    def __post_init__(self) -> None:
        """Refuse a plan the bench could not honour, naming the field that is wrong."""
        for field, value in (
            ("crash_call_index", self.crash_call_index),
            ("device_full_call_index", self.device_full_call_index),
            ("partial_write_call_index", self.partial_write_call_index),
            ("reorder_from_call_index", self.reorder_from_call_index),
            ("reorder_flush_call_index", self.reorder_flush_call_index),
        ):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
                raise _refuse(field, value, "a call index starts at one.")
        for field, value in (
            ("crash_occurrence", self.crash_occurrence),
            ("device_full_occurrence", self.device_full_occurrence),
            ("partial_write_occurrence", self.partial_write_occurrence),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise _refuse(field, value, "an occurrence starts at one.")
        for field, value in (
            ("crash_method", self.crash_method),
            ("device_full_method", self.device_full_method),
            ("partial_write_method", self.partial_write_method),
        ):
            if value is not None and value not in PORT_METHODS:
                raise _refuse(field, value, "a method of the storage port is required.")
        if self.crash_moment not in _MOMENTS:
            raise _refuse("crash_moment", self.crash_moment, "either 'before' or 'after' is required.")
        if (
            isinstance(self.partial_write_bytes, bool)
            or not isinstance(self.partial_write_bytes, int)
            or self.partial_write_bytes < 0
        ):
            raise _refuse("partial_write_bytes", self.partial_write_bytes, "a byte count of zero or more is required.")


@dataclass(frozen=True, slots=True)
class _VolatileWrite:
    """One change the device acknowledged but has not proved durable for its file.

    ``kind`` says how to take it back: an append shrinks to the size it had, a page write puts
    the previous image back, a truncation appends the tail it removed, and a namespace change
    restores the whole state of the file it touched.
    """

    kind: str
    file: str
    size_before: int = 0
    page_index: int = 0
    image: bytes = b""
    existed: bool = False
    reordered: bool = False


class _SplitMix64:
    """Seeded 64-bit mixer: the bench never touches the global random state or the clock."""

    _MASK: int = (1 << 64) - 1

    def __init__(self, seed: int) -> None:
        """Start the sequence at the given seed."""
        self._state = seed & self._MASK

    def next_word(self) -> int:
        """Return the next 64-bit word of the sequence."""
        self._state = (self._state + 0x9E3779B97F4A7C15) & self._MASK
        word = self._state
        word = ((word ^ (word >> 30)) * 0xBF58476D1CE4E5B9) & self._MASK
        word = ((word ^ (word >> 27)) * 0x94D049BB133111EB) & self._MASK
        return word ^ (word >> 31)

    def below(self, bound: int) -> int:
        """Return a value in the half-open range from zero to bound."""
        return self.next_word() % bound if bound > 0 else 0


class FaultInjectingStorageDevice:
    """StorageDevice that wraps another one and injects the failures FR-16 asks for."""

    def __init__(self, inner: StorageDevice, *, seed: int = 0, plan: FaultPlan | None = None) -> None:
        """Wrap a device; the seed drives every choice the bench makes on its own."""
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise _refuse("seed", seed, "an integer seed is required.")
        self._inner = inner
        self._seed = seed
        self._plan = plan if plan is not None else FaultPlan()
        self._random = _SplitMix64(seed)
        self._sequence = 0
        self._trail: list[CallRecord] = []
        self._occurrences: dict[str, int] = {}
        self._volatile: list[_VolatileWrite] = []
        self._reordering = False
        self._rollback_failures: list[str] = []

    # --- identity -----------------------------------------------------------------------

    @property
    def name(self) -> str:
        """Bounded label of this device family, safe to use as a metric label value (TR-7)."""
        return "fault"

    @property
    def page_size(self) -> int:
        """Size in bytes of every page the wrapped device reads and writes."""
        return self._inner.page_size

    @property
    def inner(self) -> StorageDevice:
        """The wrapped device, so a test can inspect what really reached storage."""
        return self._inner

    @property
    def seed(self) -> int:
        """The seed every decision of this bench derives from."""
        return self._seed

    @property
    def plan(self) -> FaultPlan:
        """The plan currently armed."""
        return self._plan

    # --- bench control ------------------------------------------------------------------

    def arm(self, plan: FaultPlan) -> None:
        """Install a whole plan, replacing the one in force."""
        if not isinstance(plan, FaultPlan):
            raise _refuse("plan", plan, "a FaultPlan is required.")
        self._plan = plan

    def disarm(self) -> None:
        """Drop every armed fault; the device becomes a transparent recorder."""
        self._plan = FaultPlan()

    def crash_at(self, call_index: int, *, moment: str = "before") -> None:
        """Crash at the given call of the trail, before or after its effect."""
        self.arm(replace(self._plan, crash_call_index=call_index, crash_moment=moment))

    def crash_on(self, method: str, occurrence: int = 1, *, moment: str = "before") -> None:
        """Crash at the nth call of one port method, before or after its effect."""
        self.arm(
            replace(
                self._plan,
                crash_method=method,
                crash_occurrence=occurrence,
                crash_moment=moment,
            )
        )

    def fill_device_at(self, call_index: int) -> None:
        """Raise GrafxDeviceFull at the given call of the trail."""
        self.arm(replace(self._plan, device_full_call_index=call_index))

    def fill_device_on(self, method: str, occurrence: int = 1) -> None:
        """Raise GrafxDeviceFull at the nth call of one port method."""
        self.arm(replace(self._plan, device_full_method=method, device_full_occurrence=occurrence))

    def write_partially(
        self,
        stored_bytes: int,
        *,
        call_index: int | None = None,
        method: str | None = None,
        occurrence: int = 1,
    ) -> None:
        """Store only the first bytes of one append or page write.

        An append that stores a prefix raises GrafxDeviceFull, as the port demands, and leaves
        the fragment behind for recovery to find. A page write tears the page and returns: that
        is what a power loss in the middle of a sector does, and catching it is the job of the
        page checksum.
        """
        self.arm(
            replace(
                self._plan,
                partial_write_call_index=call_index,
                partial_write_method=method,
                partial_write_occurrence=occurrence,
                partial_write_bytes=stored_bytes,
            )
        )

    def lie_on_barrier(self, *, enabled: bool = True) -> None:
        """Make durable_barrier acknowledge without flushing, and start tracking what is at risk."""
        self.arm(replace(self._plan, lying_barrier=enabled))

    def start_reordering(self) -> None:
        """Let writes apply immediately while a crash keeps only a seeded part of them."""
        self._reordering = True

    def flush_reordered(self) -> int:
        """Pin everything the reorder window still held and return how many writes were pinned."""
        pinned = sum(1 for item in self._volatile if item.reordered)
        self._volatile = [item for item in self._volatile if not item.reordered]
        self._reordering = False
        return pinned

    def pending_writes(self) -> tuple[str, ...]:
        """Return a short description of every write the reorder window still holds."""
        return tuple(f"{item.kind}:{item.file}" for item in self._volatile if item.reordered)

    def volatile_files(self) -> tuple[str, ...]:
        """Return every file that has a write no honest barrier has pinned yet, sorted."""
        return tuple(sorted({item.file for item in self._volatile}))

    def rollback_failures(self) -> tuple[str, ...]:
        """Return what the rollback of a crash could not undo; it never stops the crash (A18g)."""
        return tuple(self._rollback_failures)

    def inject_interior_zeros(self, file: str, offset: int, length: int | None = None) -> int:
        """Overwrite a run of bytes inside a file with zeros, keeping the records after it.

        This is the documented signature of the reference engine: a page sized hole in the
        middle of a log whose tail still decodes. The hole is punched with the append-only
        primitives of the port, so the file keeps its size and the bytes after the hole stay
        exactly where they were (TS-5). The tail is rewritten, so the cost is linear in the size
        of the file.
        """
        run = self._inner.page_size if length is None else length
        if isinstance(run, bool) or not isinstance(run, int) or run <= 0:
            raise _refuse("length", length, "a positive run of bytes is required.")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise _refuse("offset", offset, "an offset of zero or more is required.")
        size = self._inner.log_size(file)
        if offset + run > size:
            raise GrafxUnsupportedOperation(
                f"A zero run of {run} bytes at offset {offset} would pass the end of {file!r}.",
                reason="run_past_end",
                file=file,
                offset=offset,
                length=run,
                size=size,
            )
        tail = self._inner.read_log(file, offset + run, size - offset - run)
        self._inner.truncate_log(file, offset)
        self._inner.append_log(file, bytes(run) + tail)
        return run

    # --- trail --------------------------------------------------------------------------

    def trail(self) -> tuple[CallRecord, ...]:
        """Return every port call this device has served, in order, with its real outcome."""
        return tuple(self._trail)

    def methods(self) -> tuple[str, ...]:
        """Return only the method names of the trail, which is what an ordering assertion reads."""
        return tuple(record.method for record in self._trail)

    def calls_of(self, method: str) -> tuple[CallRecord, ...]:
        """Return every trail entry of one method, in order."""
        return tuple(record for record in self._trail if record.method == method)

    def clear_trail(self) -> None:
        """Forget the trail and restart the call numbering at one."""
        self._trail.clear()
        self._occurrences.clear()
        self._sequence = 0

    def enumerate_write_points(
        self,
        workload: Callable[[FaultInjectingStorageDevice], None],
        *,
        methods: Collection[str] | None = None,
    ) -> tuple[WritePoint, ...]:
        """Run a workload once with no fault armed and return every write point it produced.

        The workload really executes against the wrapped device: no fault is armed, no write is
        held back and no volatile window is kept, so what the survey observes is exactly what an
        undisturbed run does. A crash run has to start from a fresh device. This is the
        enumeration TS-4 needs: learn the points once, then replay crashing at each of them.
        """
        previous_plan = self._plan
        previous_reordering = self._reordering
        previous_volatile = list(self._volatile)
        self._plan = FaultPlan()
        self._volatile = []
        self.clear_trail()
        try:
            workload(self)
        finally:
            self._plan = previous_plan
            self._reordering = previous_reordering
            self._volatile = previous_volatile
        selected = WRITE_POINT_METHODS if methods is None else frozenset(methods)
        return tuple(
            WritePoint(record.sequence, record.method, record.file, record.args_summary)
            for record in self._trail
            if record.method in selected
        )

    # --- namespace ----------------------------------------------------------------------

    def exists(self, file: str) -> bool:
        """Return True when the wrapped device holds the named file."""
        sequence = self._enter("exists", file, "")
        self._before(sequence, "exists", file)
        answer = self._run(sequence, lambda: self._inner.exists(file))
        self._after(sequence, "exists", file)
        return answer

    def create(self, file: str, *, exclusive: bool = True) -> None:
        """Create the named file on the wrapped device."""
        sequence = self._enter("create", file, f"exclusive={exclusive}")
        self._before(sequence, "create", file)
        self._capture_state(file)
        self._run(sequence, lambda: self._inner.create(file, exclusive=exclusive))
        self._after(sequence, "create", file)

    def remove(self, file: str) -> None:
        """Remove the named file from the wrapped device."""
        sequence = self._enter("remove", file, "")
        self._before(sequence, "remove", file)
        self._capture_state(file)
        self._run(sequence, lambda: self._inner.remove(file))
        self._after(sequence, "remove", file)

    def list_files(self, prefix: str = "") -> tuple[str, ...]:
        """Return every name of the wrapped device that starts with the prefix."""
        sequence = self._enter("list_files", None, f"prefix={prefix!r}")
        self._before(sequence, "list_files", None)
        answer = self._run(sequence, lambda: self._inner.list_files(prefix))
        self._after(sequence, "list_files", None)
        return answer

    def file_size(self, file: str) -> int:
        """Return the size of the named file."""
        sequence = self._enter("file_size", file, "")
        self._before(sequence, "file_size", file)
        answer = self._run(sequence, lambda: self._inner.file_size(file))
        self._after(sequence, "file_size", file)
        return answer

    def atomic_replace(self, source: str, target: str) -> None:
        """Move source onto target on the wrapped device."""
        sequence = self._enter("atomic_replace", source, f"target={target!r}")
        self._before(sequence, "atomic_replace", source)
        self._capture_state(source)
        self._capture_state(target)
        self._run(sequence, lambda: self._inner.atomic_replace(source, target))
        self._after(sequence, "atomic_replace", source)

    def recycle(self, file: str) -> bool:
        """Release the named file on the wrapped device."""
        sequence = self._enter("recycle", file, "")
        self._before(sequence, "recycle", file)
        self._capture_state(file)
        answer = self._run(sequence, lambda: self._inner.recycle(file))
        self._after(sequence, "recycle", file)
        return answer

    # --- paged space --------------------------------------------------------------------

    def page_count(self, file: str) -> int:
        """Return how many pages the wrapped device holds for the named file."""
        sequence = self._enter("page_count", file, "")
        self._before(sequence, "page_count", file)
        answer = self._run(sequence, lambda: self._inner.page_count(file))
        self._after(sequence, "page_count", file)
        return answer

    def allocate(self, file: str, count: int = 1) -> PageIndex:
        """Grow the named file by count pages on the wrapped device."""
        sequence = self._enter("allocate", file, f"count={count}")
        self._before(sequence, "allocate", file)
        size_before = self._size_of(file)
        first = self._run(sequence, lambda: self._inner.allocate(file, count))
        self._record(_VolatileWrite("append", file, size_before=size_before, reordered=self._reordering))
        self._after(sequence, "allocate", file)
        return first

    def read_page(self, file: str, page_index: PageIndex) -> bytes:
        """Return one page from the wrapped device."""
        sequence = self._enter("read_page", file, f"page={page_index}")
        self._before(sequence, "read_page", file)
        answer = self._run(sequence, lambda: self._inner.read_page(file, page_index))
        self._after(sequence, "read_page", file)
        return answer

    def write_page(self, file: str, page_index: PageIndex, data: bytes) -> None:
        """Write one page, possibly tearing it the way a power loss does."""
        sequence = self._enter(
            "write_page", file, f"page={page_index},bytes={_payload_length(data)}"
        )
        self._before(sequence, "write_page", file)
        stored = self._partial_limit(sequence, "write_page")
        if stored is not None and _payload_length(data) == "?":
            # A payload with no length is the wrapped device's business, not the bench's.
            stored = None
        self._capture_page(file, page_index)
        if stored is None:
            self._run(sequence, lambda: self._inner.write_page(file, page_index, data))
        else:
            # The read that builds the torn image is part of serving this call, so a failure of
            # that read is the real outcome of the call and must replace the injected label.
            previous = self._run(sequence, lambda: self._inner.read_page(file, page_index))
            torn = bytes(memoryview(data)[:stored]) + previous[stored:]
            self._run(sequence, lambda: self._inner.write_page(file, page_index, torn))
        self._after(sequence, "write_page", file)

    # --- append-only log space ----------------------------------------------------------

    def append_log(self, file: str, payload: bytes) -> int:
        """Append to a log; a partial append stores its prefix and then refuses the write."""
        sequence = self._enter("append_log", file, f"bytes={_payload_length(payload)}")
        self._before(sequence, "append_log", file)
        stored = self._partial_limit(sequence, "append_log")
        if stored is not None and _payload_length(payload) == "?":
            # Let the wrapped device answer for a payload that is not bytes at all: the bench
            # never turns a typed refusal into a bare TypeError of its own.
            stored = None
        size_before = self._size_of(file)
        if stored is None:
            size = self._run(sequence, lambda: self._inner.append_log(file, payload))
            self._record(_VolatileWrite("append", file, size_before=size_before, reordered=self._reordering))
            self._after(sequence, "append_log", file)
            return size
        self._run(sequence, lambda: self._inner.append_log(file, bytes(payload[:stored])))
        self._record(_VolatileWrite("append", file, size_before=size_before, reordered=self._reordering))
        raise GrafxDeviceFull(
            f"The fault bench stored {stored} of {len(payload)} bytes appended to {file!r}.",
            file=file,
            requested=len(payload),
            stored=stored,
            sequence=sequence,
        )

    def read_log(self, file: str, offset: int, length: int) -> bytes:
        """Return one fill-until-EOF byte range from the wrapped device."""
        sequence = self._enter("read_log", file, f"offset={offset},length={length}")
        self._before(sequence, "read_log", file)
        answer = self._run(sequence, lambda: self._inner.read_log(file, offset, length))
        self._after(sequence, "read_log", file)
        return answer

    def log_size(self, file: str) -> int:
        """Return the size of a log on the wrapped device."""
        sequence = self._enter("log_size", file, "")
        self._before(sequence, "log_size", file)
        answer = self._run(sequence, lambda: self._inner.log_size(file))
        self._after(sequence, "log_size", file)
        return answer

    def truncate_log(self, file: str, size: int) -> None:
        """Shrink a log on the wrapped device.

        The undo image is read before the call and kept only once the call succeeded, so an
        argument the wrapped device refuses is answered by the device itself, with its own type
        and its own trail entry, whether or not a fault is armed.
        """
        sequence = self._enter("truncate_log", file, f"size={size}")
        self._before(sequence, "truncate_log", file)
        removed = self._capture_truncation(file, size)
        self._run(sequence, lambda: self._inner.truncate_log(file, size))
        if removed is not None:
            self._record(
                _VolatileWrite(
                    "truncate", file, size_before=int(size), image=removed, reordered=self._reordering
                )
            )
        self._after(sequence, "truncate_log", file)

    # --- durability ---------------------------------------------------------------------

    def durable_barrier(self, file: str | None = None) -> None:
        """Flush the wrapped device, or acknowledge without flushing when the plan says to lie.

        An honest barrier pins exactly what it names: with a file, only that file leaves the
        volatile window; with none, everything does. It also flushes the reorder window for the
        same files, because a real device empties its write cache when it is told to (A18a, b).
        """
        sequence = self._enter("durable_barrier", file, "")
        self._before(sequence, "durable_barrier", file)
        try:
            self._serve_barrier(sequence, file)
        except GrafxDurabilityBarrierFailed:
            raise
        except GrafxError as failure:
            # A28: one type leaves this door whatever went wrong, lying or honest, because the
            # caller counts exactly that type into oktografx_barrier_failures_total.
            self._mark(sequence, "durability_barrier_failed")
            raise barrier_failure_from(failure) from failure
        self._after(sequence, "durable_barrier", file)

    def _serve_barrier(self, sequence: int, file: str | None) -> None:
        """Flush the wrapped device, or acknowledge without flushing when the plan says to lie."""
        if self._plan.lying_barrier:
            # The lie is about persistence only. Everything else the door would have refused it
            # still refuses: a device that cannot serve at all, and a file it does not hold. The
            # failure itself is raised in the canonical shape of the wrapped device, so the
            # conversion above gives it the very details an honest barrier would carry (H6, H7).
            if file is None:
                self._run(sequence, lambda: self._inner.list_files())
            elif not self._run(sequence, lambda: self._inner.exists(file)):
                raise refuse_missing_file(file, "open")
            # The caller is told the bytes are safe. They stay in the volatile window instead,
            # and the next crash makes them disappear.
            return
        self._run(sequence, lambda: self._inner.durable_barrier(file))
        self._pin(file)

    # --- lifecycle ----------------------------------------------------------------------

    def invalidate_descriptor_identity(self, file: str | None = None) -> None:
        """Forward the optional cache-only identity invalidation outside the fault trail."""
        invalidate = getattr(self._inner, "invalidate_descriptor_identity", None)
        if callable(invalidate):
            invalidate(file)

    def close(self) -> None:
        """Close the wrapped device when it has something to close."""
        closer = getattr(self._inner, "close", None)
        if callable(closer):
            closer()

    def __enter__(self) -> FaultInjectingStorageDevice:
        """Return the device itself so it can be used as a context manager."""
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the wrapped device when the block ends, successfully or not."""
        self.close()

    # --- internals ----------------------------------------------------------------------

    def _enter(self, method: str, file: str | None, summary: str) -> int:
        """Record one port call and return its sequence number."""
        self._sequence += 1
        self._occurrences[method] = self._occurrences.get(method, 0) + 1
        self._trail.append(CallRecord(self._sequence, method, file, summary))
        return self._sequence

    def _mark(self, sequence: int, outcome: str) -> None:
        """Note how one recorded call ended."""
        position = sequence - 1
        if 0 <= position < len(self._trail):
            self._trail[position] = replace(self._trail[position], outcome=outcome)

    def _run(self, sequence: int, action: Callable[[], _T]) -> _T:
        """Forward one call to the wrapped device, recording the real outcome either way (A18e)."""
        try:
            return action()
        except GrafxError as failure:
            self._mark(sequence, failure.code)
            raise

    def _tracking(self) -> bool:
        """Return True while a crash would take some of the acknowledged writes back."""
        return self._plan.lying_barrier or self._reordering

    def _record(self, item: _VolatileWrite) -> None:
        """Keep one change in the volatile window, but only while a crash could undo it."""
        if self._tracking():
            self._volatile.append(item)

    def _pin(self, file: str | None) -> None:
        """Drop from the volatile window everything an honest barrier just made durable."""
        if file is None:
            self._volatile.clear()
            return
        self._volatile = [item for item in self._volatile if item.file != file]

    def _size_of(self, file: str) -> int:
        """Return the current size of a file, or zero when the device does not hold it yet."""
        try:
            return self._inner.log_size(file)
        except GrafxError:
            return 0

    def _capture_page(self, file: str, page_index: PageIndex) -> None:
        """Keep the current image of a page so a crash can put it back.

        A page the wrapped device refuses to read is simply not tracked: the write that follows
        raises the canonical failure of the device, which is what the caller must observe.
        """
        if not self._tracking():
            return
        try:
            image = self._inner.read_page(file, page_index)
        except GrafxError:
            return
        self._volatile.append(
            _VolatileWrite("page", file, page_index=page_index, image=image, reordered=self._reordering)
        )

    def _capture_truncation(self, file: str, size: object) -> bytes | None:
        """Return the bytes a truncation would remove, or None when it cannot be undone.

        Nothing here decides whether the truncation is legal: a size the device would refuse is
        simply not captured, and the device answers with its own typed failure.
        """
        if not self._tracking():
            return None
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            return None
        try:
            current = self._inner.log_size(file)
            return self._inner.read_log(file, size, current - size) if current > size else b""
        except GrafxError:
            return None

    def _capture_state(self, file: str) -> None:
        """Keep the whole state of one file so a namespace change can be undone (A18f)."""
        if not self._tracking():
            return
        try:
            existed = self._inner.exists(file)
            content = self._inner.read_log(file, 0, self._inner.log_size(file)) if existed else b""
        except GrafxError:
            return
        self._volatile.append(_VolatileWrite("state", file, image=content, existed=existed, reordered=self._reordering))

    def _selects(self, index: int | None, method: str | None, occurrence: int, sequence: int, name: str) -> bool:
        """Return True when a fault selector points at the call being served."""
        if index is not None and index == sequence:
            return True
        return method is not None and method == name and self._occurrences.get(name, 0) == occurrence

    def _before(self, sequence: int, method: str, file: str | None) -> None:
        """Apply everything the plan asks for before the effect of a call reaches the device."""
        plan = self._plan
        if plan.reorder_from_call_index is not None and plan.reorder_from_call_index == sequence:
            self._reordering = True
        if plan.reorder_flush_call_index is not None and plan.reorder_flush_call_index == sequence:
            self.flush_reordered()
        if self._selects(
            plan.device_full_call_index, plan.device_full_method, plan.device_full_occurrence, sequence, method
        ):
            self._mark(sequence, "device_full")
            raise GrafxDeviceFull(
                f"The fault bench refused {method} on {file!r} at call {sequence}.",
                file=file,
                operation=method,
                sequence=sequence,
            )
        if plan.crash_moment == "before" and self._selects(
            plan.crash_call_index, plan.crash_method, plan.crash_occurrence, sequence, method
        ):
            self._crash(sequence, method, file, "before")

    def _after(self, sequence: int, method: str, file: str | None) -> None:
        """Apply the crash the plan asks for once the effect of a call has reached the device."""
        plan = self._plan
        if plan.crash_moment == "after" and self._selects(
            plan.crash_call_index, plan.crash_method, plan.crash_occurrence, sequence, method
        ):
            self._crash(sequence, method, file, "after")

    def _crash(self, sequence: int, method: str, file: str | None, moment: str) -> None:
        """Take back what was never durable and unwind the stack like a process death.

        The rollback can meet a device that refuses it, and that must never turn a crash into an
        ordinary exception a retry loop could swallow (A18g): whatever happens, this raises
        SimulatedCrash, and what could not be undone is reported by rollback_failures().
        """
        self._mark(sequence, f"crash_{moment}")
        try:
            self._discard_volatile()
        except BaseException as failure:  # pragma: no cover - defensive, reported not raised
            self._rollback_failures.append(f"{type(failure).__name__}: {failure}")
        raise SimulatedCrash(
            f"The fault bench stopped the process {moment} {method} on {file!r} at call {sequence}.",
            sequence=sequence,
            method=method,
            file=file,
            moment=moment,
        )

    def _discard_volatile(self) -> None:
        """Undo the writes a crash takes back, newest first.

        A lying barrier loses everything still in the window. A reordering device loses only a
        seeded tail per file, because the writes that were still in its cache never reached the
        platter while the earlier ones did.
        """
        if not self._volatile:
            return
        doomed = (
            {id(item) for item in self._volatile}
            if self._plan.lying_barrier
            else self._reorder_losses()
        )
        for item in reversed([item for item in self._volatile if id(item) in doomed]):
            try:
                self._undo(item)
            except GrafxError as failure:
                self._rollback_failures.append(f"{item.kind}:{item.file}: {failure.code}")
        self._volatile = [item for item in self._volatile if id(item) not in doomed]

    def _reorder_losses(self) -> set[int]:
        """Return the identity of every write in the seeded tail a reordering crash loses."""
        by_file: dict[str, list[_VolatileWrite]] = {}
        for item in self._volatile:
            if item.reordered:
                by_file.setdefault(item.file, []).append(item)
        doomed: set[int] = set()
        for file in sorted(by_file):
            entries = by_file[file]
            keep = self._random.below(len(entries) + 1)
            doomed.update(id(item) for item in entries[keep:])
        return doomed

    def _undo(self, item: _VolatileWrite) -> None:
        """Take one acknowledged change back off the wrapped device."""
        if item.kind == "append":
            self._inner.truncate_log(item.file, item.size_before)
        elif item.kind == "page":
            self._inner.write_page(item.file, item.page_index, item.image)
        elif item.kind == "truncate":
            self._inner.append_log(item.file, item.image)
        elif item.kind == "state":
            self._restore_state(item)

    def _restore_state(self, item: _VolatileWrite) -> None:
        """Put one file back exactly as it was before a namespace change touched it."""
        if not item.existed:
            if self._inner.exists(item.file):
                self._inner.remove(item.file)
            return
        if not self._inner.exists(item.file):
            self._inner.create(item.file)
        self._inner.truncate_log(item.file, 0)
        if item.image:
            self._inner.append_log(item.file, item.image)

    def _partial_limit(self, sequence: int, method: str) -> int | None:
        """Return how many bytes of this write actually reach the device, or None for all of them."""
        plan = self._plan
        if not self._selects(
            plan.partial_write_call_index,
            plan.partial_write_method,
            plan.partial_write_occurrence,
            sequence,
            method,
        ):
            return None
        self._mark(sequence, "partial_write")
        return plan.partial_write_bytes


def _payload_length(payload: object) -> object:
    """Return the length of a payload for the trail, or a marker when it has none.

    The trail is written before the wrapped device sees the call, so a payload that is not a
    byte sequence must not make the bench raise a bare TypeError of its own: the device answers
    with its own typed refusal, and the trail keeps the entry that refusal belongs to.
    """
    try:
        return len(payload)  # type: ignore[arg-type]
    except TypeError:
        return "?"


def _refuse(field: str, value: object, reason: str) -> GrafxUnsupportedOperation:
    """Build the refusal raised when the bench is asked for something it cannot honour."""
    return GrafxUnsupportedOperation(
        f"Invalid fault bench setting for {field!r}: {reason} Got {value!r}.",
        reason="invalid_fault_plan",
        field=field,
        value=value,
    )
