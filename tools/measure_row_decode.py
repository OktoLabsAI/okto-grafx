"""Row decode before/after: payload decoders and full engine scans, old and new in one process.

The "old" arm forces every row through the per-row oracle (``_decode_tuple``) by making the planned
decoder decline; the "new" arm is the planned decoder as shipped. The arms are interleaved round by
round inside ONE process (alternating which arm goes first), so host load drifts across both rather
than favouring one. Reported per case: the median ratio new/old of rows per second with the min..max
spread over rounds, never a single run.

Two layers:
  payload  decode_tuple / projection / landing over pre-encoded payloads of Neuron-shaped tables
           (scalar-heavy edges, string-heavy nodes, nodes with a 4096-component embedding).
  engine   a predicate scan and an edge scan through a real database (heap scan, query engine).
           While each scan runs, a second thread samples how long it waits to get the GIL back
           (a 200 us sleep loop; the gap above that is time the scan thread kept the interpreter),
           and the scan thread reports its own CPU time, a proxy for GIL-held time.

Usage: python tools/measure_row_decode.py [--rounds 7] [--dim 4096] [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import random
import statistics
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from okto_grafx import connect  # noqa: E402
from okto_grafx.domain.model import schema as S  # noqa: E402
from okto_grafx.domain.model.schema import ColumnDef, TableDef, encode_tuple  # noqa: E402
from okto_grafx.domain.model.value import Timestamp, ValueType as V, VectorValue  # noqa: E402

_REAL_FAST_ROW = S._fast_row


def use_arm(arm: str) -> None:
    S._fast_row = _REAL_FAST_ROW if arm == "new" else (lambda *args: None)


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))] if ordered else float("nan")


def text(rnd: random.Random, low: int, high: int) -> str:
    return "".join(rnd.choice("abcdefghij klmnop") for _ in range(rnd.randrange(low, high)))


def fixture_tables(dim: int) -> dict[str, TableDef]:
    def node(table_id: int, name: str, columns: tuple[ColumnDef, ...]) -> TableDef:
        return TableDef(table_id=table_id, name=name, kind="node", columns=columns, primary_key=columns[0].name)

    strings = (
        ColumnDef("id", V.STRING, nullable=False),
        ColumnDef("label", V.STRING, nullable=False),
        ColumnDef("name", V.STRING),
        ColumnDef("body", V.STRING),
        ColumnDef("src", V.STRING),
        ColumnDef("weight", V.DOUBLE, nullable=False),
        ColumnDef("count", V.INT64, nullable=False),
        ColumnDef("when", V.TIMESTAMP),
        ColumnDef("props", V.MAP),
    )
    return {
        "edge (scalars)": TableDef(
            table_id=1, name="E", kind="rel", from_table="N", to_table="N",
            columns=(
                ColumnDef("weight", V.DOUBLE, nullable=False),
                ColumnDef("kind", V.STRING, nullable=False),
                ColumnDef("seen", V.INT64, nullable=False),
                ColumnDef("flag", V.BOOL, nullable=False),
                ColumnDef("created", V.INT64, nullable=False),
            ),
        ),
        "node (strings)": node(2, "S", strings),
        "node (strings + %d-d vector)" % dim: node(
            3, "V", strings + (ColumnDef("emb", V.VECTOR_F32, vector_space="emb"),)
        ),
    }


def make_row(table: TableDef, rnd: random.Random, dim: int) -> tuple[object, ...]:
    row: list[object] = []
    for position, column in enumerate(table.columns):
        kind = column.type
        if table.kind == "rel" and position < 2:
            row.append(rnd.randrange(1, 10**6))
        elif kind is V.INT64:
            row.append(rnd.randrange(10**9))
        elif kind is V.DOUBLE:
            row.append(rnd.random())
        elif kind is V.BOOL:
            row.append(rnd.random() < 0.5)
        elif kind is V.STRING:
            row.append(text(rnd, 8, 200) if column.name != "kind" else rnd.choice(("depends", "refines", "mentions")))
        elif kind is V.TIMESTAMP:
            row.append(Timestamp(rnd.randrange(2**40)))
        elif kind is V.MAP:
            row.append({"k": rnd.randrange(99), "s": text(rnd, 5, 30)})
        elif kind is V.VECTOR_F32:
            row.append(VectorValue(tuple(rnd.random() for _ in range(dim)), 1))
        else:  # pragma: no cover - the fixtures above are closed
            raise AssertionError(kind)
    return tuple(row)


def interleaved(rounds: int, run) -> dict[str, list[float]]:
    """Run run(arm) for both arms ``rounds`` times, alternating which goes first; seconds per arm."""
    seconds: dict[str, list[float]] = {"old": [], "new": []}
    for number in range(rounds):
        for arm in (("old", "new") if number % 2 == 0 else ("new", "old")):
            use_arm(arm)
            seconds[arm].append(run(arm))
    use_arm("new")
    return seconds


def ratio_line(label: str, rows: int, seconds: dict[str, list[float]]) -> dict[str, object]:
    ratios = [old / new for old, new in zip(seconds["old"], seconds["new"])]
    result = {
        "case": label,
        "rows": rows,
        "old_rows_per_s": rows / statistics.median(seconds["old"]),
        "new_rows_per_s": rows / statistics.median(seconds["new"]),
        "ratio_median": statistics.median(ratios),
        "ratio_min": min(ratios),
        "ratio_max": max(ratios),
        "rounds": len(ratios),
    }
    print(
        "%-46s old %9.0f  new %9.0f rows/s   ratio %.2fx  (min %.2f  max %.2f, %d rounds)"
        % (label, result["old_rows_per_s"], result["new_rows_per_s"], result["ratio_median"],
           result["ratio_min"], result["ratio_max"], len(ratios))
    )
    return result


def payload_layer(rounds: int, dim: int) -> list[dict[str, object]]:
    print("== payload decoders (pre-encoded rows) ==")
    rnd = random.Random(11)
    out = []
    for name, table in fixture_tables(dim).items():
        count = 300 if "vector" in name else 3000
        payloads = [encode_tuple(table, make_row(table, rnd, dim)) for _ in range(count)]  # type: ignore[arg-type]
        positions = frozenset((len(table.columns) - 3, len(table.columns) - 2)) if "vector" not in name else frozenset((5, 6))
        forms = {
            "full": lambda b: S.decode_tuple(table, b),
            "projection": lambda b: S._decode_tuple_projection(table, b, positions),
        }
        if "vector" in name:
            forms["landing (vector validated)"] = lambda b: S.decode_tuple_landing(table, b)
        for form, decode in forms.items():
            def run(arm: str, decode=decode) -> float:
                started = time.perf_counter()
                for payload in payloads:
                    decode(payload)
                return time.perf_counter() - started

            out.append(ratio_line("%s / %s" % (name, form), count, interleaved(rounds, run)))
    return out


class GilSampler:
    """Time a 200 us sleep loop in a second thread: a long gap is the scan thread keeping the GIL."""

    def __init__(self) -> None:
        self.gaps: list[float] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        last = time.perf_counter()
        while not self._stop.is_set():
            time.sleep(0.0002)
            now = time.perf_counter()
            self.gaps.append(now - last)
            last = now

    def __enter__(self) -> "GilSampler":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join()


def engine_layer(rounds: int, dim: int, nodes: int, edges: int, switch_interval: float | None) -> list[dict[str, object]]:
    print("== engine scans (real database; GIL sampler running during each scan) ==")
    if switch_interval is not None:
        sys.setswitchinterval(switch_interval)
    print("switch interval: %.4f s" % sys.getswitchinterval())
    rnd = random.Random(5)
    out = []
    with tempfile.TemporaryDirectory(prefix="grafx-decode-") as directory:
        with connect(pathlib.Path(directory) / "db") as db:
            with db.begin() as tx:
                tx.execute("CREATE VECTOR SPACE emb {dimension:%d,metric:'cosine',storage_dtype:'float32'}" % dim)
                tx.execute(
                    "CREATE NODE TABLE Node(id INT64, label STRING, name STRING, body STRING, weight DOUBLE,"
                    " count INT64, emb VECTOR(emb), PRIMARY KEY(id))"
                )
                tx.execute("CREATE REL TABLE Edge(FROM Node TO Node, weight DOUBLE, kind STRING, seen INT64)")
            space_ref = db.catalog.catalog.space("emb").space_id
            with db.begin() as tx:
                for i in range(nodes):
                    tx.execute(
                        "CREATE (:Node {id:$i,label:$l,name:$n,body:$b,weight:$w,count:$c,emb:$e})",
                        {"i": i, "l": text(rnd, 4, 12), "n": text(rnd, 8, 40), "b": text(rnd, 80, 200),
                         "w": rnd.random(), "c": rnd.randrange(100), "e": VectorValue(tuple(rnd.random() for _ in range(dim)), space_ref)},
                    )
            with db.begin() as tx:
                for _ in range(edges):
                    tx.execute(
                        "MATCH (a:Node {id:$a}),(b:Node {id:$b}) CREATE (a)-[:Edge {weight:$w,kind:$k,seen:$s}]->(b)",
                        {"a": rnd.randrange(nodes), "b": rnd.randrange(nodes), "w": rnd.random(),
                         "k": rnd.choice(("depends", "refines")), "s": rnd.randrange(20)},
                    )
            scans = {
                "node predicate scan (weight > 0.5, count)": ("MATCH (n:Node) WHERE n.weight > 0.5 RETURN count(n)", nodes),
                "edge predicate scan (seen > 5)": ("MATCH (a:Node)-[r:Edge]->(b:Node) WHERE r.seen > 5 RETURN count(r)", edges),
                "edge scan, edge properties returned": ("MATCH (a:Node)-[r:Edge]->(b:Node) RETURN r.weight, r.kind", edges),
            }
            for label, (query, rows) in scans.items():
                gaps_by_arm: dict[str, list[float]] = {"old": [], "new": []}
                cpu_by_arm: dict[str, list[float]] = {"old": [], "new": []}

                use_arm("old")
                started = time.perf_counter()
                db.execute(query)
                repeat = max(1, int(0.4 / max(time.perf_counter() - started, 1e-4)))  # >= ~0.4 s per timed block

                def run(arm: str, query=query, repeat=repeat) -> float:
                    with GilSampler() as sampler:
                        cpu0 = time.thread_time()
                        started = time.perf_counter()
                        for _ in range(repeat):
                            db.execute(query)
                        elapsed = (time.perf_counter() - started) / repeat
                        cpu = (time.thread_time() - cpu0) / repeat
                    gaps_by_arm[arm].extend(sampler.gaps)
                    cpu_by_arm[arm].append(cpu)
                    return elapsed

                result = ratio_line(label, rows, interleaved(rounds, run))
                for arm in ("old", "new"):
                    gaps = gaps_by_arm[arm]
                    result["%s_cpu_s_median" % arm] = statistics.median(cpu_by_arm[arm])
                    result["%s_gap_p99_ms" % arm] = pct(gaps, 0.99) * 1e3
                    result["%s_gap_max_ms" % arm] = max(gaps) * 1e3 if gaps else float("nan")
                    print(
                        "    %-3s scan-thread CPU %.4f s   sampler wake gap p99 %.2f ms  max %.2f ms  (%d samples)"
                        % (arm, result["%s_cpu_s_median" % arm], result["%s_gap_p99_ms" % arm],
                           result["%s_gap_max_ms" % arm], len(gaps))
                    )
                out.append(result)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rounds", type=int, default=7)
    parser.add_argument("--dim", type=int, default=4096)
    parser.add_argument("--nodes", type=int, default=400)
    parser.add_argument("--edges", type=int, default=6000)
    parser.add_argument("--switch-interval", type=float, default=None, help="seconds; default leaves the interpreter's value")
    parser.add_argument("--json", type=pathlib.Path)
    arguments = parser.parse_args()
    report = {
        "payload": payload_layer(arguments.rounds, arguments.dim),
        "engine": engine_layer(arguments.rounds, arguments.dim, arguments.nodes, arguments.edges, arguments.switch_interval),
    }
    if arguments.json:
        arguments.json.write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
