"""Execute the new consumer recipes, not separate lookalike examples."""

from pathlib import Path
import re

import pytest

from okto_grafx import connect


def snippets(name):
    root = Path(__file__).resolve().parents[2]
    return re.findall(r"```python\n(.*?)\n```", (root / "docs" / name).read_text(encoding="utf-8"), re.S)


def test_projection_recipe_and_fulltext_recipe():
    with connect(":memory:") as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE Person(id INT64,PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE KNOWS(FROM Person TO Person)")
            tx.execute("CREATE (:Person {id:1})")
            tx.execute("MATCH (a:Person) CREATE (a)-[:KNOWS]->(a)")
        scope = {"db": db}
        exec(compile(snippets("GRAPH_PROJECTIONS.md")[0], "projection documentation", "exec"), scope)
        assert scope["counts"] == (2,)
        assert scope["components"] == scope["graph"].nodes
    exec(compile(snippets("FULL_TEXT_SEARCH.md")[0], "FTS documentation", "exec"), {})


@pytest.mark.optional_dependency("pyarrow")
def test_arrow_import_recipe():
    pa = pytest.importorskip("pyarrow")
    with connect(":memory:") as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE Document(id INT64,title STRING,PRIMARY KEY(id))")
        batch = pa.record_batch([pa.array([1], type=pa.int64()), pa.array(["title"])], names=["id", "title"])
        scope = {"db": db, "batches": (batch,)}
        exec(compile(snippets("EXTENSIONS_AND_ARROW.md")[1], "Arrow import documentation", "exec"), scope)
        assert scope["report"].statements == 1
        assert db.execute("MATCH (d:Document) RETURN d.title").rows == (("title",),)
