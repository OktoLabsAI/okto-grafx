"""Explicit RF02 slices using the original scalar UNWIND workload.

The existing campaign schema supplies revision/environment/raw samples/result
hashes and the unchanged 3-pair/5+20 optimization gates. Listing is not a pass.
"""


def matrix():
    from bench.harness.campaign_fixtures import Scenario

    result = [Scenario(f"rf02-scalar-n1024-k{k}", "keys", nodes=1024,
                       edges_per_node=0, keys=tuple(range(k)), role="target",
                       work_counter="physical_node_versions_examined")
              for k in (1, 8, 16, 64)]
    result += [Scenario("rf02-scalar-n1024-occ16-dist8", "keys", nodes=1024,
                        edges_per_node=0, keys=tuple(i % 8 for i in range(16)),
                        role="target", work_counter="physical_node_versions_examined"),
               Scenario("rf02-empty", "keys", edges_per_node=0, keys=()),
               Scenario("rf02-wide-n1024-k1024", "keys", edges_per_node=0,
                        keys=tuple(range(1024))),
               Scenario("rf02-map-control", "pk_seek", edges_per_node=0,
                        keys=tuple(range(16))),
               Scenario("rf02-scan-control", "node_scan", edges_per_node=0),
               Scenario("rf02-constant-control", "constant", nodes=1, edges_per_node=0)]
    return result
