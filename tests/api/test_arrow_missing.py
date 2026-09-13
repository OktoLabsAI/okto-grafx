"""The optional Arrow boundary remains callable and typed without the dependency."""

import builtins

import pytest

from okto_grafx import QueryResult, connect
from okto_grafx.arrow import ArrowDecimalType, to_arrow_batches, import_arrow_batches
from okto_grafx.errors import GrafxUnsupportedOperation


@pytest.mark.parametrize("types, value", [(("INT64",), 1), ((ArrowDecimalType(12, 4),), None)])
def test_missing_pyarrow_is_a_typed_optional_refusal(monkeypatch, tmp_path, types, value):
    original = builtins.__import__

    def without_arrow(name, *args, **kwargs):
        if name == "pyarrow":
            raise ImportError("absent optional dependency")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_arrow)
    with pytest.raises(GrafxUnsupportedOperation, match="arrow"):
        next(to_arrow_batches(QueryResult(columns=("v",), rows=((value,),)), types=types))
    with connect(tmp_path / "db") as db, db.begin() as tx:
        with pytest.raises(GrafxUnsupportedOperation, match="arrow"):
            import_arrow_batches(tx, "RETURN $v", (), types=types)
        assert tx.active
