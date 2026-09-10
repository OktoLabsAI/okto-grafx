"""Persisted native FTS: ranking, snapshots, writes, bounds, rebuild and recovery."""

import pytest

from okto_grafx import CancellationToken, TextIndexOptions, TextSearchLimits, connect
from okto_grafx.domain.index.fulltext import analyze, decode_options
from okto_grafx.domain.vector.filter import RecordIdFilter
from okto_grafx.errors import (
    GrafxConfigurationError,
    GrafxQueryBudgetExceeded,
    GrafxQueryCancelled,
)


def seed(root):
    """Fixed corpus; no external embedding or NLP provider participates."""
    db = connect(root)
    with db.begin() as tx:
        tx.execute(
            "CREATE NODE TABLE Doc(id INT64, title STRING, body STRING, PRIMARY KEY(id))"
        )
        tx.execute(
            "CREATE (:Doc {id:1, title:'Graph WAL', body:'durable graph graph'})"
        )
        tx.execute(
            "CREATE (:Doc {id:2, title:'Recovery', body:'WAL replay durability'})"
        )
        tx.execute("CREATE (:Doc {id:3, title:'Unrelated', body:null})")
    db.create_text_index(
        "docs_text",
        "Doc",
        ("title", "body"),
        options=TextIndexOptions(field_weights=(2.0, 1.0)),
        bucket_count=4,
    )
    return db


def test_persisted_search_ranking_and_filters(tmp_path):
    with seed(tmp_path / "db") as db:
        result = db.search_text(index="docs_text", query="GRAPH")
        assert [h.record_id for h in result.hits] == [1]
        assert result.hits[0].matched_fields == ("body", "title")
        assert result.regime == "exact_index" and result.corpus_documents == 3
        wal = db.search_text(index="docs_text", query="WAL")
        assert len(wal.hits) == 2
        assert wal.hits[0].record_id == 1
        assert (
            db.search_text(
                index="docs_text", query="WAL", filter=RecordIdFilter.of([2])
            )
            .hits[0]
            .record_id
            == 2
        )
        assert not db.search_text(
            index="docs_text", query="WAL", filter=RecordIdFilter.of([])
        ).hits
        assert not db.verify("all").findings
    with connect(tmp_path / "db") as reopened:
        assert (
            reopened.search_text(index="docs_text", query="graph").hits == result.hits
        )
        assert not reopened.verify("all").findings


def test_update_delete_rollback_old_reader_rebuild(tmp_path):
    with seed(tmp_path / "db") as db, db.begin("read") as old:
        before = db.search_text(old, index="docs_text", query="graph")
        with db.begin() as tx:
            tx.execute("MATCH (d:Doc {id:1}) SET d.title='Changed', d.body='newterm'")
        assert not db.search_text(index="docs_text", query="graph").hits
        assert db.search_text(old, index="docs_text", query="graph").hits == before.hits
        assert db.search_text(index="docs_text", query="newterm").hits[0].record_id == 1
        tx = db.begin()
        tx.execute("MATCH (d:Doc {id:2}) SET d.body='rolledback'")
        tx.rollback()
        assert not db.search_text(index="docs_text", query="rolledback").hits
        with db.begin() as tx:
            tx.execute("MATCH (d:Doc {id:2}) DELETE d")
        assert not db.search_text(index="docs_text", query="WAL").hits
        assert len(db.search_text(old, index="docs_text", query="WAL").hits) == 2
        db.rebuild_index("docs_text")
        assert db.search_text(old, index="docs_text", query="graph").hits == before.hits
        assert not db.verify("all").findings


@pytest.mark.parametrize(
    "name,text,expected",
    [
        ("standard", "Hello, WAL! ação", ("hello", "wal", "ação")),
        ("keyword", "  pkg.Symbol  ", ("pkg.symbol",)),
        ("whitespace", "Graph.WAL  code_path", ("graph.wal", "code_path")),
        (
            "code_identifier",
            "HTTPServer2 pkg::MyType snake_case",
            (
                "httpserver2",
                "http",
                "server",
                "2",
                "pkg::mytype",
                "pkg",
                "my",
                "type",
                "snake_case",
                "snake",
                "case",
            ),
        ),
    ],
)
def test_analyzer_identity_and_technical_corpus(name, text, expected):
    options = TextIndexOptions(analyzer=name)
    assert analyze(text, options) == expected
    assert decode_options(options.derivation()) == options


def test_cancel_budgets_and_unsupported_options(tmp_path):
    with seed(tmp_path / "db") as db:
        token = CancellationToken()
        token.cancel()
        with pytest.raises(GrafxQueryCancelled):
            db.search_text(index="docs_text", query="graph", cancellation=token)
        with pytest.raises(GrafxQueryBudgetExceeded):
            db.search_text(
                index="docs_text",
                query="graph",
                limits=TextSearchLimits(max_postings=1),
            )
        with pytest.raises(GrafxQueryBudgetExceeded):
            db.search_text(
                index="docs_text",
                query="graph WAL",
                limits=TextSearchLimits(max_query_tokens=1),
            )
        with pytest.raises(GrafxConfigurationError):
            db.create_text_index("bad", "Doc", ("id",))
        assert db.search_text(index="docs_text", query="graph").hits
    with pytest.raises(GrafxConfigurationError):
        TextIndexOptions(analyzer_version=2)


def test_foreign_handle_updates_and_reopen(tmp_path):
    with seed(tmp_path / "db") as reader, connect(tmp_path / "db") as writer:
        assert not reader.search_text(index="docs_text", query="foreign").hits
        with writer.begin() as tx:
            tx.execute("CREATE (:Doc {id:4, title:'foreign', body:'other writer'})")
        assert (
            reader.search_text(index="docs_text", query="foreign").hits[0].record_id
            == 4
        )
        reader.checkpoint()
    with connect(tmp_path / "db", read_only=True) as db:
        assert db.search_text(index="docs_text", query="foreign").hits[0].record_id == 4


def test_failed_build_and_overlong_write_do_not_publish(tmp_path):
    with seed(tmp_path / "db") as db:
        with pytest.raises(GrafxQueryBudgetExceeded):
            db.create_text_index(
                "short", "Doc", ("body",), options=TextIndexOptions(max_token_bytes=2)
            )
        assert not db._catalog.catalog.has_index_definition("short")
        with pytest.raises(GrafxQueryBudgetExceeded):
            with db.begin() as tx:
                tx.execute(
                    "CREATE (:Doc {id:7, title:$text, body:''})", {"text": "a" * 65}
                )
        assert not db.execute("MATCH (d:Doc {id:7}) RETURN d.id").rows
        assert not db.verify("all").findings


def test_procedure_unicode_and_no_trailing_clause(tmp_path):
    from okto_grafx.errors import GrafxParseError, GrafxTransactionStateError

    with seed(tmp_path / "db") as db:
        query = "CALL grafx.search_text($index, $query, 10, $ids);"
        result = db.execute(query, {"index": "docs_text", "query": "WAL", "ids": [2]})
        assert result.columns[:2] == ("record_id", "score")
        assert [r[0] for r in result.rows] == [2]
        assert result.statistics["fulltext_exact_index"] == 1
        with pytest.raises(GrafxParseError):
            db.execute("CALL grafx.search_text('docs_text', 'WAL', 10) RETURN 1")
        with db.begin() as tx, pytest.raises(GrafxTransactionStateError):
            tx.execute("CALL grafx.search_text('docs_text', 'WAL', 10)")
        with db.begin() as tx:
            tx.execute("CREATE (:Doc {id:4, title:'AÇÃO Straße Café', body:''})")
        for term in ("ação", "STRASSE", "Cafe\u0301"):
            assert db.search_text(index="docs_text", query=term).hits[0].record_id == 4
        assert analyze("AÇAO", TextIndexOptions(case_folding="none")) == ("AÇAO",)
        assert analyze("AÇAO", TextIndexOptions(case_folding="ascii")) == ("aÇao",)


def test_text_indexes_survive_logical_and_physical_transfer(tmp_path):
    from okto_grafx.backup import create_backup, restore_backup
    from okto_grafx.transfer import export_graph, import_graph

    with seed(tmp_path / "db") as db:
        wanted = db.search_text(index="docs_text", query="graph").hits[0].score
        export_graph(db, tmp_path / "logical")
        create_backup(db, tmp_path / "physical")
    logical = import_graph(tmp_path / "logical", tmp_path / "imported")
    assert len(logical.record_id_mapping) == 3
    restore_backup(
        tmp_path / "physical", tmp_path / "restored", confirm_original_offline=True
    )
    for name in ("imported", "restored"):
        with connect(tmp_path / name) as db:
            assert (
                db.search_text(index="docs_text", query="graph").hits[0].score == wanted
            )
            assert not db.verify("all").findings


@pytest.mark.parametrize("action", ["create", "update", "delete", "rebuild"])
def test_durable_wal_crash_before_page_publication_replays_fts(tmp_path, action):
    import subprocess
    import sys
    import textwrap

    with seed(tmp_path / "db") as db:
        db.checkpoint()
    script = textwrap.dedent("""
        import os, sys
        from okto_grafx import connect
        from okto_grafx.adapters.storage_local import LocalStorageDevice
        db = connect(sys.argv[1])
        original = LocalStorageDevice.durable_barrier
        action = sys.argv[2]
        def cut(self, file):
            result = original(self, file)
            if file.startswith("wal/"):
                payload = self.read_log(file, 0, self.file_size(file))
                if action in ("delete", "rebuild") or b"crashterm" in payload:
                    os._exit(73)
            return result
        LocalStorageDevice.durable_barrier = cut
        if action == "rebuild":
            db.rebuild_index("docs_text")
        else:
            with db.begin() as tx:
                if action == "create":
                    tx.execute("CREATE (:Doc {id:4, title:'crashterm', body:'durable'})")
                elif action == "update":
                    tx.execute("MATCH (d:Doc {id:1}) SET d.title='crashterm', d.body='durable'")
                else:
                    tx.execute("MATCH (d:Doc {id:1}) DELETE d")
        os._exit(74)
    """)
    child = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "db"), action],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 73, child.stderr
    with connect(tmp_path / "db") as db:
        if action in ("create", "update"):
            assert len(db.search_text(index="docs_text", query="crashterm").hits) == 1
        if action in ("update", "delete"):
            assert not db.search_text(index="docs_text", query="graph").hits
        if action == "rebuild":
            assert db.search_text(index="docs_text", query="graph").hits
        assert not db.verify("all").findings
        db.recover()
        assert not db.verify("all").findings


def test_corrupt_posting_page_refuses_and_verifier_finds_it(tmp_path):
    from okto_grafx.errors import GrafxError

    with seed(tmp_path / "db") as db:
        db.checkpoint()
        index = db._indexes.active_index("docs_text")
        entry = next(e for e in index.walk() if e.key == b"\x01graph")
        file, page = index.file, entry.page
    path = tmp_path / "db" / file
    with path.open("r+b") as stream:
        stream.seek(page * 8192 + 200)
        byte = stream.read(1)
        stream.seek(-1, 1)
        stream.write(bytes([byte[0] ^ 1]))
    with connect(tmp_path / "db", read_only=True) as db:
        with pytest.raises(GrafxError):
            db.search_text(index="docs_text", query="graph")
        assert db.verify("all").findings


@pytest.mark.parametrize(
    "limits",
    [
        TextSearchLimits(max_candidates=1),
        TextSearchLimits(max_explanation_bytes=1),
        TextSearchLimits(max_memory_bytes=1),
    ],
)
def test_each_retention_bound_refuses_without_poisoning_reader(tmp_path, limits):
    with seed(tmp_path / "db") as db, db.begin("read") as reader:
        with pytest.raises(GrafxQueryBudgetExceeded):
            db.search_text(reader, index="docs_text", query="WAL", limits=limits)
        assert len(db.search_text(reader, index="docs_text", query="WAL").hits) == 2


def test_two_independent_writers_publish_distinct_documents(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    with seed(tmp_path / "db") as reader:
        start = Barrier(2)

        def write(number):
            with connect(tmp_path / "db") as writer:
                start.wait(timeout=10)
                with writer.begin() as tx:
                    tx.execute(
                        "CREATE (:Doc {id:$id, title:$title, body:'parallel'})",
                        {"id": number, "title": f"writer{number}"},
                    )

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(write, [4, 5]))
        assert len(reader.search_text(index="docs_text", query="parallel").hits) == 2
        assert not reader.verify("all").findings


def test_stale_generation_never_becomes_an_empty_success(tmp_path):
    from okto_grafx.errors import GrafxIndexError

    with seed(tmp_path / "db") as db:
        db._indexes.active_index("docs_text").mark_stale("injected incomplete coverage")
        with pytest.raises(GrafxIndexError):
            db.search_text(index="docs_text", query="absent")
        db.rebuild_index("docs_text")
        assert db.search_text(index="docs_text", query="graph").hits


def test_fts_writes_and_rebuild_coexist_with_commit_provenance(tmp_path):
    from okto_grafx import CommitId, CommitMetadata

    with seed(tmp_path / "db") as db:
        db.ensure_identity_indexes()
        db.enable_commit_history()
        with db.begin(metadata=CommitMetadata(actor="fts-regression")) as tx:
            tx.execute("MATCH (d:Doc {id:1}) SET d.body='journalterm'")
        info = db.lookup_commit(CommitId(db.identity.database_uuid, tx.report.csn))
        assert info.metadata.actor == "fts-regression"
        db.rebuild_index("docs_text")
        db.checkpoint()
        assert not db.verify("all").findings
    with connect(tmp_path / "db") as db:
        assert db.search_text(index="docs_text", query="journalterm").hits
        assert not db.verify("all").findings


def test_native_journal_does_not_unseal_extra_catalog_transaction_interests(
    tmp_path, monkeypatch
):
    from okto_grafx.engine.txn_manager import TransactionManager
    from okto_grafx.errors import GrafxTransactionStateError

    with seed(tmp_path / "db") as db:
        db.ensure_identity_indexes()
        db.enable_commit_history()
        original = TransactionManager._prepare_journal

        def inject(self, txn, current):
            locations = original(self, txn, current)
            txn.note_write(0xFFFFFFFFFFFFFFFE)
            return locations

        with monkeypatch.context() as change:
            change.setattr(TransactionManager, "_prepare_journal", inject)
            with pytest.raises(GrafxTransactionStateError):
                db.rebuild_index("docs_text")
        assert db.search_text(index="docs_text", query="graph").hits
        db.rebuild_index("docs_text")
        assert not db.verify("all").findings
