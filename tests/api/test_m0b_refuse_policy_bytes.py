"""M0B invariant 4: ``recovery_policy="refuse"`` refuses a damaged log and preserves every data byte.

Section 8.6 step 7: the refuse policy leaves the disk untouched -- no quarantine, no truncation,
no replay, no ledger repair -- and says so with a typed refusal an operator can act on. The
replay policy remains the way out, and it still holds every acknowledged row.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxRecoveryRefused

from m0b_probe_support import data_tree, ids, insert, schema

pytestmark = pytest.mark.timeout(120, method="thread")


def test_the_refuse_policy_refuses_a_damaged_log_and_preserves_every_data_byte(
    tmp_path: Path,
) -> None:
    root = tmp_path / "db"
    with connect(root, page_size=512) as database:
        schema(database)
        insert(database, 1)
    segments = sorted(p for p in (root / "wal").iterdir() if p.is_file())
    assert segments, "no log segment on disk"
    with segments[-1].open("ab") as tail:
        tail.write(os.urandom(97))  # a torn tail: bytes that are no record
    before = data_tree(root)
    with pytest.raises(GrafxRecoveryRefused):
        connect(root, page_size=512, recovery_policy="refuse")
    assert data_tree(root) == before, "the refuse policy changed a data file"
    with connect(root, page_size=512) as recovered:
        assert ids(recovered) == (1,)
