"""Real writer used by the cross-process recovery/commit-section regressions.

The storage wrapper deliberately exposes a WAL append in two physical writes.  It parks after
all but the last byte reached the real directory, which leaves an objectively torn record while
the ordinary transaction manager still owns ``COMMIT_SECTION``.  The parent therefore observes
the exact production race without patching either the WAL or the commit implementation.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import cast

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
for _entry in (str(PROJECT_ROOT), str(PROJECT_ROOT / "src")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from okto_grafx import connect  # noqa: E402
from okto_grafx.domain.ids import PageIndex  # noqa: E402
from okto_grafx.domain.ports.storage import StorageDevice  # noqa: E402
from okto_grafx.runtime.bootstrap import (  # noqa: E402
    build_default_registry,
    release_ports,
)
from okto_grafx.runtime.config import DatabaseConfig  # noqa: E402

POLL_SECONDS: float = 0.002
SAFETY_BUDGET_SECONDS: float = 120.0


class _HoldingStorageDevice:
    """Delegate every storage door, splitting only the first WAL append."""

    def __init__(self, inner: StorageDevice, ready: Path, release: Path) -> None:
        self._inner = inner
        self._ready = ready
        self._release = release
        self._armed = True

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def page_size(self) -> int:
        return self._inner.page_size

    def exists(self, file: str) -> bool:
        return self._inner.exists(file)

    def create(self, file: str, *, exclusive: bool = True) -> None:
        self._inner.create(file, exclusive=exclusive)

    def remove(self, file: str) -> None:
        self._inner.remove(file)

    def list_files(self, prefix: str = "") -> tuple[str, ...]:
        return self._inner.list_files(prefix)

    def file_size(self, file: str) -> int:
        return self._inner.file_size(file)

    def atomic_replace(self, source: str, target: str) -> None:
        self._inner.atomic_replace(source, target)

    def recycle(self, file: str) -> bool:
        return self._inner.recycle(file)

    def page_count(self, file: str) -> int:
        return self._inner.page_count(file)

    def allocate(self, file: str, count: int = 1) -> PageIndex:
        return self._inner.allocate(file, count)

    def read_page(self, file: str, page_index: PageIndex) -> bytes:
        return self._inner.read_page(file, page_index)

    def write_page(self, file: str, page_index: PageIndex, data: bytes) -> None:
        self._inner.write_page(file, page_index, data)

    def append_log(self, file: str, payload: bytes) -> int:
        if self._armed and file.startswith("wal/"):
            if len(payload) < 2:
                raise RuntimeError("the WAL append is too short to expose a torn record")
            self._armed = False
            partial_size = self._inner.append_log(file, payload[:-1])
            self._ready.write_text(f"{file} {partial_size}", encoding="ascii")
            deadline = time.monotonic() + SAFETY_BUDGET_SECONDS
            while not self._release.exists() and time.monotonic() < deadline:
                time.sleep(POLL_SECONDS)
            if not self._release.exists():
                raise RuntimeError("the parent never released the parked WAL append")
            return self._inner.append_log(file, payload[-1:])
        return self._inner.append_log(file, payload)

    def read_log(self, file: str, offset: int, length: int) -> bytes:
        return self._inner.read_log(file, offset, length)

    def log_size(self, file: str) -> int:
        return self._inner.log_size(file)

    def truncate_log(self, file: str, size: int) -> None:
        self._inner.truncate_log(file, size)

    def durable_barrier(self, file: str | None = None) -> None:
        self._inner.durable_barrier(file)

    def close(self) -> None:
        closer = getattr(self._inner, "close", None)
        if callable(closer):
            closer()


def main(root: str, ready_name: str, release_name: str, result_name: str) -> None:
    """Commit one row through the public API and report only after it was acknowledged."""
    ready = Path(ready_name)
    release = Path(release_name)
    result = Path(result_name)
    config = DatabaseConfig(
        path=root,
        commit_lock_timeout_seconds=30.0,
        lease_timeout_seconds=30.0,
    )
    registry = build_default_registry(config)
    storage = cast(StorageDevice, registry.get("storage"))
    registry.bind("storage", _HoldingStorageDevice(storage, ready, release))
    database = None
    try:
        database = connect(
            root,
            registry=registry,
            commit_lock_timeout_seconds=30.0,
            lease_timeout_seconds=30.0,
        )
        with database.begin("write") as txn:
            txn.execute("CREATE (:P {id: 7, name: 'parked-writer'})")
        result.write_text("committed", encoding="ascii")
    finally:
        if database is not None:
            database.close()
        release_ports(registry)


if __name__ == "__main__":
    main(*sys.argv[1:])
