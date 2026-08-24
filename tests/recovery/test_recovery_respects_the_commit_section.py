"""Recovery never judges a WAL tail while a healthy commit is still appending it.

These are public, real-directory and cross-process proofs of P0.4.  The writer is an ordinary
``connect``/transaction process; its test storage adapter merely parks the real append one byte
before completion.  That makes a torn tail directly observable without replacing either the WAL
manager, transaction manager, recovery manager or coordinator.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Iterator, cast

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxLeaseTimeout
from okto_grafx.domain.ids import Epoch, Lsn
from okto_grafx.domain.ports.coordination import (
    DeadOwnerReport,
    Lease,
    ProcessCoordinator,
    ReaderHandle,
)
from okto_grafx.engine.coordination import COMMIT_SECTION
from okto_grafx.runtime.bootstrap import build_default_registry, release_ports
from okto_grafx.runtime.config import DatabaseConfig

CHILD = Path(__file__).resolve().parent / "recovery_commit_child.py"
CHILD_BUDGET_SECONDS: float = 120.0
OPEN_BUDGET_SECONDS: float = 120.0


class _SignallingCoordinator:
    """Expose the precise instant recovery asks for ``COMMIT_SECTION``."""

    def __init__(self, inner: ProcessCoordinator, attempted: threading.Event) -> None:
        self._inner = inner
        self._attempted = attempted

    def owner_id(self) -> str:
        return self._inner.owner_id()

    def current_epoch(self) -> Epoch:
        return self._inner.current_epoch()

    def acquire_writer_lease(self, *, timeout: float) -> Lease:
        return self._inner.acquire_writer_lease(timeout=timeout)

    def renew_lease(self, lease: Lease) -> Lease:
        return self._inner.renew_lease(lease)

    def release_lease(self, lease: Lease) -> None:
        self._inner.release_lease(lease)

    def validate_epoch(self, epoch: Epoch) -> None:
        self._inner.validate_epoch(epoch)

    def detect_dead_owner(self, *, stall_threshold: float) -> DeadOwnerReport | None:
        return self._inner.detect_dead_owner(stall_threshold=stall_threshold)

    def takeover(self) -> Lease:
        return self._inner.takeover()

    def register_reader(self, snapshot_lsn: Lsn) -> ReaderHandle:
        return self._inner.register_reader(snapshot_lsn)

    def refresh_reader(self, handle: ReaderHandle) -> None:
        self._inner.refresh_reader(handle)

    def unregister_reader(self, handle: ReaderHandle) -> None:
        self._inner.unregister_reader(handle)

    def reader_horizon(self) -> Lsn | None:
        return self._inner.reader_horizon()

    def exclusive(self, name: str, *, timeout: float) -> AbstractContextManager[None]:
        return self._signalled_section(name, timeout)

    @contextmanager
    def _signalled_section(self, name: str, timeout: float) -> Iterator[None]:
        if name == COMMIT_SECTION:
            self._attempted.set()
        with self._inner.exclusive(name, timeout=timeout):
            yield


def _bootstrap(root: Path) -> None:
    database = connect(root)
    try:
        with database.begin("write") as txn:
            txn.execute("CREATE NODE TABLE P(id INT64, name STRING, PRIMARY KEY(id))")
        database.checkpoint()
    finally:
        database.close()


def _start_parked_writer(
    root: Path, tmp_path: Path
) -> tuple[subprocess.Popen[str], Path, Path, Path]:
    ready = tmp_path / "writer.ready"
    release = tmp_path / "writer.release"
    result = tmp_path / "writer.result"
    process = subprocess.Popen(
        [sys.executable, str(CHILD), str(root), str(ready), str(release), str(result)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _wait_for_marker(ready, process)
    return process, ready, release, result


def _wait_for_marker(marker: Path, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + CHILD_BUDGET_SECONDS
    while time.monotonic() < deadline:
        if marker.exists():
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"the writer exited before {marker.name}: stdout={stdout!r} stderr={stderr[-1200:]!r}"
            )
        time.sleep(0.002)
    raise AssertionError(f"the writer did not create {marker.name} within the safety budget")


def _finish_writer(
    process: subprocess.Popen[str], release: Path, result: Path
) -> tuple[str, str]:
    release.write_text("release", encoding="ascii")
    stdout, stderr = process.communicate(timeout=CHILD_BUDGET_SECONDS)
    assert process.returncode == 0, stderr[-1200:]
    assert result.read_text(encoding="ascii") == "committed"
    return stdout, stderr


def _stop_writer(process: subprocess.Popen[str], release: Path) -> None:
    if process.poll() is not None:
        return
    release.write_text("release", encoding="ascii")
    try:
        process.communicate(timeout=10.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=10.0)


def _wal_bytes(root: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted((root / "wal").glob("*.wal"))}


def _forensic_bytes(root: Path) -> dict[str, bytes]:
    evidence: dict[str, bytes] = {}
    for directory in (root / "ledger", root / "quarantine"):
        if not directory.exists():
            continue
        for path in sorted(item for item in directory.rglob("*") if item.is_file()):
            evidence[str(path.relative_to(root))] = path.read_bytes()
    return evidence


@pytest.mark.multiprocess
@pytest.mark.timeout(180, method="thread")
def test_a_second_open_waits_for_an_in_flight_commit_instead_of_truncating(
    tmp_path: Path,
) -> None:
    root = tmp_path / "db"
    _bootstrap(root)
    forensic_before = _forensic_bytes(root)
    writer, _ready, release, result = _start_parked_writer(root, tmp_path)
    partial_tail = _wal_bytes(root)

    attempted = threading.Event()
    finished = threading.Event()
    outcome: dict[str, object] = {}
    config = DatabaseConfig(path=str(root), commit_lock_timeout_seconds=30.0)
    registry = build_default_registry(config)
    coordinator = cast(ProcessCoordinator, registry.get("coordinator"))
    registry.bind("coordinator", _SignallingCoordinator(coordinator, attempted))

    def open_in_parent() -> None:
        database = None
        try:
            database = connect(
                root,
                registry=registry,
                commit_lock_timeout_seconds=30.0,
            )
            outcome["rows"] = database.execute("MATCH (p:P) RETURN p.id").rows
            outcome["findings"] = database.verify("all").findings
        except BaseException as failure:
            outcome["failure"] = failure
        finally:
            if database is not None:
                database.close()
            finished.set()

    opener = threading.Thread(target=open_in_parent, daemon=True)
    opener.start()
    try:
        assert attempted.wait(timeout=30.0), "the second open never reached recovery's section"
        assert not finished.is_set(), "recovery crossed COMMIT_SECTION while the writer held it"
        assert _wal_bytes(root) == partial_tail, "the live writer's torn suffix was truncated"
        assert _forensic_bytes(root) == forensic_before

        _finish_writer(writer, release, result)
        opener.join(timeout=OPEN_BUDGET_SECONDS)
        assert not opener.is_alive(), "the second open did not resume after the commit completed"
    finally:
        _stop_writer(writer, release)
        opener.join(timeout=10.0)
        release_ports(registry)

    assert "failure" not in outcome, repr(outcome.get("failure"))
    assert outcome["rows"] == ((7,),)
    assert outcome["findings"] == ()
    assert _forensic_bytes(root) == forensic_before


@pytest.mark.multiprocess
@pytest.mark.timeout(180, method="thread")
def test_a_timed_out_open_is_typed_and_does_not_mutate_the_live_writers_tail(
    tmp_path: Path,
) -> None:
    root = tmp_path / "db"
    _bootstrap(root)
    forensic_before = _forensic_bytes(root)
    writer, _ready, release, result = _start_parked_writer(root, tmp_path)
    partial_tail = _wal_bytes(root)
    try:
        with pytest.raises(GrafxLeaseTimeout) as timed_out:
            connect(root, commit_lock_timeout_seconds=0.1)
        assert timed_out.value.retryable is True
        assert timed_out.value.details["section"] == COMMIT_SECTION
        assert _wal_bytes(root) == partial_tail
        assert _forensic_bytes(root) == forensic_before
        _finish_writer(writer, release, result)
    finally:
        _stop_writer(writer, release)

    reopened = connect(root)
    try:
        assert reopened.execute("MATCH (p:P) RETURN p.id").rows == ((7,),)
        assert reopened.verify("all").findings == ()
    finally:
        reopened.close()
    assert _forensic_bytes(root) == forensic_before
