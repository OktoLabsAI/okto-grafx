"""Fail-closed public vector reads while their owner has staged rows."""

from __future__ import annotations

from pathlib import Path

import pytest

import okto_grafx
from okto_grafx.domain.errors import GrafxUnsupportedOperation


def test_public_vector_search_refuses_a_dirty_owner_before_searching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle = okto_grafx.connect(str(tmp_path / "db"))
    try:
        with handle.begin("write") as schema:
            schema.execute(
                "CREATE VECTOR SPACE minilm_v2 {dimension: 4, metric: 'cosine'}"
            )
            schema.execute(
                "CREATE NODE TABLE Chunk("
                "id INT64, embedding VECTOR(minilm_v2), PRIMARY KEY(id))"
            )

        writer = handle.begin("write")
        writer.execute(
            "CREATE (:Chunk {id: 1, embedding: [1.0, 0.0, 0.0, 0.0]})"
        )

        def search_must_not_run(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("dirty-owner refusal must precede the vector engine")

        monkeypatch.setattr(type(handle._vectors), "search", search_must_not_run)
        with pytest.raises(GrafxUnsupportedOperation) as raised:
            handle.search_vectors(
                writer,
                space="minilm_v2",
                query=(1.0, 0.0, 0.0, 0.0),
                k=1,
            )

        assert raised.value.details == {
            "field": "table",
            "value": "Chunk",
            "table_id": handle.catalog.catalog.table("Chunk").table_id,
            "operation": "search_vectors",
            "space": "minilm_v2",
        }
        assert writer.execute("MATCH (c:Chunk) RETURN c.id").rows == ((1,),)
        writer.rollback()
    finally:
        handle.close()
