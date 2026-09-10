"""Publication proof and snapshot pagination without a global reader lock."""

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxUnsupportedOperation
from okto_grafx.domain.page import Page
from okto_grafx.domain.txn.records import COMMIT_DIRECTORY_FILE
from okto_grafx.engine.commit_history_reader import observe_commit_catalog


def write(db, query, params=None):
    with db.begin() as tx:
        tx.execute(query, params)


def prepared(tmp_path):
    db = connect(tmp_path / "db", page_size=512)
    db.ensure_identity_indexes()
    with db.begin() as tx:
        db._transactions.prepare_commit_catalog_activation(tx._context)
    activation = db._catalog.catalog.commit_catalog_activation
    write(db, "CREATE NODE TABLE P(id INT64, PRIMARY KEY(id))")
    for i in range(5):
        write(db, "CREATE (:P {id:$id})", {"id": i})
    return db, activation


def observe(db, activation, operation, **overrides):
    args = dict(
        database_uuid=db._transactions._database_uuid, page_size=512,
        activation=activation, read_page=db._storage.read_page,
        file_size=db._storage.file_size, exists=db._storage.exists,
        published=lambda: db._transactions.published_state().last_committed_lsn,
        operation=operation, empty=(),
    )
    args.update(overrides)
    return observe_commit_catalog(**args)


def test_pages_exact_order_old_snapshot_and_foreign_append(tmp_path):
    db, activation = prepared(tmp_path)
    with db:
        sequence = db._transactions.published_state().last_committed_lsn
        all_entries = observe(db, activation, lambda store: store.history(after=0, read_lsn=sequence, limit=100))
        first = observe(db, activation, lambda store: store.history(after=0, read_lsn=sequence, limit=2))
        tail = observe(db, activation, lambda store: store.history(after=first[-1].identity.sequence, read_lsn=sequence, limit=100))
        assert first + tail == all_entries
        with connect(tmp_path / "db", page_size=512) as writer:
            write(writer, "CREATE (:P {id:99})")
        assert observe(db, activation, lambda store: store.history(after=0, read_lsn=sequence, limit=100)) == all_entries
        assert observe(db, activation, lambda store: store.verify()).entry_count > len(all_entries)


def test_retries_completed_foreign_publication_during_read(tmp_path):
    db, activation = prepared(tmp_path)
    with db, connect(tmp_path / "db", page_size=512) as writer:
        fired = False

        def read(file, index):
            nonlocal fired
            raw = db._storage.read_page(file, index)
            if not fired:
                fired = True
                write(writer, "CREATE (:P {id:99})")
            return raw

        head = observe(db, activation, lambda store: store.verify(), read_page=read)
        assert fired
        assert head.last_sequence == writer._transactions.published_state().last_committed_lsn


def test_unpublished_future_page_refuses_boundedly(tmp_path):
    db, activation = prepared(tmp_path)
    with db:
        sequence = db._transactions.published_state().last_committed_lsn
        reads = 0

        def read(file, index):
            nonlocal reads
            reads += 1
            page = Page.from_bytes(db._storage.read_page(file, index))
            page.page_lsn = sequence + 1
            return page.to_bytes()

        with pytest.raises(GrafxUnsupportedOperation) as failure:
            observe(db, activation, lambda store: store.verify(), read_page=read)
        assert failure.value.retryable
        assert reads == 4


def test_stable_crc_corruption_is_not_hidden_as_empty_or_retry(tmp_path):
    db, activation = prepared(tmp_path)
    with db:
        def read(file, index):
            raw = db._storage.read_page(file, index)
            if file == COMMIT_DIRECTORY_FILE and index == 0:
                damaged = bytearray(raw)
                damaged[-1] ^= 1
                return bytes(damaged)
            return raw

        with pytest.raises(GrafxCorruptionDetected):
            observe(db, activation, lambda store: store.verify(), read_page=read)


def test_publication_below_owned_snapshot_refuses_instead_of_false_absence(tmp_path):
    db, activation = prepared(tmp_path)
    with db:
        sequence = db._transactions.published_state().last_committed_lsn
        with pytest.raises(GrafxCorruptionDetected) as failure:
            observe(db, activation, lambda store: store.verify(), minimum_sequence=sequence + 1)
        assert failure.value.details["field"] == "published_regression"
