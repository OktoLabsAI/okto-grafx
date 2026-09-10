"""Explicit analyzer replacement uses detached generation publication, never in-place edits."""

import pytest

from okto_grafx import TextIndexOptions, connect
from okto_grafx.errors import GrafxConfigurationError
from test_fulltext import seed


def test_replace_options_preserves_old_snapshot_and_reopens(tmp_path):
    root = tmp_path / "db"
    with seed(root) as db:
        before = db.indexes.indexes()
        with db.begin("read") as reader:
            old = db.search_text(reader, index="docs_text", query="graph")
            db.replace_text_index("docs_text", options=TextIndexOptions(
                field_weights=(2.0, 1.0), positions=True))
            with db.begin() as writer:
                writer.execute("CREATE (:Doc {id:4, title:'graph', body:'later'})")
            assert [hit.record_id for hit in db.search_text(reader, index="docs_text", query="graph",
                return_positions=True).hits] == [hit.record_id for hit in old.hits]
        assert len(db.search_text(index="docs_text", query="graph", return_positions=True).hits) == 2
        assert db.indexes.indexes() != before
        assert db.verify().clean
        db.checkpoint()
    with connect(root, read_only=True) as db:
        assert len(db.search_text(index="docs_text", query="graph", return_positions=True).hits) == 2


def test_invalid_replacement_keeps_original_active(tmp_path):
    with seed(tmp_path / "db") as db:
        before = db.indexes.indexes()
        with pytest.raises(GrafxConfigurationError):
            db.replace_text_index("docs_text", options=TextIndexOptions(field_weights=(1.0,)))
        assert db.indexes.indexes() == before
        assert db.search_text(index="docs_text", query="graph").hits


def test_actual_analyzer_semantics_change_without_drop_gap(tmp_path):
    with seed(tmp_path / "db") as db:
        original = db.indexes.index("docs_text").definition
        with db.begin("read") as reader:
            assert db.search_text(reader, index="docs_text", query="graph").hits
            db.replace_text_index("docs_text", options=TextIndexOptions(
                analyzer="keyword", field_weights=(2.0, 1.0), case_folding="none"))
            assert db.search_text(reader, index="docs_text", query="graph").hits == ()
            assert len(db.search_text(reader, index="docs_text", query="Graph WAL").hits) == 1
        assert db.indexes.index("docs_text").definition.key_derivation != original.key_derivation
        with db.begin() as tx:
            tx.execute("CREATE (:Doc {id:8,title:'Graph WAL',body:'new'})")
        assert len(db.search_text(index="docs_text", query="Graph WAL").hits) == 2
        assert db.verify().clean
        db.checkpoint()
