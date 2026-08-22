"""A damaged range too large to carry inside a ledger entry (FR-9, FR-10, G8).

A damaged range can be a whole segment. The entry keeps its provenance, quarantine keeps the
bytes, and an export refuses by NAMING the quarantine entry rather than returning a partial
range under a digest that says the entry is complete.
"""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import GrafxLedgerError
from okto_grafx.domain.recovery.report import OUTCOME_TRUNCATED
from okto_grafx.engine.recovery_manager import MAX_LEDGER_BODY_BYTES

from .conftest import HEAP_FILE, Stack, build_stack, commit_pages, make_page_image

SMALL = 96


def _commit(stack: Stack) -> None:
    """Commit one heap page so the log holds an intact record before the damage."""
    commit_pages(
        stack,
        [(HEAP_FILE, 3, make_page_image(stack.codec, [b"kept"], page_index=3))],
        txn_id=1,
    )


def _damage(stack: Stack, size: int) -> tuple[str, int]:
    """Append a run of bytes that is not a record, and return where it starts."""
    name = stack.wal.segments()[-1].name
    offset = stack.storage.log_size(name)  # type: ignore[attr-defined]
    stack.storage.append_log(name, bytes(size))  # type: ignore[attr-defined]
    stack.wal.open()
    return name, offset


def _reopened(stack: Stack) -> Stack:
    """Return a second stack over the same device, as a reopen of the database would build."""
    return build_stack(stack.storage, clock=stack.clock, metrics=stack.metrics, bootstrap=False)


def test_a_small_damaged_range_is_carried_in_the_entry_and_exports(stack: Stack) -> None:
    _commit(stack)
    _damage(stack, SMALL)
    reopened = _reopened(stack)
    reopened.recovery().run()
    entry = reopened.ledger.list(origin_class="forensic")[0]
    assert reopened.ledger.export(entry.entry_id) == bytes(SMALL)


@pytest.mark.slow
def test_a_range_too_large_to_carry_is_kept_in_quarantine_and_named_by_the_entry(
    stack: Stack,
) -> None:
    _commit(stack)
    size = MAX_LEDGER_BODY_BYTES + 1
    name, offset = _damage(stack, size)
    reopened = _reopened(stack)
    report = reopened.recovery().run()
    assert report.outcome == OUTCOME_TRUNCATED
    assert report.ledger_entries_created == report.records_discarded == 1

    entry = reopened.ledger.list(origin_class="forensic")[0]
    provenance = reopened.ledger.provenance(entry.entry_id)
    assert provenance.origin == name and provenance.offset == offset
    assert provenance.length == size
    assert provenance.body == b""
    assert provenance.quarantine

    with pytest.raises(GrafxLedgerError) as caught:
        reopened.ledger.export(entry.entry_id)
    assert caught.value.details["quarantine"] == provenance.quarantine

    # The bytes themselves are all there, in the entry the refusal named.
    assert len(reopened.quarantine.read(provenance.quarantine)) == size
