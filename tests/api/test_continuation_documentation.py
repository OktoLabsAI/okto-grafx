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
        exec(compile(snippets("GRAPH_PROJECTIONS.md")[1], "algorithm documentation", "exec"), scope)
        assert scope["strong"] == scope["graph"].nodes
        assert scope["ranks"].converged
        assert scope["path"].found
        assert scope["cores"] == (0,)
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


@pytest.mark.optional_dependency("pyarrow")
def test_arrow_vector_recipe():
    pytest.importorskip("pyarrow")
    with connect(":memory:") as db:
        with db.begin() as tx:
            tx.execute("CREATE VECTOR SPACE emb {dimension:2,metric:'cosine'}")
            tx.execute("CREATE NODE TABLE Documents(id INT64,v VECTOR(emb),PRIMARY KEY(id))")
            tx.execute("CREATE NODE TABLE Copies(id INT64,v VECTOR(emb),PRIMARY KEY(id))")
            tx.execute("CREATE (:Documents {id:1,v:[1.0,0.0]})")
        scope = {"db": db}
        recipes = [code for code in snippets("EXTENSIONS_AND_ARROW.md") if "ArrowVectorType" in code]
        assert len(recipes) == 1, "Execute the unique printed vector recipe, not a shifted snippet index"
        exec(compile(recipes[0], "Arrow vector documentation", "exec"), scope)
        assert scope["report"].statements == 1
        assert db.execute("MATCH (n:Copies) RETURN n.v").rows == db.execute("MATCH (n:Documents) RETURN n.v").rows


def test_weighted_projection_recipe():
    with connect(":memory:") as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE Person(id INT64,PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE KNOWS(FROM Person TO Person,cost DOUBLE)")
            tx.execute("CREATE (:Person {id:1})")
            tx.execute("MATCH (n:Person) CREATE (n)-[:KNOWS {cost:2.0}]->(n)")
        scope = {"db": db}
        exec(compile(snippets("GRAPH_PROJECTIONS.md")[2], "weighted documentation", "exec"), scope)
        assert scope["route"].found and scope["personalized"].converged
        exec(compile(snippets("GRAPH_PROJECTIONS.md")[3], "prepared documentation", "exec"), scope)
        assert scope["rank_again"].converged and scope["communities"].converged


@pytest.mark.optional_dependency("pyarrow")
@pytest.mark.optional_dependency("pandas")
def test_tabular_parquet_recipes(tmp_path):
    pytest.importorskip("pyarrow")
    pytest.importorskip("pandas")
    with connect(":memory:") as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE Document(id INT64,title STRING,PRIMARY KEY(id))")
        scope = {"db": db, "export_root": tmp_path}
        for code in snippets("TABULAR_AND_PARQUET.md"):
            exec(compile(code, "tabular documentation", "exec"), scope)
        assert db.execute("MATCH (d:Document) RETURN d.id ORDER BY d.id").rows == ((1,), (2,), (3,), (4,))


@pytest.mark.optional_dependency("networkx")
@pytest.mark.optional_dependency("pyarrow")
def test_graph_exchange_recipes():
    pytest.importorskip("networkx")
    pytest.importorskip("pyarrow")
    from tests.api.test_projection_algorithms import picture
    scope = {"graph": picture(3, [(0, 1), (1, 2)])}
    for code in snippets("GRAPH_EXCHANGE.md"):
        exec(compile(code, "graph exchange documentation", "exec"), scope)


@pytest.mark.optional_dependency("polars")
@pytest.mark.optional_dependency("pyarrow")
def test_polars_recipe():
    pytest.importorskip("polars")
    pytest.importorskip("pyarrow")
    with connect(":memory:") as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE Document(id INT64,title STRING,PRIMARY KEY(id))")
        scope = {"db": db}
        exec(compile(snippets("POLARS_RECIPE.md")[0], "polars documentation", "exec"), scope)
        assert db.execute("MATCH (n:Document) RETURN n.id").rows == ((5,),)


def test_local_text_recipes(tmp_path):
    (tmp_path / "input.csv").write_text('id,title\n1,CSV\n', encoding="utf-8")
    (tmp_path / "input.jsonl").write_text('{"id":2,"title":"JSONL"}\n', encoding="utf-8")
    with connect(":memory:") as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE Document(id INT64,title STRING,PRIMARY KEY(id))")
        scope = {"db": db, "input_root": tmp_path}
        for code in snippets("LOCAL_TEXT_IMPORT.md"):
            exec(compile(code, "local text documentation", "exec"), scope)
        assert scope["row_count"] == 1
        assert db.execute("MATCH (n:Document) RETURN n.id ORDER BY n.id").rows == ((1,), (2,))
