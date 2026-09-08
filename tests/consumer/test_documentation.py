"""Consumer examples and coverage checks; no production database/network access."""

from __future__ import annotations

import asyncio
from pathlib import Path
import re
import runpy
import subprocess
import sys
import threading

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxError, GrafxUnsupportedOperation

ROOT = Path(__file__).resolve().parents[2]


def blocks(name: str) -> list[str]:
    return re.findall(
        r"```python\n(.*?)\n```", (ROOT / name).read_text(encoding="utf-8"), re.S
    )


@pytest.mark.parametrize("name", ["README.md", "docs/GETTING_STARTED.md"])
def test_complete_documentation_examples(name: str) -> None:
    for snippet in blocks(name):
        exec(compile(snippet, name, "exec"), {})


def test_reference_coverage_and_preservation() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools/check_documentation.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_cursor_bulk_and_snapshot_scan_recipes() -> None:
    with connect(":memory:") as db:
        with db.begin("write") as txn:
            txn.execute(
                "CREATE NODE TABLE Person(id INT64, name STRING, city STRING, PRIMARY KEY(id))"
            )

        class Row:
            def __init__(self, identifier: int) -> None:
                self.id, self.name, self.city = (
                    identifier,
                    f"person-{identifier}",
                    "London",
                )

        scope = {"db": db, "incoming_rows": [Row(i) for i in range(3)]}
        exec(compile(blocks("docs/INTEGRATION.md")[1], "bulk recipe", "exec"), scope)
        query = db.query("MATCH (p:Person) RETURN p.id ORDER BY p.id")
        with query.cursor(batch_size=2) as cursor:
            assert cursor.fetchone() == (0,)
            assert cursor.fetchmany() == ((1,), (2,))
            assert cursor.fetchone() is None
            assert cursor.closed
        with db.begin("read") as reader:
            first = reader.scan_rows_v1("Person", limit=2)
            assert len(first.rows) == 2 and first.next_cursor is not None
            second = reader.scan_rows_v1("Person", limit=2, cursor=first.next_cursor)
            assert len(second.rows) == 1


def test_vector_recipe_and_filter() -> None:
    with connect(":memory:") as db:
        scope = {"db": db}
        snippets = blocks("docs/INDEXES_AND_VECTORS.md")
        exec(compile(snippets[0], "vector recipe", "exec"), scope)
        with db.begin("read") as reader:
            page = reader.scan_rows_v1("Chunk", limit=10)
            scope["record_ids"] = [r.record_id for r in page.rows]
        exec(compile(snippets[1], "filter recipe", "exec"), scope)
        result = scope["result"]
        assert result.achieved_k == 1 and result.requested_k == 10


def test_conflict_recipe_and_ddl_rollback() -> None:
    scope: dict = {}
    exec(compile(blocks("docs/OPERATIONS.md")[0], "retry recipe", "exec"), scope)
    with connect(":memory:") as db:
        with pytest.raises(RuntimeError, match="abandon"):
            with db.begin("write") as txn:
                txn.execute("CREATE NODE TABLE Abandoned(id INT64, PRIMARY KEY(id))")
                raise RuntimeError("abandon")
        with pytest.raises(GrafxError):
            db.execute("MATCH (n:Abandoned) RETURN n.id")
        with db.begin("write") as txn:
            txn.execute("CREATE NODE TABLE Person(id INT64, PRIMARY KEY(id))")
        report = scope["write_with_conflict_retry"](
            db, "CREATE (:Person {id: $id})", {"id": 1}
        )
        assert report.durable and report.wrote


def test_query_contract_examples() -> None:
    with connect(":memory:") as db:
        with db.begin("write") as txn:
            txn.execute("CREATE NODE TABLE Decision(id INT64, PRIMARY KEY(id))")
            txn.execute("CREATE REL TABLE R(FROM Decision TO Decision)")
            txn.execute("CREATE (:Decision {id: 1})")
        assert db.execute(
            "MATCH (d:Decision) OPTIONAL MATCH (d)-[r]-() RETURN d.id, count(r)"
        ).rows == ((1, 0),)
        assert db.execute("RETURN 1 AS n UNION RETURN 1.0 AS n").rows == ((1.0,),)


def test_async_recipe(tmp_path: Path) -> None:
    with connect(tmp_path / "graph") as db:
        with db.begin("write") as txn:
            txn.execute("CREATE NODE TABLE Person(id INT64, PRIMARY KEY(id))")
            txn.execute("CREATE (:Person {id: 1})")
    scope: dict = {}
    exec(compile(blocks("docs/INTEGRATION.md")[0], "async recipe", "exec"), scope)

    async def run() -> None:
        assert (
            await scope["read_count_async"](tmp_path / "graph", asyncio.Semaphore(2))
            == 1
        )

    asyncio.run(run())


def test_async_recipe_retains_slot_through_repeated_cancellation() -> None:
    scope: dict = {}
    exec(compile(blocks("docs/INTEGRATION.md")[0], "async recipe", "exec"), scope)
    started = threading.Event()
    finish = threading.Event()

    def blocking_read(path: object) -> int:
        started.set()
        assert finish.wait(5), "test worker was not released"
        return 1

    scope["read_count"] = blocking_read

    async def run() -> None:
        slots = asyncio.Semaphore(1)
        task = asyncio.create_task(scope["read_count_async"]("unused", slots))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert slots.locked() and not task.done()
        finally:
            finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not slots.locked()

    asyncio.run(run())


def test_read_only_admission_requires_checkpoint(tmp_path: Path) -> None:
    path = tmp_path / "graph"
    with connect(path) as db:
        with db.begin("write") as txn:
            txn.execute("CREATE NODE TABLE Person(id INT64, PRIMARY KEY(id))")
            txn.execute("CREATE (:Person {id: 1})")
    with pytest.raises(GrafxUnsupportedOperation) as failure:
        connect(path, read_only=True)
    assert failure.value.details["field"] == "read_only_consistency"
    with connect(path) as db:
        db.checkpoint()
    with connect(path, read_only=True) as db:
        assert db.execute("MATCH (n:Person) RETURN count(*)").rows == ((1,),)


def test_cli_help_documents_transactional_schema() -> None:
    from okto_grafx.cli.parser import parse

    outcome = parse(["query", "--help"])
    assert "NOT undone" not in outcome.message
    assert "Schema changes share that transaction" in outcome.message


def test_documentation_check_can_load_without_engine() -> None:
    namespace = runpy.run_path(str(ROOT / "tools/generate_api_reference.py"))
    assert "Database.close_complete" in namespace["render"]()
