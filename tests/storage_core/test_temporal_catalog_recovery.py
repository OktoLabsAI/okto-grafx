"""Temporal metadata fence recovery, independent of general value admission."""

import pytest

from okto_grafx.domain.model.catalog import Catalog
from okto_grafx.domain.model.temporal_codec import TEMPORAL_VALUES_CAPABILITY
from okto_grafx.domain.recovery.decision import committed_replay
from okto_grafx.engine.commit_redo import CommitRedo
from tests.storage_core.test_catalog_replay_images import records, staged


@pytest.mark.parametrize("torn_header", [False, True])
def test_committed_temporal_metadata_recovers_and_replay_is_idempotent(torn_header):
    value = Catalog().upgrade_index_catalog()._enable_temporal_values()
    pool, store, images = staged(value, 1000)
    if torn_header:
        pool.flush()
        pool.invalidate("catalog.dat")
        pool.storage.write_page("catalog.dat", 0, bytes(512))
    replay = committed_replay(records(images, 1000))
    redo = CommitRedo(pool)
    proof = redo.preflight(replay, _passage=store)
    result = redo.apply(replay, _preflighted=proof, _passage=store)
    assert result.page_images_applied == len(images)
    assert store.read_from_pages().serialize() == value.serialize()
    assert store.read_from_pages().requires_capability(TEMPORAL_VALUES_CAPABILITY)
    assert redo.apply(replay).page_images_applied == 0


def test_uncommitted_temporal_metadata_never_becomes_a_recovery_effect():
    value = Catalog().upgrade_index_catalog()._enable_temporal_values()
    pool, store, images = staged(value, 1000)
    before = store.read_from_pages().serialize()
    writes = list(pool.storage.write_calls)
    replay = committed_replay(records(images, 1000)[:-1])
    assert not replay.effects
    assert CommitRedo(pool).apply(replay).page_images_applied == 0
    assert store.read_from_pages().serialize() == before
    assert pool.storage.write_calls == writes

