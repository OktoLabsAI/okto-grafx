"""Bounded churn/RSS/snapshot attribution in temporary stores, never live data."""
from __future__ import annotations

from contextlib import ExitStack
import gc
import json
from pathlib import Path
import tempfile
import time

import psutil
import okto_grafx
from okto_grafx import connect


def footprint(root):
    groups = {}
    for path in root.rglob("*"):
        if path.is_file():
            group = path.relative_to(root).parts[0]
            groups[group] = groups.get(group, 0) + path.stat().st_size
    return groups


def sample(root, handles):
    process = psutil.Process()
    with connect(root) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE Item(id INT64, value STRING, category INT64, PRIMARY KEY(id))")
        with db.begin("write") as tx:
            for i in range(64):
                tx.execute("CREATE (:Item {id:$id,value:$v,category:0})", {"id": i, "v": "old"})
        db.create_index("category_idx", "Item", ("category",))
        db.checkpoint()
    observations = []
    with ExitStack() as stack:
        participants = [stack.enter_context(connect(root)) for _ in range(handles)]
        writer = participants[0]
        pinned_db = stack.enter_context(connect(root))
        pinned = stack.enter_context(pinned_db.begin("read"))
        assert pinned.execute("MATCH (n:Item {id:0}) RETURN n.value").rows == (("old",),)
        for cycle in range(12):
            started = time.perf_counter()
            payload = f"{cycle:02}:" + "x" * 5000
            with writer.begin("write") as tx:
                for i in range(8):
                    tx.execute("MATCH (n:Item {id:$id}) SET n.value=$v,n.category=$category",
                               {"id": i, "v": payload, "category": cycle + 1})
                tx.execute("CREATE (:Item {id:999,value:'ephemeral',category:99})")
            with writer.begin("write") as tx:
                tx.execute("MATCH (n:Item {id:999}) DELETE n")
            for participant in participants:
                assert participant.execute("MATCH (n:Item {id:0}) RETURN n.value").rows == ((payload,),)
                for shape in range(24):
                    assert participant.execute(f"RETURN $v AS column_{cycle * 24 + shape}",
                                               {"v": shape}).rows == ((shape,),)
                assert participant._queries._owner_budget._used_bytes == 0
                assert len(participant._queries._plan_cache) <= 256
                assert participant._queries._plan_cache_bytes <= 32 * 1024 * 1024
            assert pinned.execute("MATCH (n:Item {id:0}) RETURN n.value").rows == (("old",),)
            gc.collect()
            observations.append({"cycle": cycle, "wall_ms": round((time.perf_counter()-started)*1000, 3),
                                 "rss_bytes": process.memory_info().rss,
                                 "disk_bytes_by_root": footprint(root),
                                 "plan_entries": [len(p._queries._plan_cache) for p in participants],
                                 "plan_tariff_bytes": [p._queries._plan_cache_bytes for p in participants]})
    with connect(root) as db:
        assert db.execute("MATCH (n:Item) RETURN count(n)").rows == ((64,),)
        assert db.execute("MATCH (n:Item) WHERE n.category=12 RETURN count(n)").rows == ((8,),)
        assert db.verify("all").findings == ()
        db.checkpoint()
    gc.collect()
    return {"active_handles": handles, "extra_pinned_reader": 1, "cycles": observations,
            "after_close_rss_bytes": process.memory_info().rss,
            "disk_after_checkpoint": footprint(root),
            "process_peak_working_set_bytes": getattr(process.memory_info(), "peak_wset", None)}


def main():
    assert Path(okto_grafx.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[2] / "src")
    with tempfile.TemporaryDirectory(prefix="grafx-v005-lifetime-") as raw:
        result = {"version": okto_grafx.__version__,
                  "scope": "12 overflow/index update cycles; 1/2/4 handles plus a pinned reader",
                  "samples": [sample(Path(raw) / str(n), n) for n in (1, 2, 4)]}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
