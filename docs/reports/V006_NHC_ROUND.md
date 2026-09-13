# 0.0.6 NHC-1–8 delivery evidence

Date: September 10, 2026. Branch: `feature/v0.0.6`, based on `c577cec`.
**All eight items complete; local feature/regression/documentation DoD satisfied.**
This report does not assert publication, commit/push or Pulse installation.
The eight-item scope stays fixed; remaining larger roadmap capabilities are not
new acceptance conditions for this round.

## Scope and feature evidence

| Item | Implemented contract | Focused evidence |
| --- | --- | --- |
| NHC-1 | Frozen matched token-position output with aggregate limits and snapshot validation | `test_fts_position_results.py` |
| NHC-2 | Ordered field-local slop with repeated-term semantics and bounded matching work | `test_fts_proximity.py`, exhaustive independent combinatorial oracle |
| NHC-3 | Explicit one-hop endpoint closure of selected relationship copies; shared bounds/native receipt | `test_copy_endpoint_closure.py`, existing copy regression |
| NHC-4 | Same-store retained system-time schema/node/edge/property diff, absent versus NULL, recreate lineage | `test_temporal_diff.py` |
| NHC-5 | Native optional authenticated copy-on-write temporal tree, activation/update/recovery and versions access | `test_system_history_index.py`, `test_system_history_access_tree.py` |
| NHC-6 | Bounded predecessor/next-lineage as-of reconstruction, historical schemas/endpoints and scan parity | `test_system_history_index.py`, existing temporal tests |
| NHC-7 | Fresh complete same-name analyzer/options generation; atomic catalog switch | `test_fulltext_replacement.py`, `test_nhc_process_cuts.py` |
| NHC-8 | Quiescent full-image history replacement followed by checkpoint and tail reclaim | `test_system_history_compaction.py`, backup/restore, `test_nhc_process_cuts.py` |

Additional correction: `capture_copy(history="current-only")` no longer has its
policy shadowed by a commit-history page variable. This was exercised together
with endpoint closure, not hidden as a new independent initiative.

## Feature and corrective evidence

- Position/proximity group: 11 passed, 2.59 s.
- Copy/endpoint-closure group: 27 passed, 36.44 s.
- Temporal diff/base queries: 8 passed, 3.47 s.
- Grouped temporal/index/diff/retention/adversarial/codec tests: 58 passed, 49.46 s.
- Native hard process exits: 24 passed, 151.88 s. Four transitions × six cuts,
  with two recoveries each: index activation, indexed update, compaction, analyzer
  replacement; before COMMIT, before apply, after current effects, after history
  root/chunk, after COMMIT. Fixtures are isolated, not production databases.
- Extended index/compaction tests, including damage and physical backup/restore:
  9 passed, 18.39 s.
- Analyzer/options replacement including actual tokenization/case-folding change:
  3 passed, 3.00 s.
- Injected failure before physical tail reclaim, followed by a genuinely new
  native write/checkpoint/reclaim, plus endpoint-copy fixture reuse: 4 passed,
  10.31 s. A committed untruncated tail remained usable and later reclaimable.
- Documentation checker passed: links/anchors, 39 connection fields, generated
  public signatures/DTOs and all 11 preserved source plans.

Counts above overlap and are **not summed as distinct tests**. Full-regression
results and corrective reruns are recorded below. An early
documentation subset without the optional-dependency path had 29 passing cases
and one unapproved missing-Polars skip; that invocation is not acceptance. The
full invocation supplies the existing isolated optional-dependency directory.

The independent-participant test subsequently found stale local catalog metadata
after foreign index activation. Corrected by a fresh, bounded native catalog-chain
capture inside the same journal observation, without adopting it into the old
participant's schema view or adding an authority cache. The reader retains its
data snapshot while using current activation/horizon policy. The corrective
temporal/index/diff/compaction group passed 19 tests in 28.68 s; final affected
regression also includes that correction and qualified tree-reference extent checks.

The first final affected run recorded 267 passing cases and one failure in the
single-predecessor-read I/O contract. The compaction flag had introduced a second
head read. Both append preparation and append validation now derive that flag
from the same validated head capture, retaining the original one-read contract.
A complete affected rerun covers this correction; no assertion was weakened.

The corrected affected regression passed **269 tests in 499.54 s** with normal
timeouts, including all selected full-text/copy/temporal APIs, the 24 hard cuts,
native access-tree/append-transition tests, packaging and consumer documentation.
Artifact: `.grafx-tmp/nhc-final-focused-v2.xml` (and matching `.log`).
Four module-level annotation failures from the structural gate were also fixed;
the complete public-surface gate plus access-tree tests then passed **1,772 tests
in 33.61 s** (`.grafx-tmp/nhc-surface-final.xml`). These annotation-only edits do
not change runtime behavior. Counts overlap with the full suite, not additions.
The language-surface gate subsequently identified missing helper docstrings in
three modules. After completing those descriptions, the combined language,
public-surface, consumer-documentation, access-tree and nullable-vector checks
passed **2,740 tests in 88.76 s** (`.grafx-tmp/nhc-structural-final.xml`). No gate
rules or assertions were weakened to obtain these results.

## Wheel consumption

The expanded real-wheel matrix completed all **14 cells** on Windows 11 / Python
3.13.1: default, posting hash, nullable layout, system history, positional FTS,
temporal index and temporal compaction, each with pure/accelerated writers and
opposite-selector readback. Old-reader refusal preserved payload hashes.

| Artifact | SHA-256 |
| --- | --- |
| Legacy 0.0.5 wheel | `4f09d7e3ba1b8c7b716aa0b278bea2f39428db68fea71b27f56521f3b4bffcc5` |
| Final NHC 0.0.6 wheel | `ec270340430ca9542e6751a4f11cdee9e8855fe51f3b16ab3070aa36e3c46116` |

Reproduce with `tools/check_v006_upgrade.py` and those wheels. All 207 packaged
Python modules were also compared byte-for-byte against the final source:
zero mismatches. No global package
was replaced and no PyPI publish occurred. Windows evidence is not a claim that
the configured Windows/POSIX Python-version CI matrix has remotely executed.

## Performance and safety boundaries

Structural tests forbid `_iter_batches` during eligible ordinary indexed reads.
An as-of picture over 20 updates reads at most eight event/candidate units in the
fixed two-node/one-edge fixture. This proves a changed access path, **not** a Pulse
UI latency or universal speedup claim. Deleted/future-born lineages, tree height,
value bytes, output rows and full verification/build/recovery remain real costs.
The optional index adds value duplication and copy-on-write WAL/storage cost.

Compaction tests assert actual file shrink and retained-picture/pin preservation,
including without an index. Tree bulk construction discards obsolete copied
paths; expired zero padding shrinks, but schema/lineage/commit framing remains.
Compaction does not erase prior WAL, backups or filesystem snapshots. It requires
explicit quiescence; ordinary multi-reader/writer behavior is unchanged.

New native capability bits: 17 `system_history_index_v1`, 18
`system_history_compaction_v1`, both dependent on native history. No default
connection setting changes. Root/index/COMMIT authority is never inferred from
an in-memory cache. Both OCC checks and WAL-before-pages remain mandatory.
Recovery validates complete rewrites before applying any effect; overwritten
prefixes are not assembled from mixed pre/post-retention tree pages.

Each new text search uses the currently published analyzer with the owning data
snapshot; the feature is not historical analyzer selection. Existing generation
files are neither reinterpreted nor immediately deleted.

## Documentation delivered

README capabilities; ROADMAP status/remaining boundaries; API signatures and all
new DTOs; configuration option index; full-text, copy and temporal usage; native
temporal format extensions; operations, backup/restore and wheel compatibility;
changelog. The source archive remains unchanged.

## Final grouped regression

The full suite completed with normal per-test timeouts, isolated optional
dependencies and pinned Pulse query-corpus baselines: **16,929 passed, 9 failed,
19 skipped, 3,207.04 s**. Local artifacts: `.grafx-tmp/nhc-regression.xml` and
`.grafx-tmp/nhc-regression.log`. This initial full run is not described as green.
The skips are attributed platform cases and a helper-worker collection exclusion;
there is no accepted missing optional-dependency skip.

All nine failures are accounted for: four annotation checks, three helper-docstring
checks, the single-predecessor-read contract, and one executable FTS guide workflow.
The latter used a closed handle from a preceding example. Both new snippets now
create their own isolated database/schema/index and assert the advertised slop,
position output and actual analyzer replacement behavior. The same guide also
corrects superseded slop and analyzer-mode wording and its typed method signature.

The full process began before the final scoped corrections; it retained its
original imported modules. The final-source affected and structural reruns above
cover those edits without another full-suite rerun for annotation/docstring-only
changes. A final corrective regression includes **every failing test node** and
all sibling module cases, plus consumer documentation.

That final corrective regression passed **2,774 tests in 63.51 s**, with **zero
failures, errors or skips** (`.grafx-tmp/nhc-final-corrective.xml` and `.log`). An
explicit XML comparison confirms all nine original failing nodes are present
and passing in that run: no omitted failure or unapproved waiver. The 269-case
feature/affected regression and 14-cell final-wheel matrix provide the functional
and consumer-artifact coverage described above. The last source changes after
the affected run were annotations/docstrings only; the executable-guide correction
changed documentation, not engine behavior.

Final Ruff and whitespace checks passed. The documentation checker passed after
the acceptance status update: links/anchors, all 39 connection fields, generated
signatures/DTOs and the 11 unchanged archived source plans. The remaining broader
roadmap boundaries are explicitly outside this completed eight-item scope.
