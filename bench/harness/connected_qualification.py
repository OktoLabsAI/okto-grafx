"""RF03 topology, independent membership oracle and explicit qualification slices.

Anchor work and edge work are separate. A WITH variant is a diagnostic control,
never a replacement for the original connected query. No temporal claim here.
"""
import random

FAMILIES = frozenset({"connected_in", "connected_with", "isolated_in"})


def validate(scenario):
    if scenario.destination_nodes < 1 or scenario.parallel_edges < 0:
        raise ValueError("invalid connected topology dimensions")
    if scenario.churn_percent or scenario.reader != "short":
        raise ValueError("connected qualification does not implement churn/retained readers")
    if len(scenario.keys) > 1024 or any(type(k) is not int for k in scenario.keys):
        raise ValueError("connected probe list must contain at most 1024 integer keys")
    if any(k < -(2**63) or k >= 2**63 for k in scenario.keys):
        raise ValueError("connected keys must fit INT64")


def fixture_data(scenario):
    validate(scenario)
    starts = list(range(scenario.nodes))
    destinations = list(range(scenario.destination_nodes))
    # Uniform topology intentionally has one edge/start and repeated edges at
    # the prefix. Seed rotates destinations without changing multiplicity.
    offset = random.Random(scenario.seed).randrange(scenario.destination_nodes) if scenario.seed else 0
    edges = []
    total = scenario.nodes + scenario.parallel_edges
    hubs = max(1, scenario.nodes // 100)
    for i in range(total):
        source = i % scenario.nodes
        if scenario.distribution == "hub" and i < (total * 80 + 99) // 100:
            source = i % hubs
        edges.append((source, (source + offset) % scenario.destination_nodes, i))
    return starts, destinations, edges


def oracle(scenario):
    starts, _, edges = fixture_data(scenario)
    selected = set(scenario.keys)
    if scenario.family == "isolated_in":
        return tuple((i,) for i in starts if i in selected)
    return tuple(sorted((a, b, weight) for a, b, weight in edges if a in selected))


def query_for(scenario):
    if scenario.family == "isolated_in":
        query = "MATCH (a:N) WHERE a.id IN $keys RETURN a.id ORDER BY a.id"
    elif scenario.family == "connected_with":
        query = ("MATCH (a:N) WHERE a.id IN $keys WITH a MATCH (a)-[r:E]->(b:B) "
                 "RETURN a.id,b.id,r.weight ORDER BY a.id,b.id,r.weight")
    else:
        query = ("MATCH (a:N)-[r:E]->(b:B) WHERE a.id IN $keys "
                 "RETURN a.id,b.id,r.weight ORDER BY a.id,b.id,r.weight")
    return query, {"keys": list(scenario.keys)}


def contract(scenario):
    return {
        "anchor": "IN membership; duplicate probes never multiply nodes",
        "edge_work": "Report edge_scans/edge_lookups and physical_edge_versions_examined separately from anchor work",
        "topology": {"starts": scenario.nodes, "destinations": scenario.destination_nodes,
                     "edges": scenario.nodes + scenario.parallel_edges},
        "comparison": "same original query, topology, seed and parameters in both revisions",
        "qualification_limit": "WITH/isolated controls do not qualify the original connected query; RF04 interaction must be reported",
        "cache_limit": "reopen is a new handle, not a claim of cold OS cache",
    }


def build_fixture(db, scenario, *, apply_updates=True):
    starts, destinations, edges = fixture_data(scenario)
    with db.begin("write") as tx:
        tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
        tx.execute("CREATE NODE TABLE B(id INT64, PRIMARY KEY(id))")
        tx.execute("CREATE REL TABLE E(FROM N TO B, weight INT64)")
    # The API accepts at most 1024 list elements; construction is outside timing.
    with db.begin("write") as tx:
        for table, ids in (("N", starts), ("B", destinations)):
            for offset in range(0, len(ids), 512):
                tx.execute(f"UNWIND $ids AS id CREATE (:{table} {{id:id}})",
                           {"ids": ids[offset:offset + 512]})
        for offset in range(0, len(edges), 512):
            tx.execute("UNWIND $rows AS row MATCH (a:N {id:row.a}),(b:B {id:row.b}) "
                       "CREATE (a)-[:E {weight:row.w}]->(b)",
                       {"rows": [{"a": a, "b": b, "w": w} for a, b, w in edges[offset:offset + 512]]})
    db.ensure_identity_indexes()
    db.checkpoint()


def matrix():
    from bench.harness.campaign_fixtures import Scenario

    result = [Scenario(f"rf03-connected-n1024-k{k}", "connected_in", keys=tuple(range(k)),
                       role="target", work_counter="physical_node_versions_examined")
              for k in (1, 8, 16)]
    result += [Scenario("rf03-connected-duplicates", "connected_in", keys=(0, 0, 1, 1, 7),
                        role="target", work_counter="physical_node_versions_examined"),
               Scenario("rf03-empty", "connected_in", keys=()),
               Scenario("rf03-wide-n1024-k1024", "connected_in", keys=tuple(range(1024))),
               Scenario("rf03-broad-n16-k1024", "connected_in", nodes=16,
                        parallel_edges=7, keys=tuple(range(1024))),
               Scenario("rf03-with-diagnostic", "connected_with", keys=tuple(range(8))),
               Scenario("rf03-isolated-control", "isolated_in", keys=tuple(range(8))),
               Scenario("rf03-hub-control", "connected_in", distribution="hub", keys=tuple(range(8))),
               Scenario("rf03-reopen-control", "connected_in", cache="reopen", keys=tuple(range(8)))]
    return result
