# RF05 label-aware ordered pagination

The ordered merge can now recognize a single physical node owner selected by a
label, including dynamic labels and conjunctions of labels. The catalog chooses
candidate owners; the original predicate still checks each candidate's MVCC label
membership. Rejected candidates do not spend `LIMIT`. A page of 16 may examine
more than 16 candidates when labels or filters reject rows.

The bounded path uses the existing exact ordered index on `(created_at, id)`,
where the first column is TIMESTAMP and the second is the STRING primary key.
Both sort directions must be DESC. Existing total scalar projections/filters
remain required. `RETURN n`, `SKIP`, incompatible direction and a label with
multiple physical owners retain the canonical pipeline. Unlabeled polymorphic
ordered merges keep their existing behavior. There is no new index or cursor API.

```cypher
MATCH (n:Item)
WHERE n.created_at < $cursor_ts
   OR (n.created_at = $cursor_ts AND n.id < $cursor_id)
RETURN n.id, n.created_at
ORDER BY n.created_at DESC, n.id DESC
LIMIT $maximum
```

Pass a TIMESTAMP value for `cursor_ts` and the last row's STRING key for
`cursor_id`; omit the WHERE clause on the first page. The strict comparison
resolves timestamp ties without changing collation. NULL bounds follow the
canonical three-valued predicate, not an invented beginning/end token. A NULL
timestamp row follows canonical ordering on the initial page; this predicate
does not make NULL a usable continuation key. Applications must define their
nullable-key policy explicitly rather than expecting this optimization to change
the query's behavior.

Pages executed through one `with db.begin("read") as tx` see one fixed snapshot.
New independent transactions can observe insertions, label removals or updates
between pages. See [reading recipes](PERFORMANCE.md#rf10-reading-recipes-choose-the-snapshot-contract)
for lifecycle and retention tradeoffs.

Writes owned by the current transaction and missing/stale index capabilities use
the complete canonical fallback before output. Certified iteration still validates
heap identity, visibility and generation. Errors after opening a certified read
propagate; they do not restart a partial result against another generation.

[Deterministic tests](../tests/query/test_label_keyset_pagination.py) compare exact
sequences with the canonical `SKIP 0` control and independent expected rows.
The 1,024-node fixture without rejection examines 16 candidates for LIMIT 16;
membership rejecting the first 16 candidates examines 32 to return 16.
These are structural observations, not latency claims. RF05's fixture/campaign
card and authenticated verification remain separate; temporal qualification has
not been executed by this implementation.
