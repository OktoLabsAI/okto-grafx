"""Focused contracts for the fenced live index hot-bucket batch.

The accelerator is intentionally narrower than ``IndexStore.commit``: only the transaction
manager's already-fenced apply path may authorize an ephemeral directory.  These tests keep that
authority boundary beside the effect-equivalence and failed-prefix rules it protects.
"""

from __future__ import annotations

from contextvars import Context, copy_context

import pytest

from okto_grafx.domain.errors import GrafxIndexError
from okto_grafx.domain.ids import NO_CSN, NO_PAGE, RecordRef
from okto_grafx.domain.index import IndexChange, IndexOperation
from okto_grafx.domain.index.entry import IndexEntry
from okto_grafx.domain.index.keys import bucket_of
from okto_grafx.domain.page import Page
import okto_grafx.engine.index_manager as index_manager_module
from okto_grafx.engine.index_manager import HashIndex, IndexManager, IndexStore
from okto_grafx.engine.txn_manager import TransactionManager
from okto_grafx.runtime.scoped_value import ContextLocalValue

from .conftest import Database, TransactionDouble


COMMIT_LSN = 500


def _colliding_keys(store: IndexStore, count: int, *, prefix: str = "live") -> tuple[bytes, ...]:
    """Return distinct keys in bucket zero so a small batch crosses the hot threshold."""
    keys: list[bytes] = []
    candidate = 0
    while len(keys) < count:
        key = f"{prefix}-{candidate:08d}".encode().ljust(48, b"x")
        if bucket_of(key, store.definition.bucket_count) == 0:
            keys.append(key)
        candidate += 1
    return tuple(keys)


def _stage_inserts(
    store: IndexStore,
    txn: TransactionDouble,
    keys: tuple[bytes, ...],
    *,
    first_ref: int = 1,
) -> tuple[RecordRef, ...]:
    refs = tuple(RecordRef(first_ref + offset, 1) for offset in range(len(keys)))
    for key, ref in zip(keys, refs, strict=True):
        store.stage_insert(txn, key, ref, 0)
    return refs


def _commit_under_write_authority(
    manager: IndexManager, txn: TransactionDouble, csn: int = COMMIT_LSN
) -> int:
    """Use only the internal door whose caller already owns the production write fences."""
    return manager._commit_under_write_authority(txn, csn)  # noqa: SLF001


def _images(database: Database) -> tuple[bytes, ...]:
    file = database.exact.file
    database.pool.flush(file)
    return tuple(
        database.device.raw_page(file, page)
        for page in range(database.device.page_count(file))
    )


def _entry_images(store: IndexStore) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (entry.key, entry.ref, entry.versioned, entry.born_csn, entry.dead_csn)
        for entry in store.walk()
    )


def test_transaction_manager_selects_fenced_door_and_preserves_legacy_fallback() -> None:
    """Only the post-barrier transaction seam may select the optimized manager door."""

    class FencedIndexes:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object, int]] = []

        def _commit_under_write_authority(self, txn: object, csn: int) -> int:
            self.calls.append(("fenced", txn, csn))
            return 7

        def commit(self, _txn: object, _csn: int) -> int:
            raise AssertionError("TransactionManager bypassed the fenced index door")

    class LegacyIndexes:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object, int]] = []

        def commit(self, txn: object, csn: int) -> int:
            self.calls.append(("legacy", txn, csn))
            return 3

    txn = TransactionDouble(txn_id=99)
    fenced = FencedIndexes()
    fenced_manager = object.__new__(TransactionManager)
    fenced_manager._index_manager = fenced  # type: ignore[assignment]  # noqa: SLF001
    legacy = LegacyIndexes()
    legacy_manager = object.__new__(TransactionManager)
    legacy_manager._index_manager = legacy  # type: ignore[assignment]  # noqa: SLF001

    assert fenced_manager._apply_index_changes(txn, 600) == 7  # noqa: SLF001
    assert fenced.calls == [("fenced", txn, 600)]
    assert legacy_manager._apply_index_changes(txn, 601) == 3  # noqa: SLF001
    assert legacy.calls == [("legacy", txn, 601)]


@pytest.mark.parametrize("door", ("store", "manager"))
def test_direct_commit_doors_never_authorize_the_live_hot_directory(
    monkeypatch: pytest.MonkeyPatch, door: str
) -> None:
    database = Database(budget_pages=128)
    txn = TransactionDouble(txn_id=101)
    keys = _colliding_keys(database.exact, 8)
    refs = _stage_inserts(database.exact, txn, keys)

    def unexpected(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("a direct commit must retain scalar page authority")

    monkeypatch.setattr(IndexStore, "_apply_common_replay_hot_change", unexpected)
    if door == "store":
        applied = database.exact.commit(txn, COMMIT_LSN)
    else:
        applied = database.manager.commit(txn, COMMIT_LSN)

    assert applied == len(keys)
    assert {entry.ref for entry in database.exact.walk()} == set(refs)


def test_internal_write_authority_enables_the_live_hot_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(budget_pages=128)
    txn = TransactionDouble(txn_id=102)
    keys = _colliding_keys(database.exact, 8)
    _stage_inserts(database.exact, txn, keys)
    original = IndexStore._apply_common_replay_hot_change
    calls = 0

    def counted(store: IndexStore, *args: object, **kwargs: object) -> bool:
        nonlocal calls
        calls += 1
        return original(store, *args, **kwargs)

    monkeypatch.setattr(IndexStore, "_apply_common_replay_hot_change", counted)

    assert _commit_under_write_authority(database.manager, txn) == len(keys)
    assert calls == len(keys)


def test_bucket_scan_consumes_the_page_slot_iterator_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(budget_pages=64)
    txn = TransactionDouble(txn_id=103)
    key = _colliding_keys(database.exact, 1, prefix="slot-view")[0]
    reference = _stage_inserts(database.exact, txn, (key,))[0]
    assert database.exact.commit(txn, COMMIT_LSN) == 1

    def unexpected(_page: Page) -> tuple[int, ...]:
        raise AssertionError("the fused scan rebuilt the live-slot tuple")

    monkeypatch.setattr(Page, "live_slots", unexpected)

    assert [entry.ref for entry in database.exact.candidates(key)] == [reference]


def test_hot_directory_keeps_location_in_its_tuple_without_retagging_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(budget_pages=64)
    key = _colliding_keys(database.exact, 1, prefix="tuple-location")[0]
    reference = RecordRef(17, 1)
    identity = (key, reference)
    bucket = database.exact._prepare_common_replay_hot_bucket(  # noqa: SLF001
        0,
        {identity},
        page_limit=64,
    )
    assert bucket is not None
    assert bucket.entries[identity] is None
    insert = IndexChange(
        index=database.exact.name,
        operation=IndexOperation.INSERT,
        key=key,
        ref=reference,
        csn=NO_CSN,
        versioned=False,
    )
    tombstone = IndexChange(
        index=database.exact.name,
        operation=IndexOperation.TOMBSTONE,
        key=key,
        ref=reference,
        csn=COMMIT_LSN + 1,
        versioned=False,
    )

    def unexpected(*_args: object, **_kwargs: object) -> IndexEntry:
        raise AssertionError("the hot directory redundantly copied its tuple location")

    monkeypatch.setattr(IndexEntry, "located_at", unexpected)

    assert database.exact._apply_common_replay_hot_change(  # noqa: SLF001
        bucket, insert, COMMIT_LSN
    )
    inserted = bucket.entries[identity]
    assert inserted is not None
    assert inserted[2].page == NO_PAGE
    assert database.exact._apply_common_replay_hot_change(  # noqa: SLF001
        bucket, tombstone, COMMIT_LSN + 1
    )
    ended = bucket.entries[identity]
    assert ended is not None
    assert ended[2].dead_csn == COMMIT_LSN + 1
    assert ended[2].page == NO_PAGE


def test_instance_level_physical_hook_declines_live_acceleration() -> None:
    """A subclass dictionary must not hide a per-instance scalar hook from eligibility."""

    class CustomizableHashIndex(HashIndex):
        pass

    database = Database(budget_pages=64)
    store = CustomizableHashIndex(
        database.exact.definition, database.pool, database.metrics
    )
    setattr(store, "_apply_change", lambda *_args, **_kwargs: False)

    assert not store._uses_canonical_live_hot_hooks()  # noqa: SLF001


def test_copied_authority_context_is_revoked_after_the_fenced_call() -> None:
    """A context copied inside an override must retain only an inactive scope afterward."""
    database = Database(budget_pages=64)
    captured: list[Context] = []

    class CapturingManager(IndexManager):
        def commit(self, txn: object, csn: int) -> int:
            del txn, csn
            captured.append(copy_context())
            return 0

    context = ContextLocalValue("captured-live-commit")
    manager = CapturingManager(
        database.pool, database.heap, database.metrics, live_commit_context=context
    )
    txn = TransactionDouble(txn_id=109)

    assert manager._commit_under_write_authority(txn, COMMIT_LSN) == 0  # noqa: SLF001
    inherited = captured[0].run(context.get)

    assert isinstance(inherited, index_manager_module._LiveCommitAuthority)  # noqa: SLF001
    assert inherited.active is False
    assert inherited.store is None


@pytest.mark.parametrize(("count", "prepared"), ((1, False), (2, True)))
def test_live_hot_threshold_starts_at_the_second_effect_in_one_bucket(
    monkeypatch: pytest.MonkeyPatch, count: int, prepared: bool
) -> None:
    database = Database(budget_pages=64)
    txn = TransactionDouble(txn_id=110 + count)
    _stage_inserts(database.exact, txn, _colliding_keys(database.exact, count))
    original = IndexStore._prepare_common_replay_hot_bucket
    calls = 0

    def counted(store: IndexStore, *args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return original(store, *args, **kwargs)

    monkeypatch.setattr(IndexStore, "_prepare_common_replay_hot_bucket", counted)

    assert _commit_under_write_authority(database.manager, txn) == count
    assert (calls > 0) is prepared


def test_live_hot_bucket_uses_one_preflight_instead_of_one_scan_per_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The selected path removes the staged-count multiplier from bucket traversal."""

    def measured(*, fenced: bool) -> tuple[int, int]:
        database = Database(budget_pages=128)
        txn = TransactionDouble(txn_id=140 if fenced else 141)
        keys = _colliding_keys(database.exact, 128, prefix="curve")
        _stage_inserts(database.exact, txn, keys)
        original_scan = IndexStore._scan_bucket
        original_prepare = IndexStore._prepare_common_replay_hot_bucket
        scans = 0
        preparations = 0

        def counted_scan(
            store: IndexStore,
            bucket: int,
            key: bytes | None = None,
            ref: RecordRef | None = None,
            *,
            first_matching_page: bool = False,
        ) -> object:
            nonlocal scans
            if store is database.exact and bucket == 0:
                scans += 1
            return original_scan(
                store,
                bucket,
                key,
                ref,
                first_matching_page=first_matching_page,
            )

        def counted_prepare(
            store: IndexStore, *args: object, **kwargs: object
        ) -> object:
            nonlocal preparations
            if store is database.exact:
                preparations += 1
            return original_prepare(store, *args, **kwargs)

        with monkeypatch.context() as measurement:
            measurement.setattr(IndexStore, "_scan_bucket", counted_scan)
            measurement.setattr(
                IndexStore, "_prepare_common_replay_hot_bucket", counted_prepare
            )
            applied = (
                _commit_under_write_authority(database.manager, txn)
                if fenced
                else database.exact.commit(txn, COMMIT_LSN)
            )
        assert applied == len(keys)
        return scans, preparations

    assert measured(fenced=True) == (0, 1)
    assert measured(fenced=False) == (128, 0)


@pytest.mark.parametrize(
    "limit_name",
    (
        "_COMMON_REPLAY_HOT_TARGET_LIMIT",
        "_COMMON_REPLAY_HOT_PAGE_LIMIT",
        "_COMMON_REPLAY_HOT_BUCKET_LIMIT",
    ),
)
def test_live_hot_limits_decline_to_scalar_before_using_a_directory(
    monkeypatch: pytest.MonkeyPatch, limit_name: str
) -> None:
    database = Database(budget_pages=64)
    txn = TransactionDouble(txn_id=150)
    keys = _colliding_keys(database.exact, 2, prefix=limit_name)
    _stage_inserts(database.exact, txn, keys)
    limit = 1 if limit_name.endswith("TARGET_LIMIT") else 0
    monkeypatch.setattr(index_manager_module, limit_name, limit)

    def unexpected(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("a declined directory must not apply a hot effect")

    monkeypatch.setattr(IndexStore, "_apply_common_replay_hot_change", unexpected)

    assert _commit_under_write_authority(database.manager, txn) == len(keys)


def test_fenced_hot_batch_is_byte_equivalent_to_scalar_mixed_effects() -> None:
    accelerated = Database(name="live-hot", budget_pages=256)
    scalar = Database(name="live-scalar", budget_pages=256)
    hot_keys = _colliding_keys(accelerated.exact, 24, prefix="mixed")
    assert hot_keys == _colliding_keys(scalar.exact, 24, prefix="mixed")

    for database in (accelerated, scalar):
        seed = TransactionDouble(txn_id=201)
        _stage_inserts(database.exact, seed, hot_keys[:16])
        database.exact.commit(seed, 100)

    def stage_mixed(database: Database, txn_id: int) -> TransactionDouble:
        txn = TransactionDouble(txn_id=txn_id)
        store = database.exact
        existing = RecordRef(1, 1)
        store.stage_insert(txn, hot_keys[0], existing, 0)  # duplicate insert
        store.stage_delete(txn, hot_keys[0], existing, 300)
        store.stage_delete(txn, hot_keys[0], existing, 301)  # repeated tombstone
        store._stage(  # noqa: SLF001 - reconciliation uses this exact durable effect
            txn,
            IndexChange(
                index=store.name,
                operation=IndexOperation.REMOVE,
                key=hot_keys[0],
                ref=existing,
                csn=400,
                versioned=False,
            ),
        )
        store.stage_insert(txn, hot_keys[0], existing, 0)  # remove then reinsert
        store._stage(  # noqa: SLF001 - absent reconcile target is a counted no-op
            txn,
            IndexChange(
                index=store.name,
                operation=IndexOperation.REMOVE,
                key=hot_keys[20],
                ref=RecordRef(900, 1),
                csn=400,
                versioned=False,
            ),
        )
        _stage_inserts(store, txn, hot_keys[16:24], first_ref=101)
        return txn

    hot_txn = stage_mixed(accelerated, 202)
    scalar_txn = stage_mixed(scalar, 202)
    expected = len(accelerated.exact.pending(hot_txn))

    assert _commit_under_write_authority(accelerated.manager, hot_txn) == expected
    assert scalar.exact.commit(scalar_txn, COMMIT_LSN) == expected

    assert _entry_images(accelerated.exact) == _entry_images(scalar.exact)
    assert accelerated.exact.header == scalar.exact.header
    assert accelerated.exact.missing_targets == scalar.exact.missing_targets == 1
    assert _images(accelerated) == _images(scalar)


def test_hot_failure_keeps_staging_and_short_mark_then_retry_is_scalar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(budget_pages=128)
    txn = TransactionDouble(txn_id=301)
    keys = _colliding_keys(database.exact, 8, prefix="retry")
    refs = _stage_inserts(database.exact, txn, keys)
    expected = database.exact.pending(txn)
    original = IndexStore._apply_common_replay_hot_change
    calls = 0

    def fail_second(store: IndexStore, *args: object, **kwargs: object) -> bool:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise GrafxIndexError("injected live hot-batch failure", field="injected")
        return original(store, *args, **kwargs)

    with monkeypatch.context() as failure:
        failure.setattr(IndexStore, "_apply_common_replay_hot_change", fail_second)
        with pytest.raises(GrafxIndexError, match="injected live hot-batch failure"):
            _commit_under_write_authority(database.manager, txn)

    assert database.exact.pending(txn) == expected
    assert database.exact.stale
    assert database.exact._short_commit == txn.txn_id  # noqa: SLF001

    def unexpected(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("a short-commit retry must use the canonical scalar path")

    monkeypatch.setattr(IndexStore, "_apply_common_replay_hot_change", unexpected)

    assert _commit_under_write_authority(database.manager, txn, COMMIT_LSN + 1) == len(keys)
    assert not database.exact.stale
    assert database.exact.pending(txn) == ()
    assert {entry.ref for entry in database.exact.walk()} == set(refs)


def test_failed_authority_is_revoked_before_a_direct_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(budget_pages=128)
    txn = TransactionDouble(txn_id=302)
    keys = _colliding_keys(database.exact, 4, prefix="revoke")
    _stage_inserts(database.exact, txn, keys)

    with monkeypatch.context() as failure:
        failure.setattr(
            IndexStore,
            "_apply_common_replay_hot_change",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                GrafxIndexError("revoke me", field="injected")
            ),
        )
        with pytest.raises(GrafxIndexError, match="revoke me"):
            _commit_under_write_authority(database.manager, txn)

    def unexpected(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("the failed call leaked its write authority seal")

    monkeypatch.setattr(IndexStore, "_apply_common_replay_hot_change", unexpected)

    assert database.exact.commit(txn, COMMIT_LSN + 1) == len(keys)


def test_successful_authority_does_not_leak_to_reused_transaction_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(budget_pages=128)
    first = TransactionDouble(txn_id=303)
    _stage_inserts(database.exact, first, _colliding_keys(database.exact, 2, prefix="first"))
    assert _commit_under_write_authority(database.manager, first) == 2

    second = TransactionDouble(txn_id=303)
    keys = _colliding_keys(database.exact, 2, prefix="second")
    _stage_inserts(database.exact, second, keys, first_ref=20)

    def unexpected(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("write authority leaked to a new staged batch")

    monkeypatch.setattr(IndexStore, "_apply_common_replay_hot_change", unexpected)

    assert database.exact.commit(second, COMMIT_LSN + 1) == 2


def test_reset_batch_retains_the_existing_empty_build_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(budget_pages=128)
    token = database.exact._claim_rebuild("focused RESET exclusion")  # noqa: SLF001
    txn = TransactionDouble(txn_id=401)
    database.exact.stage_reset(txn, COMMIT_LSN, rebuild_token=token)
    keys = _colliding_keys(database.exact, 8, prefix="reset")
    _stage_inserts(database.exact, txn, keys)

    def unexpected(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("RESET owns the existing empty-build accelerator")

    monkeypatch.setattr(IndexStore, "_prepare_common_replay_hot_bucket", unexpected)

    assert _commit_under_write_authority(database.manager, txn) == len(keys) + 1
    assert not database.exact.stale


def test_locally_stale_store_declines_live_hot_acceleration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(budget_pages=128)
    database.exact.mark_stale("focused stale exclusion")
    txn = TransactionDouble(txn_id=402)
    keys = _colliding_keys(database.exact, 4, prefix="stale")
    _stage_inserts(database.exact, txn, keys)

    def unexpected(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("a stale store must remain on its established scalar path")

    monkeypatch.setattr(IndexStore, "_apply_common_replay_hot_change", unexpected)

    assert _commit_under_write_authority(database.manager, txn) == len(keys)
    assert database.exact.stale


def test_active_deferred_rebuild_declines_live_hot_acceleration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(budget_pages=128)
    token = database.exact._claim_rebuild("focused active rebuild exclusion")  # noqa: SLF001
    rebuild = TransactionDouble(txn_id=403)
    database.exact.stage_reset(
        rebuild,
        COMMIT_LSN,
        rebuild_token=token,
        defer_clear=True,
    )
    database.exact.commit(rebuild, COMMIT_LSN)
    assert database.exact._rebuild_authority is not None  # noqa: SLF001

    following = TransactionDouble(txn_id=404)
    keys = _colliding_keys(database.exact, 4, prefix="rebuild")
    _stage_inserts(database.exact, following, keys)

    def unexpected(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("an active rebuild must retain the generation-aware scalar path")

    monkeypatch.setattr(IndexStore, "_apply_common_replay_hot_change", unexpected)

    assert _commit_under_write_authority(database.manager, following, COMMIT_LSN + 1) == len(keys)
    assert database.exact._rebuild_authority is not None  # noqa: SLF001
    assert database.exact.stale
