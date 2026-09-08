"""Recovery judges and mutates WAL only while holding the commit section."""

from __future__ import annotations

from okto_grafx.runtime.capability_probe import port_has_attribute

from contextlib import contextmanager
from typing import Iterator

import pytest

from okto_grafx.domain.errors import GrafxLeaseTimeout
from okto_grafx.engine.coordination import COMMIT_SECTION
from okto_grafx.engine.recovery_manager import RecoveryManager

from .conftest import HEAP_FILE, Stack, commit_pages, make_page_image


class _Coordinator:
    """Record the shared-section lifetime and optionally refuse its acquisition."""

    def __init__(self, *, refuse: bool = False) -> None:
        self.active = False
        self.refuse = refuse
        self.events: list[tuple[str, object]] = []

    def owner_id(self) -> str:
        return "recovery-fence-test"

    def reader_horizon(self) -> int | None:
        return None

    @contextmanager
    def exclusive(self, name: str, *, timeout: float) -> Iterator[None]:
        self.events.append(("enter", (name, timeout)))
        if self.refuse:
            raise GrafxLeaseTimeout(
                "The commit section stayed busy.",
                section=name,
                retryable=True,
            )
        self.active = True
        try:
            yield
        finally:
            self.active = False
            self.events.append(("exit", name))


class _WalUnderFence:
    """Proxy the real WAL while asserting its destructive decision is fenced."""

    def __init__(self, inner: object, coordinator: _Coordinator) -> None:
        self._inner = inner
        self._coordinator = coordinator
        self.scans = 0
        self.truncations = 0

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    def scan_all(self) -> object:
        assert self._coordinator.active
        self.scans += 1
        return self._inner.scan_all()  # type: ignore[attr-defined]

    def truncate_after(self, lsn: int) -> object:
        assert self._coordinator.active
        self.truncations += 1
        return self._inner.truncate_after(lsn)  # type: ignore[attr-defined]


def _manager(stack: Stack, coordinator: _Coordinator, wal: object) -> RecoveryManager:
    return RecoveryManager(
        stack.storage,  # type: ignore[arg-type]
        wal,
        stack.ledger,
        stack.quarantine,
        stack.pool,
        stack.metrics,  # type: ignore[arg-type]
        attribute_probe=port_has_attribute,
        catalog=stack.catalog,
        coordinator=coordinator,
        commit_lock_timeout=2.5,
    )


def _append_damaged_tail(stack: Stack) -> str:
    """Create one valid WAL transaction, append a torn tail, and return its segment."""
    commit_pages(
        stack,
        [(HEAP_FILE, 3, make_page_image(stack.codec, [b"fenced"], page_index=3))],
    )
    segment = stack.wal.segments()[-1].name
    stack.storage.append_log(segment, bytes(64))  # type: ignore[attr-defined]
    return segment


def test_scan_and_truncate_share_the_exact_commit_section(stack: Stack) -> None:
    _append_damaged_tail(stack)
    coordinator = _Coordinator()
    wal = _WalUnderFence(stack.wal, coordinator)

    report = _manager(stack, coordinator, wal).run()

    assert report.outcome == "truncated"
    assert wal.scans == 1
    assert wal.truncations == 1
    assert coordinator.events == [
        ("enter", (COMMIT_SECTION, 2.5)),
        ("exit", COMMIT_SECTION),
    ]


def test_section_timeout_reads_or_changes_no_wal_byte(stack: Stack) -> None:
    segment = _append_damaged_tail(stack)
    size = stack.storage.log_size(segment)  # type: ignore[attr-defined]
    coordinator = _Coordinator(refuse=True)
    wal = _WalUnderFence(stack.wal, coordinator)

    with pytest.raises(GrafxLeaseTimeout) as refused:
        _manager(stack, coordinator, wal).run()

    assert refused.value.retryable is True
    assert wal.scans == 0
    assert wal.truncations == 0
    assert stack.storage.log_size(segment) == size  # type: ignore[attr-defined]
