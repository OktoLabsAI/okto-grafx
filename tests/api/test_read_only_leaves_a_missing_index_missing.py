"""A read-only open of a database whose index file is gone must not put one back (P1.1).

``read_only=True`` promises that no byte of the DATABASE moves because of this handle. The
assembly registers the declared indexes on a read-only open too, and ``IndexManager.register``
creates the file of an index it cannot find -- so a reader that met a missing ``pk_Person``
index created ``index/pk_Person.idx`` and persisted a stale mark into it (EVOLUTION_PLAN_CODEX.md
P1.1). The reader's answer was right (the heap is the authority), which is exactly why nothing
noticed: the property is in the bytes on the device, not in the rows.

What must hold: the reader answers exactly (the rows are in the heap) or refuses with a typed
error, and the data tree is byte-for-byte what it was before the open. Either branch is
acceptable; a silent write is not.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxError

pytestmark = pytest.mark.timeout(120, method="thread")


def _data_tree(root: Path) -> dict[str, str]:
    """Return every DATA file under the root with the digest of its bytes.

    ``control/`` is excluded on purpose, as in ``test_read_only_and_doors._tree``: a reader is
    REQUIRED to publish its registration there (AC-8). Everything else -- heap, catalog, meta,
    indexes, log, ledger, quarantine -- is what read-only promises not to touch.
    """
    tree: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root)).replace("\\", "/")
        if path.is_file() and not relative.startswith("control/"):
            tree[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return tree


@pytest.mark.xfail(
    strict=True,
    reason="P1.1 (pre-fix at 8c88e9d): the read-only open recreated index/pk_Person.idx. Pending M0C.",
)
def test_a_read_only_open_does_not_recreate_a_missing_index_file(
    tmp_path: Path,
) -> None:
    root = tmp_path / "db"
    with connect(root, page_size=512) as db:
        with db.begin("write") as txn:
            txn.execute(
                "CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))"
            )
            txn.execute("CREATE (:Person {id: 1, name: 'ada'})")

    index_files = [
        path for path in root.rglob("*") if path.is_file() and "pk_Person" in path.name
    ]
    assert len(index_files) == 1, (
        index_files
    )  # A72: the fixture produced the state it claims
    index_file = index_files[0]
    index_file.unlink()
    before = _data_tree(root)
    assert not any("pk_Person" in name for name in before)

    try:
        read_only = connect(root, page_size=512, read_only=True)
    except GrafxError as refused:
        # A typed refusal is the other acceptable answer: the reader declined rather than
        # repaired. Whatever it said, it must have written nothing.
        assert refused.code, refused
    else:
        with read_only:
            rows = tuple(read_only.execute("MATCH (p:Person) RETURN p.name"))
            # The heap is the authority: with or without the index, the row is there.
            assert rows == (("ada",),), rows

    after = _data_tree(root)
    changed = sorted(
        name for name in set(before) | set(after) if before.get(name) != after.get(name)
    )
    assert not changed, f"a read-only open changed the data tree: {changed}"
    assert not index_file.exists(), "a read-only open created an index file"
