"""CLI searches select one physical owner and never combine table-local identities."""

import hashlib
import json
import subprocess
import sys

import pytest

from okto_grafx import connect


def snapshot(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file() and p.relative_to(root).parts[0] != "control"}


@pytest.fixture(params=["pure", "numpy"])
def shared(request, tmp_path):
    root = tmp_path / "db"
    with connect(root, codec=request.param) as db:
        db.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE VECTOR SPACE s {dimension:2,metric:'cosine'}")
            tx.execute("CREATE NODE TABLE R(id INT64,v VECTOR(s),body STRING,PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE R(FROM R TO R,v VECTOR(s),body STRING)")
            tx.execute("CREATE(a:R {id:1,v:$node,body:'alpha'})-[:R {v:$edge,body:'beta'}]->(a)",
                       {"node": [1.0, 0.0], "edge": [0.0, 1.0]})
        db.create_text_index("node_text", "R", ("body",), kind="node", bucket_count=16)
        db.create_text_index("edge_text", "R", ("body",), kind="rel", bucket_count=16)
        ids = {i.table_id for i in db.vectors.indexes()}
        assert len(ids) == 2
        db.checkpoint()
    return root


def argv(root, *options):
    return ("search", "vector", str(root), "--space", "s", "--vector", "[1,0]", *options, "--json")


@pytest.mark.parametrize("kind,score", [("node", 1.0), ("rel", 0.0)])
def test_cli_qualifies_kind_and_preserves_data(cli, shared, kind, score):
    before = snapshot(shared)
    result = cli(*argv(shared, "--table", "R", "--table-kind", kind, "--k", "1"))
    assert result.code == 0, result.text
    hits = result.document["search"]["hits"]
    assert len(hits) == 1 and hits[0]["record_id"] == 1
    assert hits[0]["score"] == pytest.approx(score)
    empty = cli(*argv(shared, "--table", "R", "--table-kind", kind, "--filter", "[]"))
    assert empty.code == 0 and empty.document["search"]["hits"] == []
    assert snapshot(shared) == before


@pytest.mark.parametrize("options", [(), ("--table", "R"), ("--table-kind", "node"),
    ("--table", "Missing"), ("--table", "R", "--table-kind", "relationship")])
def test_cli_refuses_missing_ambiguous_or_invalid_owners(cli, shared, options):
    before = snapshot(shared)
    result = cli(*argv(shared, *options))
    assert result.code != 0 and result.code != 70, result.text
    if options and options[-1] == "relationship":
        assert result.code == 2 and result.document["result"] == "usage"
        assert "--table-kind must be one of: node, rel" in result.document["message"]
    else:
        assert "error" in result.document
    assert snapshot(shared) == before


def test_cli_metadata_and_real_module_entry_keep_owner_identity(cli, shared):
    help_result = cli("search", "vector", "--help")
    assert help_result.code == 0 and "--table-kind" in help_result.text
    capabilities = cli("capabilities", "--json").document
    assert capabilities["search"]["vector_owner_selection"]["table_kind"] == ["node", "rel"]
    inventory = cli("indexes", str(shared), "--json").document["vector_indexes"]
    assert len({index["table_id"] for index in inventory}) == 2
    process = subprocess.run([sys.executable, "-m", "okto_grafx.cli", *argv(shared,
        "--table=R", "--table-kind=rel")], capture_output=True, text=True, timeout=45)
    assert process.returncode == 0, process.stderr + process.stdout
    assert json.loads(process.stdout)["search"]["hits"][0]["score"] == pytest.approx(0.0)


def test_named_text_indexes_and_hybrid_node_intent_remain_separate(cli, shared):
    before = snapshot(shared)
    for index, query, other in (("node_text", "alpha", "beta"), ("edge_text", "beta", "alpha")):
        found = cli("search", "text", str(shared), "--index", index, "--query", query, "--json")
        assert found.code == 0 and len(found.document["search"]["hits"]) == 1, found.text
        missing = cli("search", "text", str(shared), "--index", index, "--query", other, "--json")
        assert missing.code == 0 and missing.document["search"]["hits"] == [], missing.text
    base = ("search", "hybrid", str(shared), "--table", "R", "--query", "alpha",
            "--space", "s", "--vector", "[1,0]", "--json")
    found = cli(*base, "--index", "node_text")
    assert found.code == 0 and len(found.document["search"]["hits"]) == 1, found.text
    wrong = cli(*base, "--index", "edge_text")
    assert wrong.code != 0 and "error" in wrong.document
    assert snapshot(shared) == before
