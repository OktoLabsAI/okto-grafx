"""The instrument behind CF-17's traversal figures, kept in the tree so anyone can re-run them.

Builds 1800 C-nodes, 400 E-nodes and 3600 M-edges in a temporary directory, times the four
traversal shapes the register quotes, and proves index-vs-scan equality by staling the endpoint
indexes and comparing. Figures are machine-dependent; the SHAPE (flat vs growing, index vs scan)
is the claim. Run: python tools/measure_traversal.py
"""

import random
import shutil
import statistics
import sys
import tempfile
import time

import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from okto_grafx import connect

root = tempfile.mkdtemp()
db = connect(root)
with db.begin("write") as t:
    t.execute("CREATE NODE TABLE C(id INT64, PRIMARY KEY(id))")
    t.execute("CREATE NODE TABLE E(id INT64, PRIMARY KEY(id))")
    t.execute("CREATE REL TABLE M(FROM C TO E, w INT64)")
rnd = random.Random(7)
for lo in range(1, 1801, 300):
    with db.begin("write") as t:
        for i in range(lo, min(lo + 300, 1801)):
            t.execute("CREATE (:C {id: $i})", {"i": i})
with db.begin("write") as t:
    for i in range(1, 401):
        t.execute("CREATE (:E {id: $i})", {"i": i})
for lo in range(1, 1801, 300):
    with db.begin("write") as t:
        for c in range(lo, min(lo + 300, 1801)):
            for _ in range(2):
                t.execute(
                    "MATCH (c:C {id: $c}), (e:E {id: $e}) CREATE (c)-[:M {w: 1}]->(e)",
                    {"c": c, "e": rnd.randint(1, 400)},
                )

queries = [
    ("reverse hop into one entity", "MATCH (c:C)-[:M]->(e:E {id: 11}) RETURN c.id"),
    ("forward hop from one node", "MATCH (c:C {id: 7})-[:M]->(e:E) RETURN e.id"),
    ("two hops out and back", "MATCH (c:C {id: 7})-[:M]->(e:E)<-[:M]-(d:C) RETURN count(*)"),
    ("hop range *1..2", "MATCH (c:C {id: 7})-[:M*1..2]->(x) RETURN count(*)"),
]
for label, q in queries:
    samples = []
    rows = ()
    for _ in range(9):
        t0 = time.perf_counter()
        rows = db.execute(q).rows
        samples.append((time.perf_counter() - t0) * 1000)
    print(f"{label}: {statistics.median(samples):.1f} ms ({len(rows)} rows)")

fresh = sorted(db.execute("MATCH (c:C)-[:M]->(e:E {id: 11}) RETURN c.id").rows)
db.indexes.index("ef_M").mark_stale("probe")
db.indexes.index("et_M").mark_stale("probe")
scan = sorted(db.execute("MATCH (c:C)-[:M]->(e:E {id: 11}) RETURN c.id").rows)
print("hybrid == scan:", fresh == scan, f"({len(fresh)} rows)")
print("verify:", len(db.verify("all").findings))
db.close()
shutil.rmtree(root, ignore_errors=True)
