"""RF01 synthetic workloads. Oracles derive from input records, never from Grafx.

The catalog is a set of relevant slices, not a Cartesian product. A campaign must
select its scenarios explicitly; listing a slice does not claim it was measured.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import random


@dataclass(frozen=True)
class Scenario:
    name: str
    family: str
    nodes: int = 1024
    edges_per_node: int = 1
    distribution: str = "uniform"
    seed: int = 0
    keys: tuple[int, ...] = ()
    page: int = 16
    fanout: int = 64
    write_rows: int = 32
    payload_bytes: int = 64
    writers: int = 1
    reader: str = "short"
    churn_percent: int = 0
    term: str = "common"
    fts_history: int = 8
    cache: str = "warm"
    cursor_batch_size: int = 0
    role: str = "control"
    work_counter: str | None = None
    query_memory_budget_bytes: int | None = None

    def __post_init__(self):
        if self.family not in {"constant", "keys", "pk_seek", "landing", "node_scan", "edge_scan", "page", "fanout", "write", "fts"}:
            raise ValueError("unsupported scenario family")
        if self.nodes < 1 or self.edges_per_node < 0 or self.writers not in (1, 2, 4):
            raise ValueError("invalid fixture dimensions")
        if self.distribution not in ("uniform", "hub") or self.reader not in ("short", "retained"):
            raise ValueError("invalid distribution/reader")
        if self.cache not in ("warm", "reopen") or self.churn_percent not in (0, 25):
            raise ValueError("invalid cache/churn")
        if min(self.page, self.fanout, self.write_rows, self.payload_bytes, self.cursor_batch_size) < 0:
            raise ValueError("negative fixture dimension")
        if self.role not in ("control", "target"):
            raise ValueError("invalid scenario role")
        if self.cursor_batch_size and self.family in ("write", "fts"):
            raise ValueError("cursor requires a Cypher read")
        if self.query_memory_budget_bytes is not None and self.query_memory_budget_bytes < 1:
            raise ValueError("logical memory budget must be positive")

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        value = dict(value)
        value["keys"] = tuple(value.get("keys", ()))
        return cls(**value)


def fixture_data(scenario: Scenario):
    rng = random.Random(scenario.seed)
    nodes = [{"id": i, "value": i * 2, "payload": "x" * scenario.payload_bytes,
              "body": "common rare" if i == 0 else "common usual"}
             for i in range(scenario.nodes)]
    edges = []
    count = scenario.nodes * scenario.edges_per_node
    hubs = max(1, scenario.nodes // 100)
    for i in range(count):
        source = i % scenario.nodes
        if scenario.distribution == "hub" and i < (count * 80 + 99) // 100:
            source = i % hubs
        edges.append((source, rng.randrange(scenario.nodes)))
    if scenario.family in ("fanout", "landing"):
        edges = [(0, i % scenario.nodes) for i in range(scenario.fanout)]
    return nodes, edges


def live_data(scenario: Scenario):
    nodes, edges = fixture_data(scenario)
    count = 0 if scenario.reader == "retained" else scenario.nodes * scenario.churn_percent // 100
    deleted = {i for i in range(count) if i % 2 == 1}
    nodes = [dict(row, value=-row["value"] if row["id"] < count else row["value"])
             for row in nodes if row["id"] not in deleted]
    return nodes, [(a, b) for a, b in edges if a not in deleted and b not in deleted]


def oracle(scenario: Scenario):
    nodes, edges = live_data(scenario)
    ids = {n["id"] for n in nodes}
    if scenario.family == "constant":
        return ((1,),)
    if scenario.family in ("keys", "pk_seek"):
        return tuple((i,) for i in sorted(scenario.keys) if i in ids)
    if scenario.family in ("node_scan", "page"):
        rows = tuple((i,) for i in sorted(ids))
        return rows[:scenario.page] if scenario.family == "page" else rows
    if scenario.family in ("edge_scan", "fanout", "landing"):
        return tuple(sorted(edges))
    if scenario.family == "write":
        return tuple((i, "x" * scenario.payload_bytes)
                     for i in range(scenario.write_rows * scenario.writers))
    # The measured FTS contract is the complete membership projection. Scores and
    # ranking are not silently certified by this workload (recorded in metadata).
    return tuple((row["id"] + 1,) for row in nodes
                 if scenario.term in row["body"].split())


def query_for(scenario: Scenario):
    family = scenario.family
    if family == "constant":
        return "RETURN 1 AS value", {}
    if family == "keys":
        return "UNWIND $keys AS k MATCH (n:N {id:k}) RETURN n.id ORDER BY n.id", {"keys": list(scenario.keys)}
    if family == "pk_seek":
        # Map-property bindings are already eligible in the historical baseline;
        # scalar UNWIND is deliberately retained separately as the RF02 workload.
        return "UNWIND $keys AS k MATCH (n:N {id:k.id}) RETURN n.id ORDER BY n.id", {
            "keys": [{"id": key} for key in scenario.keys]}
    if family == "landing":
        # A bounded indexed anchor plus a blocking consumer admits native batched
        # landing. This is not a streaming/LIMIT control.
        return "MATCH (a:N {id:0})-[:E]->(b:N) RETURN a.id,b.id ORDER BY a.id,b.id", {}
    if family == "page":
        return "MATCH (n:N) RETURN n.id ORDER BY n.id LIMIT $page", {"page": scenario.page}
    if family == "node_scan":
        return "MATCH (n:N) RETURN n.id ORDER BY n.id", {}
    if family in ("edge_scan", "fanout"):
        return "MATCH (a:N)-[:E]->(b:N) RETURN a.id,b.id ORDER BY a.id,b.id", {}
    if family == "write":
        return "UNWIND $rows AS r CREATE (:W {id:r.id,payload:r.payload})", {
            "rows": [{"id": i, "payload": "x" * scenario.payload_bytes}
                     for i in range(scenario.write_rows * scenario.writers)]}
    return "native search_text membership projection", {"term": scenario.term}


def contract(scenario: Scenario):
    value = {"result": "typed ordered rows preserving multiplicity", "snapshot": "before churn" if scenario.reader == "retained" else "fixture after churn",
            "transaction": "one atomic transaction per writer" if scenario.family == "write" else "single read snapshot",
            "durability": "default synchronous ACK", "errors": "success required; any exception invalidates run",
            "fts_projection": "sorted record IDs only; scores/ranking out of scope" if scenario.family == "fts" else None}
    if scenario.family == "landing":
        value["regime_prerequisites"] = "identity indexes provisioned; blocking consumer; query memory budget disabled in both arms"
        value["qualification_limit"] = (
            "diagnostic control only under current historical-noop memory contract: "
            "any configured query memory budget selects memory_guard and does not exercise batched landing; "
            "without that budget candidate logical-memory observation is unavailable"
        )
    return value


def build_fixture(db, scenario: Scenario, *, apply_updates=True):
    nodes, edges = fixture_data(scenario)
    with db.begin("write") as tx:
        tx.execute("CREATE NODE TABLE N(id INT64,value INT64,payload STRING,body STRING,PRIMARY KEY(id))")
        tx.execute("CREATE REL TABLE E(FROM N TO N)")
        tx.execute("CREATE NODE TABLE W(id INT64,payload STRING,PRIMARY KEY(id))")
    with db.begin("write") as tx:
        tx.execute("UNWIND $rows AS r CREATE (:N {id:r.id,value:r.value,payload:r.payload,body:r.body})", {"rows": nodes})
    if edges:
        with db.begin("write") as tx:
            tx.execute("UNWIND $rows AS r MATCH (a:N {id:r.a}),(b:N {id:r.b}) CREATE (a)-[:E]->(b)",
                       {"rows": [{"a": a, "b": b} for a, b in edges]})
    if scenario.family == "fts":
        from okto_grafx import TextIndexOptions
        db.create_text_index("campaign_text", "N", ("body",), options=TextIndexOptions(
            statistics_mode="durable", statistics_history_entries=scenario.fts_history))
    if apply_updates:
        apply_churn(db, scenario)
    if scenario.family == "landing":
        # Identity indexes are optional maintenance artifacts, not implied by a
        # primary-key index. Provision identically in both arms before timing.
        db.ensure_identity_indexes()
    db.checkpoint()


def apply_churn(db, scenario):
    for i in range(scenario.nodes * scenario.churn_percent // 100):
        with db.begin("write") as tx:
            if i % 2:
                tx.execute("MATCH (n:N {id:$id}) DETACH DELETE n", {"id": i})
            else:
                tx.execute("MATCH (n:N {id:$id}) SET n.value=$value", {"id": i, "value": -2 * i})


def matrix():
    """Named relevant slices; use CLI list to select, never implicitly run all."""
    result = [Scenario("constant", "constant"),
              Scenario("pk-seek-control", "pk_seek", nodes=128, edges_per_node=0,
                       keys=(1, 7, 7, 31, 999)),
              Scenario("landing-control", "landing", nodes=128, edges_per_node=0, fanout=64)]
    for n in (1024, 16384):
        for count in (0, 1, 8, 16, 64, n // 2):
            result.append(Scenario(f"keys-n{n}-k{count}", "keys", nodes=n, keys=tuple(range(count))))
        for distribution in ("uniform", "hub"):
            for factor in (1, 8):
                result.append(Scenario(f"edges-n{n}-{distribution}-x{factor}", "edge_scan", nodes=n,
                                       distribution=distribution, edges_per_node=factor))
        for page in (1, 16, 128):
            result.append(Scenario(f"page-n{n}-p{page}", "page", nodes=n, page=page))
    for fanout in (0, 1, 63, 64, 65, 128):
        result.append(Scenario(f"fanout-{fanout}", "fanout", fanout=fanout))
    for rows in (1, 32, 128, 512):
        for size in (64, 4096):
            for writers in (1, 2, 4):
                result.append(Scenario(f"write-r{rows}-b{size}-w{writers}", "write",
                                       write_rows=rows, payload_bytes=size, writers=writers))
    for reader in ("short", "retained"):
        for churn in (0, 25):
            result.append(Scenario(f"reader-{reader}-churn{churn}", "node_scan", reader=reader,
                                   churn_percent=churn, cursor_batch_size=1 if reader == "short" else 0))
    for term in ("rare", "common"):
        for cache in ("warm", "reopen"):
            for history in (1, 8):
                result.append(Scenario(f"fts-{term}-{cache}-h{history}", "fts", term=term,
                                       cache=cache, fts_history=history))
    return result
