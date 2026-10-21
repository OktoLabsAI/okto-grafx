"""RF05 ordered-page campaign cases with an input-derived, ordered oracle.

The target and canonical controls execute the same business query shape. SKIP 0
is an explicit fallback control, not a different pagination contract.
"""
from __future__ import annotations


CASES = ("first", "membership", "dynamic_membership", "keyset", "skip", "no_index")


def matrix(scenario_type):
    return [scenario_type(f"rf05-{case}-n1024-p16", "pagination", nodes=1024,
                          edges_per_node=0, page=16, pagination_case=case)
            for case in CASES]


def validate(scenario):
    if scenario.pagination_case not in CASES or scenario.nodes < 32 or scenario.page < 1:
        raise ValueError("invalid RF05 pagination case or dimensions")
    if scenario.page > scenario.nodes - 16:
        raise ValueError("RF05 fixture must retain a full page after membership rejection")
    if scenario.reader != "short" or scenario.churn_percent or scenario.writers != 1:
        raise ValueError("RF05 pagination fixtures require one short reader without churn")


def expected_ids(scenario):
    validate(scenario)
    excluded = set()
    if scenario.pagination_case in ("membership", "dynamic_membership"):
        excluded = set(range(scenario.nodes - 16, scenario.nodes))
    if scenario.pagination_case == "keyset":
        # Cursor is the independently known 12th row. The next ID shares its
        # timestamp, so the strict PK tie breaker is exercised.
        boundary = scenario.nodes - 12
        excluded.update(range(boundary, scenario.nodes))
    return tuple(f"n-{i:04d}" for i in range(scenario.nodes - 1, -1, -1)
                 if i not in excluded)[:scenario.page]


def oracle(scenario):
    return tuple((value,) for value in expected_ids(scenario))


def query_for(scenario):
    validate(scenario)
    from okto_grafx.domain.model.value import Timestamp

    label = "Selected" if scenario.pagination_case == "dynamic_membership" else "Item"
    skip = "SKIP 0 " if scenario.pagination_case == "skip" else ""
    where = ""
    params = {"page": scenario.page}
    if scenario.pagination_case == "keyset":
        boundary = scenario.nodes - 12
        where = ("WHERE n.created_at < $cursor_ts OR "
                 "(n.created_at = $cursor_ts AND n.id < $cursor_pk) ")
        params.update(cursor_ts=Timestamp(1000 + boundary // 3),
                      cursor_pk=f"n-{boundary:04d}")
    return (f"MATCH (n:{label}) {where}RETURN n.id "
            f"ORDER BY n.created_at DESC, n.id DESC {skip}LIMIT $page", params)


def contract(scenario):
    validate(scenario)
    return {"result": "exact ordered STRING primary keys; no ties or duplicates lost",
            "snapshot": "checkpointed fixture; one short read snapshot",
            "transaction": "single read statement", "durability": "synchronous fixture ACK",
            "errors": "success required; any exception invalidates run",
            "qualification_limit": "structural target and fallback controls; timings require calibrated campaign",
            "case": scenario.pagination_case, "rejected_top_candidates": 16 if
            scenario.pagination_case in ("membership", "dynamic_membership") else 0}


def validate_qualification_sample(sample, scenario, *, arm):
    """Require observed ordered work for the candidate, not just an EXPLAIN arm."""
    if sample.get("first_row_ns") is not None or sample.get("consume_all_ns") != sample.get("elapsed_ns"):
        raise ValueError("RF05 materialized page cannot claim a first-row or partial-consume boundary")
    if arm != "B":
        return
    stats = sample["statistics"]
    case = scenario.pagination_case
    ordered = case in ("first", "membership", "dynamic_membership", "keyset")
    expected_candidates = scenario.page + (16 if case in ("membership", "dynamic_membership") else 0)
    if ordered:
        if ("OrderedNodeMerge" not in sample["selected_plan"]
                or stats.get("ordered_candidates_examined") != expected_candidates
                or stats.get("ordered_merge_rows") != scenario.page):
            raise ValueError("RF05 candidate lacks the bounded ordered execution evidence")
        if case == "first" and stats.get("physical_node_versions_examined", scenario.nodes) >= scenario.nodes:
            raise ValueError("RF05 first page did not reduce physical node work")
    elif stats.get("ordered_merge_rows", 0) != 0:
        raise ValueError("RF05 fallback control unexpectedly used ordered merge")


def build_fixture(db, scenario):
    from okto_grafx.domain.model.value import Timestamp

    validate(scenario)
    with db.begin("write") as tx:
        tx.execute("CREATE NODE TABLE Item(id STRING, created_at TIMESTAMP, PRIMARY KEY(id))")
    rows = [{"id": f"n-{i:04d}", "ts": Timestamp(1000 + i // 3)}
            for i in range(scenario.nodes)]
    with db.begin("write") as tx:
        tx.execute("UNWIND $rows AS r CREATE (:Item {id:r.id, created_at:r.ts})", {"rows": rows})
    if scenario.pagination_case != "no_index":
        db.create_index("item_page", "Item", ("created_at", "id"), layout="ordered")
    if scenario.pagination_case == "dynamic_membership":
        with db.begin("write") as tx:
            tx.execute("MATCH (n:Item) SET n:Selected")
    if scenario.pagination_case in ("membership", "dynamic_membership"):
        label = "Selected" if scenario.pagination_case == "dynamic_membership" else "Item"
        with db.begin("write") as tx:
            tx.execute(f"MATCH (n:{label}) WHERE n.id >= $first REMOVE n:{label}",
                       {"first": f"n-{scenario.nodes - 16:04d}"})
    db.checkpoint()
