"""Read-only table inventory through the real CLI and storage stack."""

from okto_grafx import __version__


def test_schema_definition(cli, database_path):
    result = cli("schema", database_path, "--json")
    assert result.code == 0
    document = result.document
    assert document["schema_version"] == 1
    assert document["read_only"] is True
    assert document["total_tables"] == document["returned_tables"] == 1
    assert document["truncated"] is False
    table = document["tables"][0]
    assert table["name"] == "Person"
    assert table["kind"] == "node"
    assert table["primary_key"] == "id"
    assert [column["type"] for column in table["columns"]] == ["INT64", "STRING"]
    assert cli("schema", database_path, "--json").document == document


def test_empty(cli, empty_database_path):
    result = cli("schema", empty_database_path, "--json")
    assert result.code == 0
    assert result.document["tables"] == []
    assert result.document["truncated"] is False


def test_zero_limit_is_explicit_truncation(cli, database_path):
    result = cli("schema", database_path, "--limit", "0", "--json")
    assert result.code == 0
    assert result.document["tables"] == []
    assert result.document["total_tables"] == 1
    assert result.document["truncated"] is True


def test_refuses_creation_and_missing_paths(cli, tmp_path):
    path = tmp_path / "absent"
    for flags in [(), ("--create",), ("--limit", "-1"), ("--write",)]:
        result = cli("schema", str(path), *flags, "--json")
        assert result.code != 0
        assert not path.exists()


def test_text_and_version(cli, database_path):
    assert "Person" in cli("schema", database_path).out
    assert __version__ == "0.0.7"
    assert "0.0.7" in cli("--version").out
