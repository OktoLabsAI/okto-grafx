"""Atomic metadata merge before general temporal value writes are enabled."""

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxConfigurationError, GrafxUnsupportedOperation, GrafxWriteConflict
from okto_grafx.domain.model.temporal_codec import TEMPORAL_VALUES_CAPABILITY
from okto_grafx.domain.model.temporal_values import DateValue
from okto_grafx.domain.page import Page
from okto_grafx.engine.catalog_store import read_catalog_page_images
from okto_grafx.engine.txn_manager import TransactionManager


def effective(db, tx):
    images = []
    for (file, index), raw in tx._context.page_images.items():
        if file == db._transactions._file_ids.catalog_file:
            page = Page.from_bytes(raw)
            page.page_lsn = 1
            images.append((index, page.to_bytes()))
    return read_catalog_page_images(tuple(images), page_size=512, sequence=1)


def admit(db, tx):
    return db._transactions._prepare_native_row_admission(tx._context)


def setup(db):
    db.ensure_identity_indexes()
    with db.begin() as tx:
        tx.execute("CREATE NODE TABLE N(id INT64, val ANY, PRIMARY KEY(id))")
    return db._catalog.catalog.table("N")


@pytest.mark.parametrize("nested", [False, True])
def test_first_value_metadata_merge_preserves_statement_schema_and_rolls_back(tmp_path, nested):
    with connect(tmp_path / "db", page_size=512) as db:
        table = setup(db)
        before = db._catalog.read_from_pages().serialize()
        tx = db.begin()
        try:
            tx.execute("CREATE NODE TABLE Added(id INT64, PRIMARY KEY(id))")
            value = {"inside": [DateValue(2000)]} if nested else DateValue(2000)
            tx._context.stage_row_insert(table, (1, value))
            assert admit(db, tx)
            candidate = effective(db, tx)
            assert candidate.has_table("Added") and candidate.has_table("N")
            assert candidate.requires_capability(TEMPORAL_VALUES_CAPABILITY)
            assert not db._catalog.read_from_pages().requires_capability(TEMPORAL_VALUES_CAPABILITY)
            images = dict(tx._context.page_images)
            assert not admit(db, tx)
            assert tx._context.page_images == images
            assert int(tx._context.txn_id) not in db._transactions._maintenance_txns
            assert not db._transactions._temporal_activation_plans
        finally:
            tx.rollback()
        assert db._catalog.read_from_pages().serialize() == before


def test_failed_merge_restores_prior_schema_pages_and_can_retry(tmp_path, monkeypatch):
    with connect(tmp_path / "db", page_size=512) as db:
        table = setup(db)
        tx = db.begin()
        try:
            tx.execute("CREATE NODE TABLE Added(id INT64, PRIMARY KEY(id))")
            tx._context.stage_row_insert(table, (1, DateValue(2000)))
            before = (dict(tx._context.page_images), dict(tx._context._page_image_proofs),
                      set(tx._context.read_partitions), set(tx._context.write_partitions))
            original = TransactionManager._stage_page_image
            def fail(self, txn, file, index, image):
                original(self, txn, file, index, image)
                raise OSError("injected metadata merge failure")
            with monkeypatch.context() as patch:
                patch.setattr(TransactionManager, "_stage_page_image", fail)
                with pytest.raises(OSError):
                    admit(db, tx)
            assert (tx._context.page_images, tx._context._page_image_proofs,
                    tx._context.read_partitions, tx._context.write_partitions) == before
            assert not tx._context._staging_marks
            assert admit(db, tx)
        finally:
            tx.rollback()


def test_unproved_schema_image_is_not_laundered_into_trusted_admission(tmp_path):
    with connect(tmp_path / "db", page_size=512) as db:
        table = setup(db)
        tx = db.begin()
        try:
            tx.execute("CREATE NODE TABLE Added(id INT64, PRIMARY KEY(id))")
            tx._context.stage_row_insert(table, (1, DateValue(2000)))
            key = next(iter(tx._context.page_images))
            page = Page.from_bytes(tx._context.page_images[key])
            page.page_lsn += 1  # Correctly framed bytes, but not the proved image.
            tx._context.page_images[key] = page.to_bytes()
            before = dict(tx._context.page_images)
            with pytest.raises(GrafxConfigurationError) as failure:
                admit(db, tx)
            assert failure.value.details["field"] == "page_image_provenance"
            assert tx._context.page_images == before
        finally:
            tx.rollback()


def test_prepared_merge_does_not_forgive_a_concurrent_schema_commit(tmp_path):
    path = tmp_path / "db"
    with connect(path, page_size=512) as first:
        table = setup(first)
        with connect(path, page_size=512) as second:
            tx = first.begin()
            try:
                tx._context.stage_row_insert(table, (1, DateValue(2000)))
                assert admit(first, tx)
                with second.begin() as winner:
                    winner.execute("CREATE NODE TABLE Winner(id INT64, PRIMARY KEY(id))")
                with pytest.raises(GrafxWriteConflict):
                    tx.commit()
            finally:
                tx.rollback()
    with connect(path, page_size=512) as db:
        assert db._catalog.catalog.has_table("Winner")
        assert not db._catalog.catalog.requires_capability(TEMPORAL_VALUES_CAPABILITY)
        assert db.execute("MATCH (n:N) RETURN count(n)").rows == ((0,),)


def test_empty_and_cancelled_insert_have_no_admission_effect(tmp_path):
    with connect(tmp_path / "db", page_size=512) as db:
        table = setup(db)
        tx = db.begin()
        try:
            assert not admit(db, tx)
            ref = tx._context.stage_row_insert(table, (1, DateValue(2000)))
            tx._context.stage_row_delete(table, ref)
            assert not admit(db, tx)
            assert not tx._context.page_images
        finally:
            tx.rollback()
        assert not db._catalog.catalog.requires_capability(TEMPORAL_VALUES_CAPABILITY)


def test_reduced_update_is_scanned_instead_of_transient_insert_values(tmp_path):
    with connect(tmp_path / "db", page_size=512) as db:
        table = setup(db)
        tx = db.begin()
        try:
            ref = tx._context.stage_row_insert(table, (1, DateValue(2000)))
            tx._context.stage_row_update(table, ref, (1, "ordinary"))
            assert not admit(db, tx)
            tx._context.stage_row_update(table, ref, (1, {"nested": DateValue(2001)}))
            assert admit(db, tx)
        finally:
            tx.rollback()


def test_pending_relationship_endpoints_are_not_treated_as_property_values(tmp_path):
    with connect(tmp_path / "db", page_size=512) as db:
        table = setup(db)
        with db.begin() as schema:
            schema.execute("CREATE REL TABLE R(FROM N TO N, val ANY)")
        edge = db._catalog.catalog.table("R")
        tx = db.begin()
        try:
            left = tx._context.stage_row_insert(table, (1, "left"))
            right = tx._context.stage_row_insert(table, (2, "right"))
            tx._context.stage_row_insert(edge, (left, right, DateValue(2000)))
            assert admit(db, tx)
            assert effective(db, tx).requires_capability(TEMPORAL_VALUES_CAPABILITY)
        finally:
            tx.rollback()


def test_legacy_catalog_refuses_without_staging_or_implicit_migration(tmp_path):
    with connect(tmp_path / "db", page_size=512) as db:
        with db.begin() as schema:
            schema.execute("CREATE NODE TABLE N(id INT64, val STRING, PRIMARY KEY(id))")
        tx = db.begin()
        try:
            tx._context.stage_row_insert(db._catalog.catalog.table("N"), (1, DateValue(2000)))
            with pytest.raises(GrafxUnsupportedOperation):
                admit(db, tx)
            assert not tx._context.page_images
        finally:
            tx.rollback()
