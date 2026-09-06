"""Bounded external merge-sort adapter for query spill records.

Temporary files live in an adapter-owned ``TemporaryDirectory`` and never in the database
namespace.  The engine supplies opaque versioned keys/payloads and their total comparison; this
module supplies only host I/O and a bounded two-way merge.  Run bytes are never deserialised as
Python objects here, and pickle is deliberately absent.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cmp_to_key
from struct import Struct

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxDeviceFull,
    GrafxError,
    GrafxQueryBudgetExceeded,
    GrafxStorageError,
)
from okto_grafx.domain.ports.query_spill import SpillComparator
from okto_grafx.domain.query.memory import LogicalMemoryBudget

__all__ = ["LocalQuerySpillFactory"]

_RUN_MAGIC = b"OGXS\x01"
_LENGTHS = Struct("<QQ")
_RECORD_OVERHEAD = 32


@dataclass(frozen=True, slots=True)
class _Record:
    key: bytes
    payload: bytes
    charge: int


def _storage_failure(action: str, failure: OSError) -> GrafxError:
    """Translate host temporary-I/O failures into the stable Grafx taxonomy."""
    error_number = getattr(failure, "errno", None)
    windows_error = getattr(failure, "winerror", None)
    if getattr(failure, "errno", None) == 28:
        return GrafxDeviceFull(
            f"Temporary query spill could not {action}: the host device is full.",
            field="query_spill",
            action=action,
            errno=error_number,
            winerror=windows_error,
        )
    return GrafxStorageError(
        f"Temporary query spill could not {action}: the host filesystem refused the operation.",
        field="query_spill",
        action=action,
        errno=error_number,
        winerror=windows_error,
    )


class LocalQuerySpillFactory:
    """Create isolated local external-sort workspaces on demand."""

    def __init__(self, directory: str | None = None) -> None:
        self._directory = directory

    def open(self, budget: LogicalMemoryBudget) -> _Workspace:
        """Open one workspace without exposing its random host path to the pure core."""
        if type(budget) is not LogicalMemoryBudget:
            raise GrafxConfigurationError(
                "A query spill workspace needs an exact LogicalMemoryBudget.",
                field="query_spill.budget",
                value=type(budget).__name__,
            )
        try:
            temporary = tempfile.TemporaryDirectory(
                prefix="okto-grafx-query-", dir=self._directory
            )
        except OSError as failure:
            raise _storage_failure("create its workspace", failure) from failure
        return _Workspace(temporary, budget)


class _Workspace:
    """All sorters of one blocking operator, sharing one logical-memory counter."""

    def __init__(
        self, temporary: tempfile.TemporaryDirectory[str], budget: LogicalMemoryBudget
    ) -> None:
        self._temporary = temporary
        self._budget = budget
        self._sorters: list[_Sorter] = []
        self._next_run = 0
        self._closed = False

    def sorter(self, comparator: SpillComparator) -> _Sorter:
        """Create one sorter after validating its engine-owned comparison capability."""
        self._require_open()
        if not callable(comparator):
            raise GrafxConfigurationError(
                "A query spill sorter needs a callable total comparator.",
                field="query_spill.comparator",
                value=type(comparator).__name__,
            )
        sorter = _Sorter(self, comparator)
        self._sorters.append(sorter)
        return sorter

    def close(self) -> None:
        """Release every sorter and remove the workspace, preserving the first failure."""
        if self._closed:
            return
        failure: BaseException | None = None
        for sorter in tuple(self._sorters):
            try:
                sorter.close()
            except BaseException as caught:
                if failure is None:
                    failure = caught
                else:
                    failure.add_note(
                        "Another query spill sorter also failed to close with "
                        f"{type(caught).__name__}: {caught}"
                    )
        try:
            self._temporary.cleanup()
        except OSError as caught:
            translated = _storage_failure("remove its workspace", caught)
            if failure is None:
                failure = translated
            else:
                failure.add_note(
                    "The query spill workspace also failed to clean up with "
                    f"{type(translated).__name__}: {translated}"
                )
        if failure is not None:
            raise failure
        self._closed = True

    def reserve(self, amount: int, *, reason: str) -> None:
        """Make room for and charge core-owned retained aggregate state."""
        try:
            self._make_room(amount)
        except GrafxQueryBudgetExceeded as failure:
            failure.details["reason"] = reason
            raise

    def release(self, amount: int) -> None:
        """Release core-owned retained aggregate state."""
        self._release(amount)

    def _path(self) -> str:
        """Allocate one private run name beneath the owned temporary directory."""
        self._require_open()
        self._next_run += 1
        return os.path.join(self._temporary.name, f"run-{self._next_run:08d}.bin")

    def _make_room(self, amount: int) -> None:
        """Flush retained sorter buffers until the shared counter can admit ``amount``."""
        while not self._budget.try_reserve(amount):
            candidates = [sorter for sorter in self._sorters if sorter._buffer_bytes]
            if not candidates:
                self._budget.reserve(amount, reason="spill_merge_workspace")
                return  # pragma: no cover - reserve either succeeds above or raises
            max(candidates, key=lambda sorter: sorter._buffer_bytes)._flush()

    def _release(self, amount: int) -> None:
        self._budget.release(amount)

    def _flush_all(self) -> None:
        for sorter in tuple(self._sorters):
            sorter._flush()

    def _forget(self, sorter: _Sorter) -> None:
        """Drop a closed sorter so per-group aggregate helpers cannot accumulate O(N)."""
        for position, candidate in enumerate(self._sorters):
            if candidate is sorter:
                del self._sorters[position]
                return
        raise RuntimeError("A query spill sorter lost its workspace ownership.")

    def _require_open(self) -> None:
        if self._closed:
            raise GrafxConfigurationError(
                "A closed query spill workspace cannot be reused.",
                field="query_spill",
                value="closed",
            )


class _Sorter:
    """One bounded append buffer followed by disk-backed pairwise merge passes."""

    def __init__(self, workspace: _Workspace, comparator: SpillComparator) -> None:
        self._workspace = workspace
        self._comparator = comparator
        self._buffer: list[_Record] = []
        self._buffer_bytes = 0
        # Binary merge levels keep run-path metadata O(log N), rather than retaining one Python
        # string per flushed record/run until input exhaustion.
        self._runs: list[str | None] = []
        self._reading = False
        self._closed = False

    def append(self, key: bytes, payload: bytes) -> None:
        """Retain one record or flush prior records before admission."""
        self._require_appendable()
        if type(key) is not bytes or type(payload) is not bytes:
            raise GrafxConfigurationError(
                "Query spill keys and payloads must be exact immutable bytes.",
                field="query_spill.record",
                value=f"{type(key).__name__}/{type(payload).__name__}",
            )
        charge = _RECORD_OVERHEAD + len(key) + len(payload)
        # A merge compares two run heads. Refusing a record larger than half the limit makes
        # that fixed two-record workspace provably bounded instead of hoping two large values
        # are never adjacent.
        if charge > self._workspace._budget.limit // 2:
            raise GrafxQueryBudgetExceeded(
                "One query spill record cannot fit beside another merge head within "
                "query_memory_budget_bytes.",
                field="query_memory_budget_bytes",
                limit=self._workspace._budget.limit,
                observed=charge * 2,
                record_bytes=charge,
                operator="query_spill",
                reason="single_record_merge_workspace",
            )
        self._workspace._make_room(charge)
        self._buffer.append(_Record(key, payload, charge))
        self._buffer_bytes += charge

    def records(self) -> Iterator[tuple[bytes, bytes]]:
        """Finalize all runs and yield their externally merged order."""
        self._require_appendable()
        self._reading = True
        self._workspace._flush_all()
        try:
            self._runs = [path for path in self._runs if path is not None]
            while len(self._runs) > 1:
                merged: list[str | None] = []
                for position in range(0, len(self._runs), 2):
                    left = self._runs[position]
                    assert left is not None
                    if position + 1 >= len(self._runs):
                        merged.append(left)
                        continue
                    right = self._runs[position + 1]
                    assert right is not None
                    target = self._workspace._path()
                    self._merge(left, right, target)
                    self._remove(left)
                    self._remove(right)
                    merged.append(target)
                self._runs = merged
            if self._runs:
                final = self._runs[0]
                assert final is not None
                yield from self._read(final)
        finally:
            self._remove_runs()

    def close(self) -> None:
        """Release buffered reservations and delete all completed runs; idempotent."""
        if self._closed:
            return
        if self._buffer_bytes:
            self._workspace._release(self._buffer_bytes)
        self._buffer.clear()
        self._buffer_bytes = 0
        self._remove_runs()
        self._workspace._forget(self)
        self._closed = True

    def _flush(self) -> None:
        """Sort and write the current buffer as one immutable run."""
        if not self._buffer:
            return
        records = self._buffer
        retained = self._buffer_bytes
        path = self._workspace._path()
        try:
            try:
                records.sort(key=cmp_to_key(self._compare_records))
                with open(path, "xb") as stream:
                    stream.write(_RUN_MAGIC)
                    for record in records:
                        self._write_record(stream, record)
            except OSError as failure:
                raise _storage_failure("write an ordered run", failure) from failure
        except BaseException:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
            raise
        finally:
            self._buffer = []
            self._buffer_bytes = 0
            self._workspace._release(retained)
        self._adopt_run(path)

    def _adopt_run(self, path: str) -> None:
        """Compact a completed run into binary levels with O(log N) path metadata."""
        current = path
        level = 0
        while level < len(self._runs):
            left = self._runs[level]
            if left is None:
                self._runs[level] = current
                return
            target = self._workspace._path()
            try:
                self._merge(left, current, target)
            except BaseException:
                try:
                    self._remove(target)
                except GrafxError:
                    pass
                raise
            self._runs[level] = None
            self._remove(left)
            self._remove(current)
            current = target
            level += 1
        self._runs.append(current)

    def _merge(self, left_path: str, right_path: str, target: str) -> None:
        """Merge two runs while retaining at most their two charged heads."""
        left = self._read(left_path)
        right = self._read(right_path)
        primary: BaseException | None = None
        try:
            left_record = next(left, None)
            right_record = next(right, None)
            try:
                with open(target, "xb") as stream:
                    stream.write(_RUN_MAGIC)
                    while left_record is not None and right_record is not None:
                        if self._compare_keys(left_record[0], right_record[0]) <= 0:
                            self._write_pair(stream, left_record)
                            left_record = next(left, None)
                        else:
                            self._write_pair(stream, right_record)
                            right_record = next(right, None)
                    while left_record is not None:
                        self._write_pair(stream, left_record)
                        left_record = next(left, None)
                    while right_record is not None:
                        self._write_pair(stream, right_record)
                        right_record = next(right, None)
            except OSError as failure:
                raise _storage_failure("merge ordered runs", failure) from failure
        except BaseException as caught:
            primary = caught
            raise
        finally:
            cleanup_failure: BaseException | None = primary
            for reader in (left, right):
                close = getattr(reader, "close", None)
                if not callable(
                    close
                ):  # pragma: no cover - _read always returns a generator
                    continue
                try:
                    close()
                except BaseException as close_failure:
                    if cleanup_failure is None:
                        cleanup_failure = close_failure
                    else:
                        cleanup_failure.add_note(
                            "A spill merge reader also failed to close with "
                            f"{type(close_failure).__name__}: {close_failure}"
                        )
            if primary is None and cleanup_failure is not None:
                raise cleanup_failure

    def _read(self, path: str) -> Iterator[tuple[bytes, bytes]]:
        """Read one run, charging each yielded head until its consumer advances."""
        try:
            with open(path, "rb") as stream:
                if stream.read(len(_RUN_MAGIC)) != _RUN_MAGIC:
                    raise GrafxCorruptionDetected(
                        "A temporary query spill run has an invalid version header.",
                        field="query_spill.header",
                        value="invalid",
                    )
                while True:
                    header = stream.read(_LENGTHS.size)
                    if not header:
                        return
                    if len(header) != _LENGTHS.size:
                        raise GrafxCorruptionDetected(
                            "A temporary query spill run ended inside a record header.",
                            field="query_spill.record",
                            value="truncated_header",
                        )
                    key_size, payload_size = _LENGTHS.unpack(header)
                    charge = _RECORD_OVERHEAD + key_size + payload_size
                    if charge > self._workspace._budget.limit // 2:
                        raise GrafxCorruptionDetected(
                            "A temporary query spill run declares an oversized record.",
                            field="query_spill.record",
                            value="oversized",
                            record_bytes=charge,
                            limit=self._workspace._budget.limit // 2,
                        )
                    key = stream.read(key_size)
                    payload = stream.read(payload_size)
                    if len(key) != key_size or len(payload) != payload_size:
                        raise GrafxCorruptionDetected(
                            "A temporary query spill run ended inside a record body.",
                            field="query_spill.record",
                            value="truncated_body",
                        )
                    self._workspace._make_room(charge)
                    try:
                        yield key, payload
                    finally:
                        self._workspace._release(charge)
        except OSError as failure:
            raise _storage_failure("read an ordered run", failure) from failure

    def _compare_records(self, left: _Record, right: _Record) -> int:
        return self._compare_keys(left.key, right.key)

    def _compare_keys(self, left: bytes, right: bytes) -> int:
        try:
            compared = self._comparator(left, right)
        except GrafxError:
            raise
        except Exception as failure:
            raise GrafxCorruptionDetected(
                "A temporary query spill key could not be compared.",
                field="query_spill.key",
                value="invalid",
            ) from failure
        if type(compared) is not int:
            raise GrafxConfigurationError(
                "A query spill comparator must return an exact integer.",
                field="query_spill.comparator",
                value=type(compared).__name__,
            )
        return compared

    @staticmethod
    def _write_record(stream: object, record: _Record) -> None:
        _Sorter._write_pair(stream, (record.key, record.payload))

    @staticmethod
    def _write_pair(stream: object, record: tuple[bytes, bytes]) -> None:
        key, payload = record
        stream.write(_LENGTHS.pack(len(key), len(payload)))  # type: ignore[attr-defined]
        stream.write(key)  # type: ignore[attr-defined]
        stream.write(payload)  # type: ignore[attr-defined]

    def _remove_runs(self) -> None:
        failure: GrafxError | None = None
        for path in tuple(self._runs):
            if path is None:
                continue
            try:
                self._remove(path)
            except GrafxError as caught:
                if failure is None:
                    failure = caught
                else:
                    failure.add_note(
                        "Another temporary query run also failed to delete with "
                        f"{type(caught).__name__}: {caught}"
                    )
        self._runs.clear()
        if failure is not None:
            raise failure

    @staticmethod
    def _remove(path: str) -> None:
        try:
            os.remove(path)
        except FileNotFoundError:
            return
        except OSError as failure:
            raise _storage_failure("delete an ordered run", failure) from failure

    def _require_appendable(self) -> None:
        self._workspace._require_open()
        if self._closed:
            raise GrafxConfigurationError(
                "A closed query spill sorter cannot be reused.",
                field="query_spill.sorter",
                value="closed",
            )
        if self._reading:
            raise GrafxConfigurationError(
                "A query spill sorter cannot accept records after reading starts.",
                field="query_spill.sorter",
                value="reading",
            )
