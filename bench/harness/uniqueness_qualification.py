"""RF08 CREATE workload with independent rows and explicit durability boundaries.

The unbudgeted target exercises certified groups. A separate budgeted control
exercises the scalar guard and supplies a real logical-memory observation. The
unbudgeted samples retain a null logical peak; RSS is a distinct measurement.
"""
from __future__ import annotations


FAMILY = "unique_create"
INSERT = "UNWIND $ids AS id CREATE (n:N {id: id}) RETURN n.id"
READBACK = "MATCH (n:N) RETURN n.id ORDER BY n.id"
QUOTA_BYTES = 1 << 20


def validate(scenario):
    if scenario.nodes != 1 or scenario.edges_per_node != 0 or scenario.writers != 1:
        raise ValueError("RF08 fixture uses one writer and no seed rows")
    if scenario.write_shape != "unwind" or scenario.reader != "short" or scenario.cache != "reopen":
        raise ValueError("RF08 fixture requires one materialized UNWIND and a fresh handle")
    if scenario.write_rows not in (1, 128):
        raise ValueError("RF08 campaign supports target 128 and scalar control 1")
    if scenario.query_memory_budget_bytes not in (None, QUOTA_BYTES):
        raise ValueError("RF08 quota control must use the declared positive budget")
    target = scenario.write_rows == 128 and scenario.query_memory_budget_bytes is None
    if scenario.role != ("target" if target else "control"):
        raise ValueError("RF08 target/control role differs from batch eligibility")
    if scenario.work_counter != ("certificate_attempts" if target else None):
        raise ValueError("RF08 target requires physical certificate-attempt counter")
    if scenario.query_memory_budget_bytes is not None and scenario.write_rows != 128:
        raise ValueError("RF08 quota control uses the same 128-row query as target")


def matrix(Scenario):
    return [
        Scenario("rf08-create-128", FAMILY, nodes=1, edges_per_node=0,
                 write_rows=128, cache="reopen", role="target",
                 work_counter="certificate_attempts"),
        Scenario("rf08-create-1-control", FAMILY, nodes=1, edges_per_node=0,
                 write_rows=1, cache="reopen"),
        Scenario("rf08-create-128-quota-control", FAMILY, nodes=1,
                 edges_per_node=0, write_rows=128, cache="reopen",
                 query_memory_budget_bytes=QUOTA_BYTES),
    ]


def query_for(scenario):
    return INSERT, {"ids": list(range(scenario.write_rows))}


def oracle(scenario):
    return tuple((row_id,) for row_id in range(scenario.write_rows))


def contract(scenario):
    return {
        "result": "ordered INT64 IDs from one CREATE statement; independent readback after ACK",
        "transaction": "one atomic write transaction",
        "durability": "one returned durable ACK before readback and reopen",
        "errors": "success required; any error invalidates temporal sample",
        "batch_regime": "certified groups of at most 64" if scenario.role == "target" else "scalar control",
        "memory_limit": ("logical peak unavailable while query budget is disabled; process RSS only"
                         if scenario.query_memory_budget_bytes is None else
                         "positive query budget, scalar guard, observed logical peak"),
        "qualification_scope": "unbudgeted temporal diagnostic and separate budgeted control",
    }


def build_fixture(db, scenario):
    with db.begin("write") as tx:
        tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
    db.checkpoint()


def validate_sample(sample, scenario, *, arm):
    """Reject missing ACKs, fabricated batch work and confused memory regimes."""
    evidence = sample.get("unique_create")
    if not isinstance(evidence, dict):
        raise ValueError("RF08 sample lacks write evidence")
    if evidence.get("ids") != scenario.write_rows or evidence.get("statement_rows") != scenario.write_rows:
        raise ValueError("RF08 write count differs from input")
    if evidence.get("readback_rows") != scenario.write_rows or sample.get("rows") != scenario.write_rows:
        raise ValueError("RF08 durable readback differs from input")
    ack = evidence.get("ack")
    if not isinstance(ack, dict) or type(ack.get("csn")) is not int or ack["csn"] < 1:
        raise ValueError("RF08 commit report missing")
    if ack.get("durable") is not True or ack.get("wrote") is not True:
        raise ValueError("RF08 commit was not durably acknowledged")
    if evidence.get("ack_count") != 1 or evidence.get("atomic_unit") != "one transaction":
        raise ValueError("RF08 atomic unit differs from contract")
    if sample.get("commit_ns") is None or sample["commit_ns"] > sample["elapsed_ns"]:
        raise ValueError("RF08 ACK duration is outside timed interval")
    stats = sample.get("statistics", {})
    expected = 2 if scenario.role == "target" and arm == "B" else 0
    if stats.get("pk_uniqueness_batches", 0) != expected:
        raise ValueError("RF08 effective batch regime differs from scenario")
    if scenario.role == "target" and arm == "B":
        if stats.get("certificate_attempts") != 2 or stats.get("index_probe_keys") != 128:
            raise ValueError("RF08 certified work differs from target")
    memory = sample.get("memory", {})
    if scenario.query_memory_budget_bytes is None:
        if (memory.get("logical_memory_peak_bytes") is not None
                or memory.get("coverage") != "unavailable"
                or memory.get("unavailable_reason") != "query_memory_budget_disabled"):
            raise ValueError("RF08 unbudgeted batch cannot claim logical memory")
    elif (type(memory.get("logical_memory_peak_bytes")) is not int
          or memory.get("coverage") == "unavailable"
          or memory.get("unavailable_reason") is not None):
        raise ValueError("RF08 quota control requires a measured logical peak")
