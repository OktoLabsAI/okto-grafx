"""M0B invariant 3: index certification stays inside table history and publication.

Freshness is a claim about a log position. An index AHEAD of the published position holds
entries for a commit the published snapshot cannot see -- the shape a crash between the index
flush and the ``commit.state`` publication leaves. An index BEHIND the committed physical
high-water mark of its own table can omit a visible row. Neither may be handed to the planner as
fresh: either the published position is brought up to the log (P0.1 republishes ``commit.state``
in recovery) or the index is marked stale and rebuilt. Commits to unrelated tables do not raise
this index's required floor.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxError
from okto_grafx.runtime.bootstrap import release_ports

from m0b_probe_support import crash_before_the_commit_state_publication, insert, schema

pytestmark = pytest.mark.timeout(300, method="thread")


def test_an_index_ahead_of_the_published_position_is_never_certified_fresh() -> None:
    registry, bench, inner = crash_before_the_commit_state_publication(seed=1)
    try:
        reopened = connect(":memory:", registry=registry)
    except GrafxError as refused:
        release_ports(registry)
        pytest.fail(f"the writable reopen refused: {refused.code}")
    with reopened:
        published = reopened.transactions.published_state().last_committed_lsn
        certified_ahead = [
            (index.name, index.built_through_lsn, published)
            for index in reopened.indexes.indexes()
            if not index.stale and index.built_through_lsn > published
        ]
        assert not certified_ahead, (
            f"indexes certified fresh ahead of the published position: {certified_ahead}"
        )
    release_ports(registry)


def test_an_index_missing_its_tables_checkpoint_state_is_stale_or_rebuilt(
    tmp_path: Path,
) -> None:
    root = tmp_path / "db"
    with connect(root, page_size=512) as database:
        schema(database)
        insert(database, 1)
        database.checkpoint()
        checkpoint = database.transactions.published_state().checkpoint_lsn
    index_file = next(
        p for p in root.rglob("*") if p.is_file() and "pk_Person" in p.name
    )
    index_file.unlink()  # the replacement must cover the table state captured by the checkpoint
    with connect(root, page_size=512) as reopened:
        for index in reopened.indexes.indexes():
            if index.name != "pk_Person":
                continue
            assert index.stale or index.built_through_lsn >= checkpoint, (
                f"{index.name} was certified fresh at {index.built_through_lsn} after its "
                f"checkpointed table state at {checkpoint} was removed"
            )
