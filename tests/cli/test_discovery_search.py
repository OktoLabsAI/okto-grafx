"""MP-1/2 acceptance against real native search, not simulated rankings."""

import pytest

from tests.api.test_hybrid_search import seed


@pytest.fixture
def searchable(tmp_path):
    path = tmp_path / "search"
    with seed(path) as db:
        db.checkpoint()
    return str(path)


def args(kind):
    result = []
    if kind != "vector":
        result += ["--index", "text", "--query", "wal"]
    if kind != "text":
        result += ["--space", "semantic", "--vector", "[1,0]"]
    if kind == "hybrid":
        result += ["--table", "Doc"]
    return result


@pytest.mark.parametrize("kind", ["text", "vector", "hybrid"])
def test_search(cli, searchable, kind):
    result = cli("search", kind, searchable, *args(kind), "--k", "1", "--json")
    assert result.code == 0, result.text
    assert len(result.document["search"]["hits"]) == 1
    empty = cli("search", kind, searchable, *args(kind), "--filter", "[]", "--json")
    assert empty.code == 0, empty.text
    assert empty.document["search"]["hits"] == []


@pytest.mark.parametrize(
    "flags",
    [
        ("--k", "1001"),
        ("--filter", "[true]"),
        ("--filter", "{}"),
        ("--timeout-seconds", "nan"),
        ("--vector", "[NaN]"),
        ("--vector", "[true]"),
        ("--vector", "[1]"),
        ("--space", "missing"),
    ],
)
def test_invalid_search(cli, searchable, flags):
    # Replace the selected option instead of relying on duplicate-option precedence.
    options = dict(zip(args("vector")[::2], args("vector")[1::2]))
    options[flags[0]] = flags[1]
    argv = [part for pair in options.items() for part in pair]
    result = cli("search", "vector", searchable, *argv, "--json")
    assert result.code != 0, result.text
    assert "error" in result.document


def test_inventory(cli, searchable):
    schema = cli("schema", searchable, "--json").document
    assert schema["spaces"][0]["name"] == "semantic"
    result = cli("indexes", searchable, "--json")
    assert result.code == 0, result.text
    assert any(i["name"] == "text" for i in result.document["indexes"])
    assert result.document["vector_indexes"][0]["space_name"] == "semantic"
    limited = cli("indexes", searchable, "--limit", "0", "--json").document
    assert limited["truncated"] is True and limited["indexes"] == []


def test_vector_unique_table_selector_and_kind(cli, searchable):
    for options in (("--table", "Doc"), ("--table", "Doc", "--table-kind", "node")):
        result = cli("search", "vector", searchable, *args("vector"), *options, "--json")
        assert result.code == 0 and result.document["search"]["hits"], result.text


def test_capabilities_and_no_creation(cli, tmp_path):
    result = cli("capabilities", "--json")
    assert result.code == 0
    assert result.document["scope"] == "build_cli_contracts_not_store_activation"
    assert "search hybrid" in result.document["commands"]
    missing = tmp_path / "missing"
    for command in [("indexes",), ("schema",), ("search", "text")]:
        assert cli(*command, str(missing), "--create", "--json").code != 0
        assert not missing.exists()


def test_oversized_number_and_stale_handle(cli, searchable, monkeypatch):
    from okto_grafx import connect
    from okto_grafx.cli import commands

    huge = "[" + "1" + "0" * 1000 + ",0]"
    result = cli(
        "search",
        "vector",
        searchable,
        "--space",
        "semantic",
        "--vector",
        huge,
        "--json",
    )
    assert result.code != 0 and result.code != 70, result.text
    closed = connect(searchable, read_only=True)
    closed.close()
    monkeypatch.setattr(commands, "connect", lambda *a, **k: closed)
    assert cli("schema", searchable, "--json").code != 0
    assert cli("indexes", searchable, "--json").code != 0
