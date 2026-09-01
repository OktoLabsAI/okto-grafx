"""Atomicity gates for vector DDL effects that precede the transaction journal."""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxIndexError
from okto_grafx.engine.vector_engine import VectorEngine


def test_a_failed_vector_attach_returns_no_unjournaled_registry_or_map_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fallible publication inside attach cannot strand effects QueryEngine never received."""
    database = connect(tmp_path / "vector-attach-atomicity", page_size=512)
    transaction = database.begin("write")
    engine_type = type(database._vectors)
    original_publish = engine_type._publish_space_metrics

    def refuse_attached_map(engine: VectorEngine) -> None:
        if engine is database._vectors and "s" in database._vectors._by_space:
            raise GrafxIndexError(
                "Injected vector attach publication refusal.",
                field="metrics",
                index="vector_Person_s",
            )
        original_publish(engine)

    try:
        transaction.execute("CREATE VECTOR SPACE s {dimension: 2, metric: 'cosine'}")
        monkeypatch.setattr(engine_type, "_publish_space_metrics", refuse_attached_map)

        with pytest.raises(GrafxIndexError) as refused:
            transaction.execute(
                "CREATE NODE TABLE Person("
                "id INT64, embedding VECTOR(s), PRIMARY KEY(id))"
            )

        assert refused.value.details["field"] == "metrics"
        assert database._indexes.indexes() == ()
        assert database._indexes._artifact_claims == {}
        assert database._indexes._schema_observed == {}
        assert database._vectors._by_space == {}
        assert database._vectors._map_claims == {}
        assert database._vectors._map_epochs == {}
        assert database._vectors._durable_by_space == {}

        transaction.rollback()
        assert database._indexes.indexes() == ()
        assert database._indexes._artifact_claims == {}
        assert database._vectors._by_space == {}
        assert database._vectors._map_claims == {}
        assert database._vectors._map_epochs == {}
    finally:
        if transaction.active:
            transaction.rollback()
        database.close()


def test_failed_outer_attach_preserves_an_identical_reentrant_adopter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cleanup is bound to one map epoch, even when the replacement is the same object."""
    database = connect(tmp_path / "vector-attach-reentrant", page_size=512)
    transaction = database.begin("write")
    engine_type = type(database._vectors)
    original_attach = engine_type._attach_speculative
    original_publish = engine_type._publish_space_metrics
    requested: tuple[object, str, object] | None = None
    inner_effect: tuple[object, object | None, object | None, object] | None = None
    fired = False

    def capture_outer_request(
        engine: VectorEngine,
        table: object,
        space_name: str,
        catalog: object,
    ) -> tuple[object, object | None, object | None, object]:
        nonlocal requested
        if engine is database._vectors:
            requested = (table, space_name, catalog)
        return original_attach(engine, table, space_name, catalog)

    def install_identical_adopter_then_refuse(engine: VectorEngine) -> None:
        nonlocal fired, inner_effect
        if engine is not database._vectors:
            original_publish(engine)
            return
        if fired:
            return
        fired = True
        assert requested is not None
        inner_effect = original_attach(engine, *requested)
        raise GrafxIndexError(
            "Outer vector publication failed after an identical re-entrant adoption.",
            field="metrics_reentrant",
            index="vector_Person_s",
        )

    def physical_pages(file: str) -> tuple[bytes, ...]:
        storage = database._pool.storage
        return tuple(
            storage.read_page(file, page_index)
            for page_index in range(storage.page_count(file))
        )

    try:
        transaction.execute("CREATE VECTOR SPACE s {dimension: 2, metric: 'cosine'}")
        monkeypatch.setattr(engine_type, "_attach_speculative", capture_outer_request)
        monkeypatch.setattr(
            engine_type,
            "_publish_space_metrics",
            install_identical_adopter_then_refuse,
        )

        with pytest.raises(GrafxIndexError) as refused:
            transaction.execute(
                "CREATE NODE TABLE Person("
                "id INT64, embedding VECTOR(s), PRIMARY KEY(id))"
            )

        assert refused.value.details["field"] == "metrics_reentrant"
        assert fired is True
        assert inner_effect is not None
        index, artifact, previous, owner = inner_effect
        assert artifact is not None
        assert database._indexes.indexes() == (index,)
        assert database._indexes._artifact_claims == {index: {artifact.owner}}
        assert database._vectors._by_space == {"s": index}
        assert database._vectors._map_claims == {"s": {index: {owner}}}
        assert set(database._vectors._map_epochs) == {"s"}
        assert set(database._vectors._maintained_at) == {"s"}
        assert database._vectors._durable_by_space == {}
        pages_before_settlement = physical_pages(index.file)

        transaction.rollback()
        assert database._indexes.indexes() == (index,)
        assert database._vectors._by_space == {"s": index}

        assert database._vectors._settle_attachment(
            "s",
            expected=index,
            owner=owner,
            previous=previous,
            committed=False,
        )
        assert database._indexes.settle_speculative(artifact, committed=False)
        assert database._indexes.indexes() == ()
        assert database._indexes._artifact_claims == {}
        assert database._indexes._heap_cache_certificates == {}
        assert database._vectors._by_space == {}
        assert database._vectors._map_claims == {}
        assert database._vectors._map_epochs == {}
        assert database._vectors._maintained_at == {}
        assert database._vectors._durable_by_space == {}
        assert database._pool.storage.exists(index.file)
        assert physical_pages(index.file) == pages_before_settlement
    finally:
        if transaction.active:
            transaction.rollback()
        database.close()
