"""Startup adopts once inside its final fence; recovery remains independently proved."""
from collections import Counter
from contextlib import contextmanager

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxError
from okto_grafx.engine.index_manager import IndexManager
from okto_grafx.engine.txn_manager import TransactionManager


def seed(root):
    with connect(root, automatic_index_expected_cardinality=10) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE A(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE (:A {id:1})")
        db.checkpoint()
        return next(d.file for d in db._catalog.catalog.active_index_definitions() if d.name == "pk_A")


def test_final_admission_is_single_and_inside_schema_fence(tmp_path, monkeypatch):
    root = tmp_path / "db"
    seed(root)
    active = False
    final = Counter()
    earlier = Counter()
    section = TransactionManager.schema_artifact_section
    adopt = IndexManager.adopt_committed

    @contextmanager
    def fenced(manager, **kwargs):
        nonlocal active
        with section(manager, **kwargs):
            active = True
            try:
                yield
            finally:
                active = False

    def admission(manager, index, **kwargs):
        (final if active else earlier)[index.name] += 1
        return adopt(manager, index, **kwargs)

    monkeypatch.setattr(TransactionManager, "schema_artifact_section", fenced)
    monkeypatch.setattr(IndexManager, "adopt_committed", admission)
    with connect(root) as db:
        assert final and all(count == 1 for count in final.values())
        assert earlier  # Recovery's independent existing-only baseline was not removed.
        assert db.execute("MATCH (n:A) RETURN n.id").rows == ((1,),)


@pytest.mark.parametrize("damage", ["missing", "torn"])
def test_damage_after_recovery_still_refuses_without_recreating(tmp_path, monkeypatch, damage):
    root = tmp_path / "db"
    target = root / seed(root)
    initialize = TransactionManager.__init__

    def init(manager, *args, **kwargs):
        initialize(manager, *args, **kwargs)
        if damage == "missing":
            target.unlink()
        else:
            target.write_bytes(b"")

    monkeypatch.setattr(TransactionManager, "__init__", init)
    with pytest.raises(GrafxError):
        connect(root)
    assert not target.exists() if damage == "missing" else target.read_bytes() == b""
