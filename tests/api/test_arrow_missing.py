"""The optional Arrow boundary remains callable and typed without the dependency."""

import builtins

import pytest

from okto_grafx import QueryResult, connect
from okto_grafx.arrow import to_arrow_batches, import_arrow_batches
from okto_grafx.errors import GrafxUnsupportedOperation


def test_missing_pyarrow_is_a_typed_optional_refusal(monkeypatch, tmp_path):
    original = builtins.__import__

    def without_arrow(name, *args, **kwargs):
        if name == "pyarrow":
            raise ImportError("absent optional dependency")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_arrow)
    with pytest.raises(GrafxUnsupportedOperation, match="arrow"):
        next(to_arrow_batches(QueryResult(columns=("v",), rows=((1,),)), types=("INT64",)))
    with connect(tmp_path / "db") as db, db.begin() as tx:
        with pytest.raises(GrafxUnsupportedOperation, match="arrow"):
            import_arrow_batches(tx, "RETURN $v", (), types=("INT64",))
        assert tx.active
