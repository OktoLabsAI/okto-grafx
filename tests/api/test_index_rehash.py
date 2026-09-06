"""Public growth-only rehash regressions for catalog-v2 exact indexes.

The cases stay deliberately small: they exercise the generation/publication contract rather
than using row volume as a proxy for it.  In particular they pin the one-scan v1 transition,
immutable ACTIVE-to-STALE rotation, RecordId derivation, and logical WAL identity after a
rehash.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import (
    GrafxCorruptionDetected,
    GrafxError,
    GrafxIndexError,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.index import (
    MAX_BUCKET_COUNT,
    MAX_EXPECTED_CARDINALITY,
    RECORD_ID_KEY_DERIVATION,
    TARGET_ENTRIES_PER_BUCKET,
    IndexGenerationState,
    IndexOperation,
    change_of,
    identity_index_name,
)
from okto_grafx.domain.page.file_header import (
    HEADER_PAGE_INDEX,
    FileHeader,
    FileHeaderPage,
    FileKind,
)
from okto_grafx.domain.model.catalog import (
    CATALOG_FORMAT_VERSION,
    CATALOG_LEGACY_FORMAT_VERSION,
)
from okto_grafx.domain.txn import WalRecordType


PAGE_SIZE = 512


def _seed_people(database: object) -> None:
    with database.begin("write") as schema:
        schema.execute(
            "CREATE NODE TABLE Person("
            "id INT64, email STRING, score INT64, PRIMARY KEY(id))"
        )
    with database.begin("write") as rows:
        rows.execute("CREATE (:Person {id: 1, email: 'ada@example.test', score: 7})")
        rows.execute("CREATE (:Person {id: 2, email: 'grace@example.test', score: 8})")
        rows.execute("CREATE (:Person {id: 3})")


def _seed_endpoint_graph(database: object) -> None:
    with database.begin("write") as schema:
        schema.execute("CREATE NODE TABLE A(id INT64, PRIMARY KEY(id))")
        schema.execute("CREATE NODE TABLE B(id INT64, PRIMARY KEY(id))")
        schema.execute("CREATE REL TABLE E(FROM A TO B, w INT64)")
    with database.begin("write") as rows:
        rows.execute("CREATE (:A {id: 1})")
        rows.execute("CREATE (:A {id: 2})")
        rows.execute("CREATE (:B {id: 7})")
        rows.execute("CREATE (:B {id: 8})")
        rows.execute("MATCH (a:A {id: 1}), (b:B {id: 7}) CREATE (a)-[:E {w: 17}]->(b)")
        rows.execute("MATCH (a:A {id: 2}), (b:B {id: 8}) CREATE (a)-[:E {w: 28}]->(b)")


def _semantic_entries(index: object) -> tuple[tuple[object, ...], ...]:
    """Project entries without their generation-specific bucket/slot locations."""

    return tuple(
        sorted(
            (
                entry.key,
                entry.ref,
                entry.versioned,
                entry.born_csn,
                entry.dead_csn,
            )
            for entry in index.walk()
        )
    )


def _generation_files(database: object) -> frozenset[str]:
    return frozenset(database._storage.list_files("index/g_"))


def _durable_fingerprint(database: object) -> tuple[object, ...]:
    return (
        database._catalog.read_from_pages().serialize(),
        database.wal.last_lsn,
        tuple((item.name, item.size_bytes) for item in database.storage.files),
    )


def test_v1_automatic_assisted_rehash_coactivates_v2_and_builds_target_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "database"
    with connect(root, page_size=PAGE_SIZE) as database:
        _seed_people(database)
        assert database._catalog.catalog.format_version == (
            CATALOG_LEGACY_FORMAT_VERSION
        )
        legacy = database.indexes.index("pk_Person")
        assert legacy.bucket_count == 64
        before_generation_files = _generation_files(database)

        manager_type = type(database._indexes)
        original_build = manager_type._build_detached_exact_generation
        builds: list[tuple[str, int, int, str]] = []

        def record_build(
            manager: object,
            definition: object,
            through_lsn: int,
        ) -> object:
            builds.append(
                (
                    str(definition.name),
                    int(definition.bucket_count),
                    int(definition.artifact_nonce),
                    str(definition.file),
                )
            )
            return original_build(manager, definition, through_lsn)

        monkeypatch.setattr(
            manager_type,
            "_build_detached_exact_generation",
            record_build,
        )

        source = database._indexes.active_index(
            "pk_Person", catalog=database._catalog.catalog
        )
        store_type = type(source)
        real_pressure = store_type.assisted_rehash_pressure

        def pressured(selected: object) -> tuple[int, int]:
            if selected is source:
                return source.definition.bucket_count * TARGET_ENTRIES_PER_BUCKET, 0
            return real_pressure(selected)

        monkeypatch.setattr(store_type, "assisted_rehash_pressure", pressured)

        grown = database.rehash_index_if_needed("pk_Person")
        assert grown is not None

        target_builds = [build for build in builds if build[0].lower() == "pk_person"]
        assert len(target_builds) == 1, (
            "v1 coactivation must replace, rather than first build, the intermediate "
            "64-bucket migration generation"
        )
        assert target_builds[0][1:] == (
            grown.bucket_count,
            grown.active_nonce,
            grown.file,
        )
        assert grown.bucket_count == 128
        assert grown.file != legacy.file
        assert grown.active_nonce is not None
        assert database._storage.exists(legacy.file)
        assert _generation_files(database) - before_generation_files == {grown.file}

        catalog = database._catalog.catalog
        assert catalog.format_version == CATALOG_FORMAT_VERSION
        logical = catalog.index_definition("pk_Person")
        assert len(logical.generations) == 1
        assert logical.generations[0].state is IndexGenerationState.ACTIVE
        assert logical.generations[0].artifact_nonce == grown.active_nonce
        assert database.execute(
            "MATCH (p:Person) WHERE p.id = 2 RETURN p.email"
        ).rows == (("grace@example.test",),)
        assert database.verify("all").findings == ()


def test_v2_custom_rehash_rotates_immutable_generations_and_sizing_hints(
    tmp_path: Path,
) -> None:
    root = tmp_path / "database"
    with connect(root, page_size=PAGE_SIZE) as database:
        _seed_people(database)
        first = database.create_index("by_email", "Person", ("email",), bucket_count=8)
        first_store = database._indexes.active_index(
            "by_email", catalog=database._catalog.catalog
        )
        expected_entries = _semantic_entries(first_store)

        derived = database.rehash_index(
            "by_email",
            expected_cardinality=1_000,
        )
        assert derived.bucket_count == 16
        assert derived.expected_cardinality == 1_000
        assert derived.active_nonce != first.active_nonce
        assert derived.file != first.file
        logical = database._catalog.catalog.index_definition("by_email")
        assert (
            logical.generation(first.active_nonce).state is IndexGenerationState.STALE
        )
        assert logical.generation(derived.active_nonce).state is (
            IndexGenerationState.ACTIVE
        )
        assert database._storage.exists(first.file)
        assert (
            _semantic_entries(
                database._indexes.active_index(
                    "by_email", catalog=database._catalog.catalog
                )
            )
            == expected_entries
        )

        explicit = database.rehash_index("by_email", bucket_count=32)
        assert explicit.bucket_count == 32
        assert explicit.expected_cardinality is None
        assert explicit.active_nonce not in {first.active_nonce, derived.active_nonce}
        logical = database._catalog.catalog.index_definition("by_email")
        assert [generation.state for generation in logical.generations].count(
            IndexGenerationState.ACTIVE
        ) == 1
        assert logical.generation(derived.active_nonce).state is (
            IndexGenerationState.STALE
        )
        assert logical.generation(explicit.active_nonce).state is (
            IndexGenerationState.ACTIVE
        )
        assert all(
            database._storage.exists(generation.file)
            for generation in logical.generations
        )
        assert database._storage.exists(first.file), (
            "an older retired generation may leave the bounded catalog history, but its "
            "immutable file is still retained as an orphan"
        )
        assert (
            _semantic_entries(
                database._indexes.active_index(
                    "by_email", catalog=database._catalog.catalog
                )
            )
            == expected_entries
        )
        assert database.execute(
            "MATCH (p:Person) WHERE p.email = 'grace@example.test' RETURN p.id"
        ).rows == ((2,),)
        assert database.verify("all").findings == ()
        final_nonce = explicit.active_nonce
        final_file = explicit.file

    with connect(root, page_size=PAGE_SIZE) as reopened:
        restored = reopened.indexes.index("by_email")
        assert restored.active_nonce == final_nonce
        assert restored.file == final_file
        assert restored.bucket_count == 32
        assert restored.expected_cardinality is None
        assert reopened.execute(
            "MATCH (p:Person) WHERE p.email = 'ada@example.test' RETURN p.id"
        ).rows == ((1,),)
        assert reopened.verify("all").findings == ()


def test_assisted_rehash_uses_bounded_directory_pressure_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with connect(tmp_path / "database", page_size=PAGE_SIZE) as database:
        _seed_people(database)
        first = database.create_index("by_email", "Person", ("email",), bucket_count=8)
        store = database._indexes.active_index(
            "by_email", catalog=database._catalog.catalog
        )
        storage_type = type(database._storage)
        real_page_count = storage_type.page_count
        page_count = real_page_count(database._storage, first.file)
        assert page_count == 1 + first.bucket_count

        def forbid_bucket_scan(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("the pressure probe scanned an index bucket")

        with monkeypatch.context() as patch:
            patch.setattr(type(store), "_bucket_pages", forbid_bucket_scan)
            assert database.rehash_index_if_needed("by_email") is None

        store_type = type(store)
        real_pressure = store_type.assisted_rehash_pressure

        def one_pressured_observation(selected: object) -> tuple[int, int]:
            if selected is store:
                return first.bucket_count * TARGET_ENTRIES_PER_BUCKET, 0
            return real_pressure(selected)

        monkeypatch.setattr(
            store_type,
            "assisted_rehash_pressure",
            one_pressured_observation,
        )
        grown = database.maintenance.rehash_index_if_needed("by_email")

        assert grown is not None
        assert grown.bucket_count == 16
        assert grown.file != first.file
        assert database._storage.exists(first.file)
        assert database.execute(
            "MATCH (p:Person) WHERE p.email = 'grace@example.test' RETURN p.id"
        ).rows == ((2,),)
        assert database.verify("all").findings == ()


def test_assisted_rehash_threshold_and_short_directory_are_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with connect(tmp_path / "database", page_size=PAGE_SIZE) as database:
        _seed_people(database)
        index = database.create_index("by_email", "Person", ("email",), bucket_count=8)
        storage_type = type(database._storage)
        real_page_count = storage_type.page_count

        def below_two_pages_per_bucket(storage: object, file: str) -> int:
            observed = real_page_count(storage, file)
            return (
                observed + index.bucket_count
                if file == index.file
                else observed
            )

        monkeypatch.setattr(storage_type, "page_count", below_two_pages_per_bucket)
        assert (
            database.rehash_index_if_needed(
                "by_email", overflow_pages_per_bucket=2
            )
            is None
        )

        def short_directory(storage: object, file: str) -> int:
            observed = real_page_count(storage, file)
            return index.bucket_count if file == index.file else observed

        monkeypatch.setattr(storage_type, "page_count", short_directory)
        with pytest.raises(GrafxCorruptionDetected) as refused:
            database.rehash_index_if_needed("by_email")
        assert refused.value.details["field"] == "bucket_count"
        assert refused.value.details["file"] == index.file
        assert refused.value.details["value"] == index.bucket_count
        assert database.indexes.index("by_email") == index


def test_assisted_rehash_proves_physical_header_before_returning_none(
    tmp_path: Path,
) -> None:
    with connect(tmp_path / "database", page_size=PAGE_SIZE) as database:
        _seed_people(database)
        index = database.create_index(
            "by_email", "Person", ("email",), bucket_count=8
        )
        with database._pool.pinned(index.file, HEADER_PAGE_INDEX) as page:
            FileHeaderPage.write(
                page,
                FileHeader(kind=FileKind.HEAP, page_size=PAGE_SIZE),
            )
        database._pool.flush(index.file)
        database._pool.invalidate(index.file)

        with pytest.raises(GrafxCorruptionDetected) as refused:
            database.rehash_index_if_needed("by_email")
        assert refused.value.details["kind"] == "HEAP"


def test_assisted_rehash_at_directory_ceiling_proves_identity_without_scanning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with connect(tmp_path / "database", page_size=PAGE_SIZE) as database:
        _seed_people(database)
        index = database.create_index(
            "by_email", "Person", ("email",), bucket_count=8
        )
        store = database._indexes.active_index(
            "by_email", catalog=database._catalog.catalog
        )

        def forbid_pressure_scan(*_args: object, **_kwargs: object) -> tuple[int, int]:
            raise AssertionError("a directory at the format ceiling cannot grow")

        monkeypatch.setattr(
            "okto_grafx.engine.database.MAX_BUCKET_COUNT", index.bucket_count
        )
        monkeypatch.setattr(
            type(store), "assisted_rehash_pressure", forbid_pressure_scan
        )

        assert database.rehash_index_if_needed("by_email") is None


def test_record_id_index_rehash_preserves_endpoint_identity_semantics(
    tmp_path: Path,
) -> None:
    root = tmp_path / "database"
    with connect(root, page_size=PAGE_SIZE) as database:
        _seed_endpoint_graph(database)
        database.ensure_identity_indexes()
        rid_name = identity_index_name(database._catalog.catalog.table("A").table_id)
        original = database.indexes.index(rid_name)
        original_store = database._indexes.active_index(
            rid_name, catalog=database._catalog.catalog
        )
        original_entries = _semantic_entries(original_store)

        grown = database.rehash_index(rid_name, bucket_count=128)

        assert grown.name == rid_name
        assert grown.automatic is True
        assert grown.key_derivation == RECORD_ID_KEY_DERIVATION
        assert grown.positions == ()
        assert grown.bucket_count == 128
        assert grown.active_nonce != original.active_nonce
        logical = database._catalog.catalog.index_definition(rid_name)
        assert logical.generation(original.active_nonce).state is (
            IndexGenerationState.STALE
        )
        assert (
            logical.generation(grown.active_nonce).state is IndexGenerationState.ACTIVE
        )
        assert (
            _semantic_entries(
                database._indexes.active_index(
                    rid_name, catalog=database._catalog.catalog
                )
            )
            == original_entries
        )
        assert database.execute(
            "MATCH (a:A)-[e:E]->(b:B) RETURN a.id, b.id, e.w"
        ).rows == ((1, 7, 17), (2, 8, 28))
        assert database.verify("all").findings == ()


def test_post_rehash_dml_targets_only_new_active_and_wal_keeps_logical_name(
    tmp_path: Path,
) -> None:
    with connect(tmp_path / "database", page_size=PAGE_SIZE) as database:
        _seed_people(database)
        database.create_index("by_email", "Person", ("email",), bucket_count=8)
        former_store = database._indexes.active_index(
            "by_email", catalog=database._catalog.catalog
        )
        former_entries = _semantic_entries(former_store)

        grown = database.rehash_index("by_email", bucket_count=16)
        active_store = database._indexes.active_index(
            "by_email", catalog=database._catalog.catalog
        )
        assert active_store is not former_store
        before_lsn = database._wal.last_lsn

        with database.begin("write") as writer:
            writer.execute(
                "CREATE (:Person {id: 4, email: 'barbara@example.test', score: 9})"
            )

        assert _semantic_entries(former_store) == former_entries
        assert len(_semantic_entries(active_store)) == len(former_entries) + 1
        assert database.execute(
            "MATCH (p:Person) WHERE p.email = 'barbara@example.test' RETURN p.id"
        ).rows == ((4,),)

        index_changes = tuple(
            change_of(record)
            for record in database._wal.read_from(before_lsn)
            if record.record_type == int(WalRecordType.INDEX_WRITE)
        )
        custom_changes = tuple(
            change for change in index_changes if change.index.lower() == "by_email"
        )
        assert len(custom_changes) == 1
        assert custom_changes[0].index == "by_email"
        assert custom_changes[0].operation is IndexOperation.INSERT
        assert grown.file not in {change.index for change in index_changes}
        assert str(grown.active_nonce) not in {change.index for change in index_changes}
        assert database.verify("all").findings == ()


def test_long_lived_read_only_verify_adopts_foreign_active_without_writing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "database"
    publisher = connect(root, page_size=PAGE_SIZE)
    reader = None
    try:
        _seed_people(publisher)
        first = publisher.create_index(
            "by_email", "Person", ("email",), bucket_count=8
        )
        publisher.checkpoint()
        reader = connect(root, page_size=PAGE_SIZE, read_only=True)
        assert reader.indexes.index("by_email").file == first.file
        assert reader.execute(
            "MATCH (p:Person) WHERE p.email = 'ada@example.test' RETURN p.id"
        ).rows == ((1,),)

        grown = publisher.rehash_index("by_email", bucket_count=16)
        publisher.checkpoint()
        files_before_adoption = tuple(reader._storage.list_files())

        assert reader.verify("all").findings == ()
        active = reader._indexes.active_index(
            "by_email", catalog=reader._catalog.catalog
        )
        assert active.file == grown.file
        assert active.definition.artifact_nonce == grown.active_nonce
        assert tuple(reader._storage.list_files()) == files_before_adoption
        assert reader.execute(
            "MATCH (p:Person) WHERE p.email = 'ada@example.test' RETURN p.id"
        ).rows == ((1,),)
    finally:
        if reader is not None:
            reader.close()
        publisher.close()


def test_missing_foreign_active_generation_keeps_read_boundary_fail_closed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "database"
    publisher = connect(root, page_size=PAGE_SIZE)
    observer = None
    try:
        _seed_people(publisher)
        publisher.create_index("by_email", "Person", ("email",), bucket_count=8)
        observer = connect(root, page_size=PAGE_SIZE)
        assert observer.execute(
            "MATCH (p:Person) WHERE p.email = 'ada@example.test' RETURN p.id"
        ).rows == ((1,),)

        grown = publisher.rehash_index("by_email", bucket_count=16)
        observer._pool.invalidate(grown.file)
        publisher._pool.invalidate(grown.file)
        publisher._storage.remove(grown.file)

        for _attempt in range(2):
            with pytest.raises(GrafxIndexError) as refused:
                observer.begin("read")
            assert refused.value.details["field"] == "file"
            assert refused.value.details["file"] == grown.file
            assert observer._transactions.open_transactions == 0
            assert not observer._storage.exists(grown.file)
    finally:
        if observer is not None:
            observer.close()
        publisher.close()


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"bucket_count": 16, "expected_cardinality": 1_000},
        {"bucket_count": 8},
        {"bucket_count": 7},
        {"bucket_count": 0},
        {"bucket_count": True},
        {"bucket_count": MAX_BUCKET_COUNT + 1},
        {"expected_cardinality": 512},
        {"expected_cardinality": MAX_EXPECTED_CARDINALITY + 1},
    ],
)
def test_refused_rehash_sizing_never_changes_durable_authority(
    tmp_path: Path,
    kwargs: dict[str, object],
) -> None:
    with connect(tmp_path / "database", page_size=PAGE_SIZE) as database:
        _seed_people(database)
        database.create_index("by_email", "Person", ("email",), bucket_count=8)
        before = _durable_fingerprint(database)

        with pytest.raises(GrafxError):
            database.rehash_index("by_email", **kwargs)

        assert _durable_fingerprint(database) == before


def test_unknown_specialized_and_read_only_rehashes_are_typed_non_mutations(
    tmp_path: Path,
) -> None:
    root = tmp_path / "database"
    with connect(root, page_size=PAGE_SIZE) as database:
        with database.begin("write") as schema:
            schema.execute("CREATE VECTOR SPACE s {dimension: 2, metric: 'cosine'}")
            schema.execute(
                "CREATE NODE TABLE Item(id INT64, embedding VECTOR(s), PRIMARY KEY(id))"
            )
        database.ensure_identity_indexes()
        before = _durable_fingerprint(database)

        for name in ("missing", "vector_Item_s"):
            with pytest.raises(GrafxError):
                database.rehash_index(name, bucket_count=128)
            assert _durable_fingerprint(database) == before
        with pytest.raises(GrafxUnsupportedOperation) as assisted_proximity:
            database.rehash_index_if_needed("vector_Item_s")
        assert assisted_proximity.value.details["field"] == "visibility"
        assert assisted_proximity.value.details["value"] == "proximity"
        assert _durable_fingerprint(database) == before
        database.checkpoint()

    with connect(root, page_size=PAGE_SIZE, read_only=True) as reader:
        before = _durable_fingerprint(reader)
        with pytest.raises(GrafxUnsupportedOperation) as refused:
            reader.rehash_index("pk_Item", bucket_count=128)
        assert refused.value.details["field"] == "read_only"
        with pytest.raises(GrafxUnsupportedOperation) as assisted:
            reader.rehash_index_if_needed("pk_Item")
        assert assisted.value.details["field"] == "read_only"
        assert _durable_fingerprint(reader) == before


def test_assisted_rehash_reassesses_after_concurrent_growth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "database"
    publisher = connect(root, page_size=PAGE_SIZE)
    observer = None
    try:
        _seed_people(publisher)
        first = publisher.create_index(
            "by_email", "Person", ("email",), bucket_count=8
        )
        publisher.checkpoint()
        observer = connect(root, page_size=PAGE_SIZE)
        observed_store = observer._indexes.active_index(
            "by_email", catalog=observer._catalog.catalog
        )
        store_type = type(observed_store)
        real_pressure = store_type.assisted_rehash_pressure

        def pressured(selected: object) -> tuple[int, int]:
            if selected is observed_store:
                return first.bucket_count * TARGET_ENTRIES_PER_BUCKET, 0
            return real_pressure(selected)

        monkeypatch.setattr(store_type, "assisted_rehash_pressure", pressured)
        database_type = type(observer)
        real_rehash = database_type.rehash_index
        interposed = False

        def publish_before_observer(
            selected: object,
            name: str,
            *,
            bucket_count: int | None = None,
            expected_cardinality: int | None = None,
        ) -> object:
            nonlocal interposed
            if selected is observer and not interposed:
                interposed = True
                publisher.rehash_index("by_email", bucket_count=16)
            return real_rehash(
                selected,
                name,
                bucket_count=bucket_count,
                expected_cardinality=expected_cardinality,
            )

        monkeypatch.setattr(database_type, "rehash_index", publish_before_observer)

        with pytest.raises(GrafxIndexError):
            observer.rehash_index_if_needed("by_email")
        assert interposed is True
        assert publisher.indexes.index("by_email").bucket_count == 16
        assert publisher.execute(
            "MATCH (p:Person) WHERE p.email = 'grace@example.test' RETURN p.id"
        ).rows == ((2,),)
        assert publisher.verify("all").findings == ()
    finally:
        if observer is not None:
            observer.close()
        publisher.close()
