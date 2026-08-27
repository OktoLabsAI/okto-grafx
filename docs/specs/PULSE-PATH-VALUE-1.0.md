# Pulse path value — oracle `pulse-path-1`

This specification freezes the finite compatibility target for M-PULSE-2O. It does not widen
the Grafx Cypher subset beyond the single statement below.

```cypher
MATCH path = (a:Decision)-[r:supersedes]->(b:Decision) RETURN path
```

## Oracle coordinates

The differential oracle was measured on 2026-08-27 against the current Pulse path:

- Pulse Community checkout `0401e412c5104d7ee0ee98e7b91061f0ce94f6d2`, branch
  `feature/v0.3.3`; its relevant public files were byte-identical to fetched branch tip
  `7e126a7130090c00891f8d1d35bd44819afe7a7a`;
- Pulse Core checkout `985f6a88b526bc16e0c9faa0b7f1d9b2acd27ca9`, branch
  `feature/v0.3.3`; its relevant public files were byte-identical to fetched branch tip
  `ab61b9a785f2018312fc91541a580877fd068bbb`;
- Python 3.13.1 and `ladybug==0.16.0`, imported by Pulse as `import ladybug as kuzu`.

The observed call chain was the public `POST /boards/{board_id}/cypher` route, Core
`execute_cypher_read_only`, `CommunityKuzuCypherExecutor.execute_read_only`, and finally the
Ladybug connection. The checkouts already carried unrelated local changes; the oracle did not
edit either repository. Its database and script were temporary and were removed after the run.

## Raw Ladybug value

Ladybug reports one column named `path`. Each result row is a list of columns. The value in that
column is a built-in dictionary whose keys, in order, are `_NODES` and `_RELS`.

`_NODES` is a list of two dictionaries in source-to-target order. Each node dictionary contains,
in order:

1. `_ID`: a dictionary with integer, non-boolean `offset` and `table` values;
2. `_LABEL`: `Decision`;
3. every declared node property in catalog/DDL order, including properties whose value is null.

`_RELS` is a list containing one dictionary. Its keys are, in order:

1. `_SRC` and `_DST`, each with integer, non-boolean `offset` and `table` values;
2. `_LABEL`: `supersedes`;
3. `_ID`, with integer, non-boolean `offset` and `table` values;
4. every declared user relationship property in catalog/DDL order, including nulls. Physical
   endpoint columns such as `_from` and `_to` are not properties in this value.

The identity numbers are opaque storage identities. This contract freezes their types,
transaction-local stability, and these correlations, not their numeric values:

```text
path["_RELS"][0]["_SRC"] == path["_NODES"][0]["_ID"]
path["_RELS"][0]["_DST"] == path["_NODES"][1]["_ID"]
```

No row is produced when there is no matching edge. One edge produces one row. Parallel edges
produce one row per edge and preserve multiplicity even when both node dictionaries are equal.

## Public Pulse variants

With `include_working=False`, Pulse first injects `LIMIT 1000`, then rewrites the statement to:

```cypher
MATCH path = (a:Decision)-[r:supersedes]->(b:Decision)
WHERE a.graph_layer = 'canonical' AND b.graph_layer = 'canonical'
RETURN path LIMIT 1000
```

The rewrite, not a later result projection, removes a path whose target is in the working layer.
The returned path dictionaries are the same objects and shapes Ladybug produced. The public
envelope advertises `query_state=canonical_only`, `layers_included=[canonical]`, and
`canonical_filter_enforced=true`.

With `include_working=True`, Pulse executes the literal statement with only `LIMIT 1000` added.
All observed paths remain, and the envelope advertises `query_state=canonical_and_working`, both
layers, and `canonical_filter_enforced=false`. The path cell itself has exactly the same shape in
both variants. Envelope rewriting and layer policy belong to the Pulse adapter milestones; they
are not a reason to admit `WHERE`, `LIMIT`, or a wider path grammar in M-PULSE-2O.

## Grafx boundary

Grafx materializes the same keys, key order, labels, properties, identities, correlations, and
edge multiplicity. Committed rows may use backend-local record and table identities. Rows pending
in the owner transaction use synthetic opaque integers that are stable for that execution. No
`RecordRef`, `PendingRowRef`, transaction token, version object, table object, or other private
capability may reach a `QueryResult`.

The native Grafx `Value` contract canonicalizes sequences to tuples. M-PULSE-2O preserves that
contract: `_NODES` and `_RELS` are tuples in a native Grafx result. The Pulse provider in
M-PULSE-6 is responsible for the narrow recursive tuple-to-list conversion when constructing the
Ladybug-compatible HTTP result. Changing every Grafx list value to a mutable list is explicitly
out of scope.

The single accepted AST has one non-optional `MATCH`, one named outgoing hop with exactly the
names and schema identifiers in the statement above, and one unaliased `RETURN path`. Every
other path projection remains refused before streaming, including aliases, additional return
items or clauses, filters, sorting or windows, maps, written ranges, anonymous elements, other
names or labels, incoming or undirected directions, multiple hops, `WITH`, aggregation, writes,
and either branch of `UNION`.
