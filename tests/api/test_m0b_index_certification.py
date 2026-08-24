"""M0B invariant 3: an index is never certified fresh behind the checkpoint or ahead of the published position.

Freshness is a claim about a log position. An index AHEAD of the published position holds
entries for a commit the published snapshot cannot see -- the shape a crash between the index
flush and the ``commit.state`` publication leaves -- and an index BEHIND the checkpoint lacks
entries every snapshot can see. Neither may be handed to the planner as fresh: either the
published position is brought up to the log (P0.1 republishes ``commit.state`` in recovery) or
the index is marked stale and rebuilt.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxError
from okto_grafx.runtime.bootstrap import release_ports

from m0b_probe_support import crash_before_the_commit_state_publication, insert, schema

pytestmark = pytest.mark.timeout(300, method="thread")


@pytest.mark.xfail(
    strict=True,
    reason="M0B invariant 3 (base 8c88e9d): after the crash and a writable reopen, pk_Person says "
    "built_through 8 while the published position is 4, and it is not stale -- certified "
    "ahead of the published state; pending M0B.",
)
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


def test_an_index_behind_the_checkpoint_is_stale_or_rebuilt_never_certified_as_it_stands(
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
    index_file.unlink()  # the index is now infinitely behind the checkpoint
    with connect(root, page_size=512) as reopened:
        for index in reopened.indexes.indexes():
            if index.name != "pk_Person":
                continue
            assert index.stale or index.built_through_lsn >= checkpoint, (
                f"{index.name} certified fresh at {index.built_through_lsn} behind checkpoint "
                f"{checkpoint}"
            )
