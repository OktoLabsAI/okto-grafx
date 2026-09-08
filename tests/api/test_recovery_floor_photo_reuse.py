"""Only the unchanged clean pre-redo interval shares its heap-watermark photograph."""

from collections import Counter

import pytest

from okto_grafx import connect
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.index_manager import IndexManager, IndexStore
from okto_grafx.engine import recovery_manager as recovery_module


def _seed(path):
    with connect(path, page_size=8192) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, name STRING, PRIMARY KEY(id))")
            tx.execute("CREATE (:N {id:1, name:'before'})")
        for i in range(4):
            with db.begin("write") as tx:
                tx.execute("MATCH (n:N {id:1}) SET n.name=$name", {"name": f"v{i}"})
        db.checkpoint()


def _count_walks(monkeypatch):
    counts = Counter()
    original = HeapStore.committed_high_water

    def count(self, table):
        counts[table.name] += 1
        return original(self, table)

    monkeypatch.setattr(HeapStore, "committed_high_water", count)
    return counts


def test_clean_reopen_removes_only_one_pre_redo_walk_and_keeps_reports(tmp_path, monkeypatch):
    path = tmp_path / "db"
    _seed(path)
    counts = _count_walks(monkeypatch)
    with monkeypatch.context() as baseline:
        baseline.setattr(recovery_module, "_CANONICAL_REPLAY_FLOOR", None)
        with connect(path) as db:
            assert counts == {"N": 4}
            before = db.verify("all")
    counts.clear()
    with connect(path) as db:
        assert counts == {"N": 3}
        assert db.verify("all") == before
        assert before.findings == ()
        assert db.execute("MATCH (n:N) RETURN n.name").rows == (("v3",),)
    counts.clear()
    with connect(path) as db:
        assert counts == {"N": 3}, "a new open must not reuse the previous call's photo"


@pytest.mark.parametrize("hook", ["floor", "photo", "ledger_repair"])
def test_custom_collaborator_keeps_separate_observations(tmp_path, monkeypatch, hook):
    path = tmp_path / "db"
    _seed(path)
    counts = _count_walks(monkeypatch)
    observed = []
    owner, name = {
        "floor": (IndexManager, "check_replay_floor"),
        "photo": (IndexManager, "table_watermark_photo"),
        "ledger_repair": (recovery_module.RecoveryManager, "_repair_ledger"),
    }[hook]
    original = getattr(owner, name)

    def custom(self, *args, **kwargs):
        observed.append(kwargs)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(owner, name, custom)
    with connect(path) as db:
        assert counts == {"N": 4}
        assert observed
        if hook == "floor":
            assert "watermarks" not in observed[0]
            assert "watermarks" not in observed[1]
        assert db.execute("MATCH (n:N) RETURN n.name").rows == (("v3",),)


def test_foreign_stale_header_between_checks_is_not_cached(tmp_path, monkeypatch):
    path = tmp_path / "db"
    _seed(path)
    counts = _count_walks(monkeypatch)
    original = IndexStore.check_freshness
    introduced = False
    later_stale = []

    def observe(self, *args, **kwargs):
        nonlocal introduced
        result = original(self, *args, **kwargs)
        if not introduced and kwargs.get("persist") is False:
            introduced = True
            self.mark_stale("injected independent header verdict", persist=True)
        elif introduced and kwargs.get("persist") is True:
            later_stale.append(self.stale)
        return result

    monkeypatch.setattr(IndexStore, "check_freshness", observe)
    with connect(path) as db:
        assert counts == {"N": 3}, "the native photo path must actually be exercised"
        assert introduced and later_stale and all(later_stale)
        assert db.execute("MATCH (n:N) RETURN n.name").rows == (("v3",),)


def test_independent_writer_commit_is_seen_by_next_recovery(tmp_path, monkeypatch):
    path = tmp_path / "db"
    _seed(path)
    with connect(path) as writer:
        with writer.begin("write") as tx:
            tx.execute("MATCH (n:N {id:1}) SET n.name='after'")
        counts = _count_walks(monkeypatch)
        with connect(path) as reader:
            assert counts == {"N": 3}
            assert reader.execute("MATCH (n:N) RETURN n.name").rows == (("after",),)
            assert reader.verify("all").findings == ()


def test_damaged_ledger_keeps_separate_photos_and_normal_repair(tmp_path, monkeypatch):
    path = tmp_path / "db"
    _seed(path)
    ledger = path / "ledger" / "ledger.log"
    ledger.parent.mkdir(exist_ok=True)
    ledger.write_bytes(b"\x00" * 30)
    counts = _count_walks(monkeypatch)
    with connect(path) as db:
        assert counts == {"N": 4}
        assert ledger.read_bytes() == b""
        assert db.execute("MATCH (n:N) RETURN n.name").rows == (("v3",),)


def test_refuse_policy_retains_its_own_photo_and_does_not_repair_ledger(tmp_path, monkeypatch):
    path = tmp_path / "db"
    _seed(path)
    ledger = path / "ledger" / "ledger.log"
    ledger.parent.mkdir(exist_ok=True)
    damaged = b"\x00" * 30
    ledger.write_bytes(damaged)
    counts = _count_walks(monkeypatch)
    with connect(path, recovery_policy="refuse") as db:
        assert counts == {"N": 3}  # no persistent pre-redo pass in refuse mode
        assert ledger.read_bytes() == damaged
        assert db.execute("MATCH (n:N) RETURN n.name").rows == (("v3",),)
