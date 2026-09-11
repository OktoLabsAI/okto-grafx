"""Parameter map admission under the case-sensitive query value contract."""

from types import MappingProxyType

from tests.query.stack import build_query_stack


def test_large_scalar_page_and_nested_case_sensitive_keys() -> None:
    """Bulk scalar parameters and nested maps preserve values without case folding."""
    stack = build_query_stack()
    result = stack.engine.execute(
        "RETURN size($ids), $rows[0].nested[0].id, $rows[0].nested[0].ID",
        stack.transaction(read_lsn=1000),
        {"ids": tuple(f"id-{i}" for i in range(500)), "rows": [{"nested": [{"id": 1, "ID": 2}]}]},
    )
    assert result.rows == ((500, 1, 2),)


def test_read_only_mapping_uses_the_same_case_sensitive_contract() -> None:
    """An immutable map and a dictionary have the same exact-key lookup semantics."""
    stack = build_query_stack()
    result = stack.engine.execute(
        "RETURN $map.id, $map.ID, $map.missing", stack.transaction(read_lsn=1000),
        {"map": MappingProxyType({"id": 1, "ID": 2})},
    )
    assert result.rows == ((1, 2, None),)


def test_mutation_is_bound_again_without_reusing_the_previous_map_value() -> None:
    """A cached query must bind the new call, not a previous parameter's key set."""
    row = {"id": 1}
    stack = build_query_stack()
    query = "RETURN $rows[0].id, $rows[0].ID"
    first = stack.engine.execute(query, stack.transaction(read_lsn=1000), {"rows": [row]})
    row["ID"] = 2
    second = stack.engine.execute(query, stack.transaction(read_lsn=1000), {"rows": [row]})
    assert first.rows == ((1, None),)
    assert second.rows == ((1, 2),)
