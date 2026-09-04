"""WAL-only read views retain catalog authority only across proved-safe boundaries."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

import okto_grafx.engine.query_engine as query_module
from okto_grafx import connect
from okto_grafx.domain.model.catalog import Catalog
from okto_grafx.engine.txn_manager import TransactionManager

pytestmark = pytest.mark.timeout(30, method="thread")

_WAIT_SECONDS = 5.0
_OPTIONS = {"page_size": 512, "checkpoint_interval_records": 1_000_000}


@pytest.mark.parametrize("descriptor_revalidation", ["strict", "generation"])
@pytest.mark.parametrize("read_only", [False, True])
def test_twenty_same_token_reads_retain_catalog_and_prepared_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    descriptor_revalidation: str,
    read_only: bool,
) -> None:
    root = tmp_path / "same-token"
    with connect(
        root,
        **_OPTIONS,
        descriptor_revalidation=descriptor_revalidation,
    ):
        pass

    database = connect(
        root,
        **_OPTIONS,
        descriptor_revalidation=descriptor_revalidation,
        read_only=read_only,
    )
    try:
        text = "RETURN 1 AS value"
        assert database.execute(text).rows == ((1,),)
        resident_catalog = database._catalog.catalog
        prepared_key, prepared_plan = next(
            (key, plan)
            for key, plan in database._queries._plan_cache.items()
            if key.text == text
        )
        drop_epoch = database._pool.cache_drop_epoch(database._catalog.file)
        calls = {"serialize": 0, "deserialize": 0, "plan": 0}
        original_serialize = Catalog.serialize
        original_deserialize = Catalog.deserialize.__func__
        original_build_plan = query_module.build_plan

        def counted_serialize(catalog: Catalog) -> bytes:
            calls["serialize"] += 1
            return original_serialize(catalog)

        def counted_deserialize(catalog_type: type[Catalog], raw: bytes) -> Catalog:
            calls["deserialize"] += 1
            return original_deserialize(catalog_type, raw)

        def counted_build_plan(*args: object, **kwargs: object) -> object:
            calls["plan"] += 1
            return original_build_plan(*args, **kwargs)  # type: ignore[arg-type]

        with monkeypatch.context() as measured:
            measured.setattr(Catalog, "serialize", counted_serialize)
            measured.setattr(
                Catalog,
                "deserialize",
                classmethod(counted_deserialize),
            )
            measured.setattr(query_module, "build_plan", counted_build_plan)
            for _ in range(20):
                assert database.execute(text).rows == ((1,),)

        assert calls == {"serialize": 0, "deserialize": 0, "plan": 0}
        assert database._catalog.catalog is resident_catalog
        assert database._pool.cache_drop_epoch(database._catalog.file) == drop_epoch
        assert database._queries._plan_cache[prepared_key] is prepared_plan
    finally:
        database.close()


@pytest.mark.parametrize("descriptor_revalidation", ["strict", "generation"])
def test_own_ddl_adopts_and_retains_the_new_catalog_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    descriptor_revalidation: str,
) -> None:
    database = connect(
        tmp_path / "own-ddl",
        **_OPTIONS,
        descriptor_revalidation=descriptor_revalidation,
    )
    try:
        assert database.execute("RETURN 1").rows == ((1,),)
        with database.begin("write") as transaction:
            transaction.execute(
                "CREATE NODE TABLE P(id INT64, name STRING, PRIMARY KEY(id))"
            )
            transaction.execute("CREATE (:P {id: 1, name: 'own'})")

        authority = database._catalog.catalog
        table = authority.table("P")
        primary = database._indexes.index("pk_P")
        assert primary.definition.table_id == table.table_id
        assert primary.stale is False
        drop_epoch = database._pool.cache_drop_epoch(database._catalog.file)
        calls = {"serialize": 0, "deserialize": 0}
        original_serialize = Catalog.serialize
        original_deserialize = Catalog.deserialize.__func__

        def counted_serialize(catalog: Catalog) -> bytes:
            calls["serialize"] += 1
            return original_serialize(catalog)

        def counted_deserialize(catalog_type: type[Catalog], raw: bytes) -> Catalog:
            calls["deserialize"] += 1
            return original_deserialize(catalog_type, raw)

        with monkeypatch.context() as measured:
            measured.setattr(Catalog, "serialize", counted_serialize)
            measured.setattr(
                Catalog,
                "deserialize",
                classmethod(counted_deserialize),
            )
            assert database.execute(
                "MATCH (p:P) WHERE p.id = 1 RETURN p.id, p.name"
            ).rows == ((1, "own"),)

        assert calls == {"serialize": 0, "deserialize": 0}
        assert database._catalog.catalog is authority
        assert database._indexes.index("pk_P") is primary
        assert database._pool.cache_drop_epoch(database._catalog.file) == drop_epoch
    finally:
        database.close()


@pytest.mark.parametrize("descriptor_revalidation", ["strict", "generation"])
def test_foreign_dml_ce3_retains_the_resident_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    descriptor_revalidation: str,
) -> None:
    root = tmp_path / "foreign-dml"
    publisher = connect(
        root,
        **_OPTIONS,
        descriptor_revalidation=descriptor_revalidation,
    )
    observer = None
    try:
        with publisher.begin("write") as transaction:
            transaction.execute("CREATE NODE TABLE P(id INT64, PRIMARY KEY(id))")
            transaction.execute("CREATE (:P {id: 1})")
        observer = connect(
            root,
            **_OPTIONS,
            descriptor_revalidation=descriptor_revalidation,
        )
        assert observer.execute("MATCH (p:P) WHERE p.id = 1 RETURN p.id").rows == (
            (1,),
        )
        authority = observer._catalog.catalog
        drop_epoch = observer._pool.cache_drop_epoch(observer._catalog.file)

        with publisher.begin("write") as transaction:
            transaction.execute("CREATE (:P {id: 2})")

        calls = {"serialize": 0, "deserialize": 0}
        original_serialize = Catalog.serialize
        original_deserialize = Catalog.deserialize.__func__

        def counted_serialize(catalog: Catalog) -> bytes:
            calls["serialize"] += 1
            return original_serialize(catalog)

        def counted_deserialize(catalog_type: type[Catalog], raw: bytes) -> Catalog:
            calls["deserialize"] += 1
            return original_deserialize(catalog_type, raw)

        with monkeypatch.context() as measured:
            measured.setattr(Catalog, "serialize", counted_serialize)
            measured.setattr(
                Catalog,
                "deserialize",
                classmethod(counted_deserialize),
            )
            assert observer.execute("MATCH (p:P) WHERE p.id = 2 RETURN p.id").rows == (
                (2,),
            )

        assert calls == {"serialize": 0, "deserialize": 0}
        assert observer._catalog.catalog is authority
        assert observer._pool.cache_drop_epoch(observer._catalog.file) == drop_epoch
    finally:
        if observer is not None:
            observer.close()
        publisher.close()


@pytest.mark.parametrize("descriptor_revalidation", ["strict", "generation"])
def test_same_token_does_not_adopt_catalog_bytes_applied_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    descriptor_revalidation: str,
) -> None:
    root = tmp_path / "apply-before-publish"
    publisher = connect(
        root,
        **_OPTIONS,
        descriptor_revalidation=descriptor_revalidation,
    )
    observer = None
    writer = None
    commit_thread = None
    release_publish = threading.Event()
    try:
        with publisher.begin("write") as transaction:
            transaction.execute("CREATE NODE TABLE P(id INT64, PRIMARY KEY(id))")
        observer = connect(
            root,
            **_OPTIONS,
            descriptor_revalidation=descriptor_revalidation,
        )
        assert observer.execute("RETURN 1").rows == ((1,),)
        old_authority = observer._catalog.catalog
        assert old_authority.has_table("P")
        assert not old_authority.has_table("Q")
        drop_epoch = observer._pool.cache_drop_epoch(observer._catalog.file)

        writer = publisher.begin("write")
        writer.execute("CREATE NODE TABLE Q(id INT64, PRIMARY KEY(id))")
        writer.execute("CREATE (:Q {id: 1})")
        publish_entered = threading.Event()
        failures: list[BaseException] = []
        original_publish = TransactionManager._publish_commit_state

        def pause_after_catalog_apply(
            manager: TransactionManager,
            previous: object,
            committed: int,
            *,
            catalog_touched: bool = False,
        ) -> None:
            if manager is publisher._transactions and catalog_touched:
                publish_entered.set()
                if not release_publish.wait(_WAIT_SECONDS):
                    raise AssertionError("observer did not release catalog publication")
            original_publish(
                manager,
                previous,  # type: ignore[arg-type]
                committed,
                catalog_touched=catalog_touched,
            )

        def commit_writer() -> None:
            try:
                assert writer is not None
                writer.commit()
            except BaseException as failure:  # noqa: BLE001 - asserted by the parent thread
                failures.append(failure)

        with monkeypatch.context() as boundary:
            boundary.setattr(
                TransactionManager,
                "_publish_commit_state",
                pause_after_catalog_apply,
            )
            commit_thread = threading.Thread(
                target=commit_writer, name="catalog-publisher"
            )
            commit_thread.start()
            try:
                assert publish_entered.wait(_WAIT_SECONDS), (
                    "DDL commit did not reach the post-apply publication boundary"
                )
                assert observer.execute("RETURN 1").rows == ((1,),)
                assert observer._catalog.catalog is old_authority
                assert not old_authority.has_table("Q")
                assert (
                    observer._pool.cache_drop_epoch(observer._catalog.file)
                    == drop_epoch
                )
            finally:
                release_publish.set()
                commit_thread.join(_WAIT_SECONDS)

        assert not commit_thread.is_alive()
        assert failures == []
        assert observer.execute("MATCH (q:Q) RETURN q.id").rows == ((1,),)
        assert observer._catalog.catalog is not old_authority
        assert observer._catalog.catalog.has_table("Q")
    finally:
        release_publish.set()
        if commit_thread is not None:
            commit_thread.join(_WAIT_SECONDS)
        if writer is not None and writer.active:
            writer.rollback()
        if observer is not None:
            observer.close()
        publisher.close()
