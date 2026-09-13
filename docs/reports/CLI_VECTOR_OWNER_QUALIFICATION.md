# CLI vector-owner selection

September 12, 2026, `feature/v0.0.6` source. This completes the CLI search selector
portion of the existing [namespace consumer contract](../specs/GRAPH_NAMESPACES_V1.md).
It is not a new ranking engine, full-profile acceptance or an installed release.

## Implemented contract

`oktografx search vector PATH` accepts `--table NAME` and
`--table-kind node|rel`. Kind requires a table. The CLI passes the corresponding
unique name or `(kind, name)` selector to `Database.search_vectors` in its existing
single owned read transaction. Omission remains valid for one unique physical
space owner; it refuses for a shared space. Missing, wrong and ambiguous owners
never select a sibling automatically.

Filters and hits retain table-local record IDs; the caller must preserve the
requested owner when interpreting results. JSON's existing `search` DTO, scoring,
ranking and snapshot behavior are unchanged. The help and build-only
`capabilities.search.vector_owner_selection` describe the accepted selectors.
`indexes --json` retains detached physical table IDs for discovery. Logical
relationship groups still require selecting their physical member table, not
requesting a merged cross-table ranking.

Text search continues to use globally unique text-index names. Hybrid retains its
node target and rejects a text index belonging to the homonymous relationship.
`--table-kind` is only a vector-search option. Invalid kind spelling is CLI usage
error 2; native ownership errors preserve the runtime error envelope.

No connection defaults, persisted preferences, format bits, Python search API
signatures or transaction/reader guarantees changed. All commands still open
read-only; these flags do not rebuild or repair a stale/missing index. See
[CLI syntax and consumption](../CLI.md#text-vector-and-hybrid-search).

## Discriminating tests

`tests/cli/test_vector_owner_selection.py` uses real stored data through the actual
CLI entry point, with pure/NumPy source codecs. Node and edge share table name R,
space s and record ID 1, but have orthogonal vectors and different text. Assertions
check the selected owner's cosine score, empty filtering, ambiguity/missing-owner
refusal, type usage errors, vector-index identities, capabilities/help and an
actual `python -m okto_grafx.cli` subprocess with equals-form options.

The same fixture validates independent lexical results for node/edge text indexes,
node-target hybrid success and refusal of the edge text index for that hybrid
target. Every in-process search/refusal keeps all non-control file hashes equal;
only successful reader registration's control directory is excluded. Catalog,
heap, WAL and indexes are included. Existing unique-owner search remains valid
with no selector, a bare name or explicit node kind.

The first receipt had 28 passes and two new-fixture failures: an invalid spelling
is rejected by the parser with the established `result=usage` JSON document,
not the runtime `error` envelope. The fixture now checks that precise existing
contract. No CLI error taxonomy was weakened or converted into success.

## Completed regression

The combined command/search/process/error/output regression finished with **324
passed, zero failures, errors or skips**, in 61.155 seconds. Receipt:
`.grafx-tmp/cli-vector-owners-qualified.xml`, SHA-256
`025838410abb428289212ab31bb0c979aad34986154c46dac7088aa6e72c3489`.
This includes all 18 new physical-owner cases and existing single-owner search,
grammar, inventory, process entry point, exit-code, output and terminal-isolation
contracts. The earlier failing receipt is retained, not counted as acceptance.

## Remaining acceptance

This is source CLI/module execution, not an installed-current CLI wheel or Pulse
qualification. The module subprocess intentionally inherits the test's source
PYTHONPATH; it is not presented as isolated installed-package evidence. Remaining
consumer/package/Pulse checks and the FP-4–8 requirements remain in the main plan.
No production graph, global installation, commit, push or release was changed.
