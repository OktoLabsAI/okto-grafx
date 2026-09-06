"""Each DDL statement works on a structural copy of the transaction's catalog, not a decoded one.

The number of catalog decodes a schema transaction performs must not grow with the number of
statements it holds: the first statement reads the durable pages once and every later one copies
the remembered working catalog.  The refusal semantics the copy exists for -- a refused statement
leaves no phantom table behind -- are covered by the schema and lifecycle suites; this test pins
the cost shape those suites cannot see.
"""

from __future__ import annotations

import pytest

import okto_grafx
from okto_grafx.domain.model import catalog as catalog_module
from okto_grafx.domain.model.catalog import Catalog


def _statements(count: int) -> tuple[str, ...]:
    return tuple(
        f"CREATE NODE TABLE T{index}(id INT64, name STRING, PRIMARY KEY(id))"
        for index in range(count)
    )


def _decodes_for(count: int, monkeypatch: pytest.MonkeyPatch) -> tuple[int, int]:
    """Return (Catalog.deserialize calls, _decode_table calls) inside one schema transaction."""
    round_trips = 0
    table_decodes = 0
    original_deserialize = Catalog.deserialize.__func__
    original_decode = catalog_module._decode_table

    def counted_deserialize(cls: type[Catalog], raw: bytes) -> Catalog:
        nonlocal round_trips
        round_trips += 1
        return original_deserialize(cls, raw)

    def counted_decode(*args: object, **kwargs: object) -> object:
        nonlocal table_decodes
        table_decodes += 1
        return original_decode(*args, **kwargs)

    database = okto_grafx.connect(":memory:")
    try:
        monkeypatch.setattr(Catalog, "deserialize", classmethod(counted_deserialize))
        monkeypatch.setattr(catalog_module, "_decode_table", counted_decode)
        with database.begin("write") as txn:
            for statement in _statements(count):
                txn.execute(statement)
        monkeypatch.undo()
        found = database.execute("MATCH (n:T0) RETURN count(n)").rows
    finally:
        database.close()
    assert found == ((0,),)
    return round_trips, table_decodes


def test_catalog_decodes_do_not_grow_with_the_number_of_ddl_statements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    few = _decodes_for(3, monkeypatch)
    many = _decodes_for(12, monkeypatch)

    # The round trips no longer grow with the statements of the transaction.
    assert many[0] == few[0]
    # A decoded round trip per statement would have decoded every table already created:
    # 0 + 1 + ... + 11 = 66 extra table decodes for twelve statements against 0 + 1 + 2 = 3
    # for three.  What remains is the durable page read of the first statement and of the
    # commit (a fixed number of reads per transaction, each decoding every table), so the
    # count grows with the tables of the schema, never with the statements that created them.
    assert many[1] - few[1] < 63 - 3
    assert many[1] <= 4 * 12
