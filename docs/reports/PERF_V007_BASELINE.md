# v0.0.7 performance baseline (Phase 0)

September 14, 2026. Evidence, not a plan and not a gate. Every number below is a measurement of
this machine on this date; nothing here qualifies or disqualifies any candidate on its own.

Label convention, kept from the reconnaissance documents: **MEDIDO** = measured in this session,
with the command shown; **CITADO** = taken from an existing document, with `path:line`;
**INFERIDO** = derived, never observed directly. A/B ratios are always reported as
**v0.0.5 wall ÷ HEAD wall**: below 1 the v0.0.5 arm was faster, above 1 the HEAD arm was faster.
Round 0 (and, for the micro-instruments, run 0) is discarded everywhere.

## Provenance

| item | value | label |
|---|---|---|
| Repository HEAD | `b0e4f51` on `feature/v0.0.7` (v0.0.6 tag `1e01be5` plus the version bump) | MEDIDO |
| `git diff -- src/` during the runs | empty; the four modified tracked files are `CHANGELOG.md`, `ROADMAP.md`, `docs/README.md`, `tools/check_documentation.py` (another agent's work) plus the untracked `docs/specs/PERFORMANCE_V007_PLAN.md` | MEDIDO |
| Comparison arm | `83cc313` ("Merge pull request #3 from OktoLabsAI/feature/v0.0.5"), 20 commits behind HEAD, checked out as a worktree under this session's scratchpad | MEDIDO |
| Machine | Windows 11 Home Single Language 10.0.26200, Intel i7-11800H, 16 logical cores | MEDIDO |
| Repository venv | `D:\Projetos\Techridy\okto_grafx\.venv\Scripts\python.exe`, CPython 3.11.14, numpy 2.4.6, `google_crc32c` C implementation | MEDIDO |
| Pulse venv (steps 1, 2) | `D:\Projetos\Techridy\okto-pulse-kg-health-grafx-codex\.venv\Scripts\python.exe`, CPython 3.11.14, numpy 2.4.4, `google_crc32c` C implementation | MEDIDO |
| Pulse trees | Core `ea11b76` (`milestone/grafx-mpulse5-logical-transfer-core`, clean), Community `72a2df3` (`fix/v0.3.3-kg-health-grafx`, 26 modified tracked files) | MEDIDO, asserted by `amdahl_pulse.pin()` in every round's JSON |
| Grafx resolved by the instrument | HEAD arm `D:\Projetos\Techridy\okto_grafx\src\okto_grafx\__init__.py`; v0.0.5 arm the worktree's `src`; never site-packages | MEDIDO |
| Connect defaults on both arms | `buffer_budget_bytes = 64 MiB` (`src/okto_grafx/runtime/config.py:266`), `codec = "numpy"` (`:291`), `vector_math = "numpy"` (`:292`) — identical text in the v0.0.5 worktree, because `4b632c0` is an ancestor of `83cc313` | MEDIDO |
| CRC | `NativeCrc32c().install()` performed by every harness; provider `google_crc32c` with the C extension present | MEDIDO |

Raw outputs, logs, per-step JSONL manifests and the two aggregation scripts live in
`.grafx-tmp/perf07/baseline/` (git-ignored). `.grafx-tmp/perf07/baseline/RUN_MANIFEST.md` carries
the command line, start time, wall time and exit code of every process.

## Machine quiescence and contamination (declared before the numbers)

The machine was **not** quiescent and could not be made quiescent without killing the user's
processes, which was not permitted. Sampled five times at 60-second intervals immediately before
step 1 (MEDIDO):

| pid | what it is | CPU seconds per 60 s of wall |
|---|---|---|
| 31712 | the live Pulse server (`okto-pulse serve`, running since 13/09 10:51) | 82.3, 147.0, 145.9, 188.0, 137.9 |
| 16600 | the concurrent Codex agent runtime | 63.3, 63.7, 64.9, 63.1, 63.9 |
| 33712 | `fp_pulse_http_final.py serve` (an earlier qualification server) | 1.1 – 1.6 |

Between 1.4 and 3.1 of the 16 logical cores were busy with foreign work for the entire session,
and the Pulse server's load varied by a factor of 2.3 across the five samples. Nothing was killed
and no measurement ran concurrently with another. **Consequence: absolute walls in this report are
inflated by an unknown, time-varying amount, and only the paired ratios (arms alternated
round by round) are defended against that drift.** This repeats the condition declared in
`.grafx-tmp/levantamento2/LEVANTAMENTO_6.md:11` for that round.

## 0. Harness and board provisioning

The levantamento-2 harnesses and boards were copied out of the other session's scratchpad into
`.grafx-tmp/lev2/`, preserving the relative layout `ab_fanout.py:19-20` requires
(`HERE.parent/survey6/PLATFORM/board_kg`):

| source (`$L2 = C:\Users\jpamb\AppData\Local\Temp\claude\D--Projetos-Techridy-okto-grafx\47402337-…\scratchpad`) | destination | size | files |
|---|---|---|---|
| `$L2\w1q` | `.grafx-tmp\lev2\w1q` | 0.2 MB | 86 |
| `$L2\w2ckpt` | `.grafx-tmp\lev2\w2ckpt` | 0.1 MB | 21 |
| `$L2\w2lb4` | `.grafx-tmp\lev2\w2lb4` | 7.1 MB | 52 |
| `$L2\survey5\CKPTCERT-verify\board` | `.grafx-tmp\lev2\survey5\CKPTCERT-verify\board` | 18.9 MB | 82 |
| `$L2\survey6\PLATFORM\board_kg` | `.grafx-tmp\lev2\survey6\PLATFORM\board_kg` | 107.2 MB | 547 |
| `$L2\survey6\VECTOR-verify\boards\b300u` | `.grafx-tmp\lev2\survey6\VECTOR-verify\boards\b300u` | 6.7 MB | 54 |

All six sources existed; nothing was skipped. Sizes and file counts match source and destination
(MEDIDO). One deviation: `board_kg/sidecar.json` stores the board path as an absolute string, and
`bench_kg_arm.py:33` opens **that** path, not the directory passed on the command line. The copy's
`board` field was repointed at the copy and the original string preserved under a new
`board_original_before_copy` key; the statement, parameters and `rel_pairs` are unchanged. Without
this edit both arms would have measured the other session's board in place.

## 1 and 2. Pulse transfer Amdahl — HEAD versus v0.0.5

Instrument `.grafx-tmp/amdahl/amdahl_pulse.py` (unmodified), driving the production sink
`CommunityGrafxLogicalCandidateSink`. Six detached invocations, alternating arms so machine drift
cancels; each invocation runs two rounds and its round 1 is discarded, so each arm contributes
three measured rounds, each preceded by its own warm-up:

```text
<pulse venv python> -u -B .grafx-tmp\amdahl\amdahl_pulse.py ^
  --core  D:\Projetos\Techridy\okto-pulse-core-mpulse5-transfer ^
  --community D:\Projetos\Techridy\okto-pulse-kg-health-grafx-codex ^
  --grafx <D:\Projetos\Techridy\okto_grafx | ...\grafx-v005> ^
  --nodes-per-type 300 --rels-per-layout 60 --batch 500 --rounds 2 --out <arm>_<i>.json
```

Order of invocation: head (01:55:20), v005 (01:58:31), v005 (02:01:22), head (02:04:13),
head (02:07:14), v005 (02:10:15). Identical input in both arms: 3,301 nodes over 12 node types,
4,140 relations over 69 layouts, 174,185 properties, 3,300 vectors, 12 node batches and 69 relation
batches; the synthetic graph generation (1.19–1.29 s) is outside the denominator.

| phase | HEAD median (min..max), s | v0.0.5 median (min..max), s | paired ratio per round (v005 ÷ HEAD) | median ratio | label |
|---|---|---|---|---|---|
| transfer, phases 1–6 | **70.19** (69.70..73.54) | **66.31** (65.82..67.83) | 0.895, 0.945, 0.973 | **0.945** | MEDIDO |
| 1 `begin_candidate` (DDL) | 5.96 (5.87..6.47) | 6.19 (5.56..6.32) | 0.947, 0.977, 1.039 | 0.977 | MEDIDO |
| 2 `write_nodes` | 14.90 (14.41..16.24) | 12.51 (12.21..13.33) | 0.770, 0.847, 0.894 | **0.847** | MEDIDO |
| 3 `write_relations` | 27.22 (26.64..28.37) | 23.95 (23.70..24.15) | 0.851, 0.871, 0.899 | **0.871** | MEDIDO |
| 4 `checkpoint` | 5.26 (4.66..5.29) | 5.93 (5.34..6.07) | 1.128, 1.010, 1.303 | **1.128** | MEDIDO |
| 5 `certify` | 15.11 (14.47..15.29) | 15.78 (15.49..16.26) | 1.013, 1.124, 1.044 | 1.044 | MEDIDO |
| 6 `finalize` | 0.000 | 0.000 | — | — | MEDIDO |
| 7 `restore_read` (outside the transfer) | 7.37 (7.22..7.55) | 7.24 (6.68..7.61) | — | 0.982 | MEDIDO |
| 8 `runtime_open` | 1.88 (1.76..1.89) | 1.88 (1.75..2.21) | — | 0.996 | MEDIDO |
| 8a 300 PK lookups | 0.447 (0.405..0.500) | 0.391 (0.372..0.504) | — | 0.967 | MEDIDO |
| 8b 300 one-hop reads | 0.617 (0.572..0.636) | 0.533 (0.469..0.537) | — | 0.871 | MEDIDO |
| 8c 30 vector searches | 3.248 (3.186..3.249) | 3.184 (3.002..3.317) | — | 0.980 | MEDIDO |

Reading: on this machine, in this shape, the HEAD transfer is **5.5 % slower than v0.0.5**
(3 of 3 rounds below 1, band 0.895–0.973). The whole of that difference and more sits in the two
write phases — `write_nodes` 0.847 and `write_relations` 0.871, both 3 of 3 rounds below 1 — while
`checkpoint` moves the other way (1.128, 3 of 3 rounds at or above 1.01) and `certify` is
marginally in HEAD's favour (1.044, band 1.013–1.124). The 3 of 3 consistency in the write phases
makes a pure-drift explanation unlikely, but the arms were not run against a common CPU-load
oracle, so **the cause is not established here**; it is a candidate for the first bisect of the
initiative, not a verdict.

### Composition of the transfer (per grafx operation, medians of the three measured rounds)

| operation | HEAD s | HEAD % of 70.19 s | v0.0.5 s | v0.0.5 % of 66.31 s | label |
|---|---|---|---|---|---|
| 3 `write_relations` · `execute` | 20.95 | **29.9 %** | 17.83 | 26.9 % | MEDIDO |
| 2 `write_nodes` · `execute` | 7.21 | 10.3 % | 5.38 | 8.1 % | MEDIDO |
| 2 `write_nodes` · `commit` | 6.27 | 8.9 % | 5.68 | 8.6 % | MEDIDO |
| 3 `write_relations` · `commit` | 5.82 | 8.3 % | 5.55 | 8.4 % | MEDIDO |
| 5 `certify` · `verify("all")` | 5.54 | 7.9 % | 5.58 | 8.4 % | MEDIDO |
| 4 `checkpoint` · `checkpoint()` | 5.25 | 7.5 % | 5.93 | 8.9 % | MEDIDO |
| 1 DDL · `execute` | 5.15 | 7.3 % | 5.28 | 8.0 % | MEDIDO |
| 5 `certify` · `connect_ro` (cold open) | 3.13 | 4.5 % | 2.99 | 4.5 % | MEDIDO |
| 5 `certify` · `_prepare` (Pulse: scan+decode+sqlite+validate) | 2.27 | 3.2 % | 2.49 | 3.8 % | MEDIDO |
| 5 `certify` · `fingerprint.add_node` (Core) | 1.79 | 2.6 % | 2.40 | 3.6 % | MEDIDO |
| 5 `certify` · `scan_rows_v1` | 1.65 | 2.3 % | 2.02 | 3.1 % | MEDIDO |
| 5 `certify` · `decode_relation` (includes `sqlite.resolve`) | 1.62 | 2.3 % | 1.84 | 2.8 % | MEDIDO |
| 5 `certify` · `sqlite.resolve` | 1.42 | 2.0 % | 1.60 | 2.4 % | MEDIDO |

The full per-operation tables, including call counts and ms/call, are in
`.grafx-tmp/perf07/baseline/amdahl_aggregate.json` and in each invocation's own JSON.

### Comparison with the last recorded transfer

| phase | `a726744`, 2026-09-06 (CITADO) | HEAD `b0e4f51`, today (MEDIDO) | difference |
|---|---|---|---|
| 1 DDL | 7.13 s (6.86..7.84) — `.grafx-tmp/levantamento2/REFRESH_HEAD_a726744.md:65` | 5.96 s | −16 % |
| 2 `write_nodes` | 11.40 s (10.80..12.67) — `:66` | 14.90 s | +31 % |
| 3 `write_relations` | 18.33 s (18.01..19.10) — `:67` | 27.22 s | +48 % |
| 4 `checkpoint` | 5.74 s (5.13..5.99) — `:68` | 5.26 s | −8 % |
| 5 `certify` | 14.22 s (13.19..14.36) — `:69` | 15.11 s | +6 % |
| transfer (1–6) | 58.49 s (58.23..59.98) — `:70` | 70.19 s | +20 % |
| 7 `restore_read` | 7.13 s — `:71` | 7.37 s | +3 % |
| 8c vector, 30 queries | 3.83 s, 128 ms/query — `:73` | 3.25 s, 108 ms/query | −15 % |

This comparison is **cross-machine-state, not cross-code-only**: the 2026-09-06 run was made on a
quiet machine after an explicitly discarded contaminated run
(`REFRESH_HEAD_a726744.md:40-45`, CITADO), whereas today's ran beside a live Pulse server. The
v0.0.5 arm measured today is the control that the citation cannot provide: it sat at 66.31 s in the
same window, so **at most 3.9 s of the 11.7 s gap to 58.49 s can be attributed to code between
`83cc313` and `b0e4f51`, and the remainder is machine state or the 20 commits between `a726744`
and `83cc313`** (INFERIDO). `a726744` itself was never re-measured in this session.

The dominant operation is unchanged in kind: relation `execute` was 22.3 % of the transfer at
`a726744` (`REFRESH_HEAD_a726744.md:79`, CITADO) and is 29.9 % today (MEDIDO).

## 1b. 256 MiB buffer budget arm — skipped

**Not supported by the instrument.** `amdahl_pulse.py`'s argparse (`:562-572`) exposes
`--core --community --grafx --nodes-per-type --rels-per-layout --batch --rounds --runtime-lookups
--seed --profile-round --out` and nothing else, and its sink construction (`:383-386`) passes
`connect_factory=timed_connect(okto_grafx.connect)` without a `connect_options` mapping.
`src/okto_grafx/runtime/config.py` reads no environment variable for any connect option (MEDIDO:
no `environ`/`getenv` in that module), so the 64 MiB default at `:266` cannot be overridden from
outside the process. The instrument was not edited.

Recorded for whoever revises the instrument: the production sink already accepts the option —
`CommunityGrafxLogicalCandidateSink.__init__(..., connect_options: Mapping[str, object] | None =
None, ...)` at `okto-pulse-kg-health-grafx-codex/src/okto_pulse/community/adapters/grafx_logical_sink.py:72`,
with `page_size`, `checkpoint_interval_records` and `descriptor_revalidation` merely
`setdefault`-ed at `:92-100`. Passing `connect_options={"buffer_budget_bytes": 268435456}` through
a new `--buffer-budget-bytes` flag is a one-line change to `amdahl_pulse.py:383-386`. Until then,
LV-1 has no measurement in this baseline.

## 3. KG page and fan-out A/B on the production-shaped board

Instruments `ab_fanout.py` → `bench_kg_arm.py` (unmodified), board
`.grafx-tmp/lev2/survey6/PLATFORM/board_kg/candidate` (2,003 nodes, 3,036 relations, 69 layouts;
`sidecar.json` carries the exact Pulse page statement and parameters), `page_size=8192`,
`read_only=True`, `descriptor_revalidation="generation"` (the harness default at
`bench_kg_arm.py:30`), 4 rounds × 4 passes, arm order rotated per round, minimum of the warm
passes per arm-round, round 0 discarded here.

```text
.venv\Scripts\python.exe -u -B .grafx-tmp\lev2\w1q\ab_fanout.py kg_v007.json 4 4 ^
  head=D:\Projetos\Techridy\okto_grafx v005=<worktree at 83cc313>
```

| path | HEAD median (min..max), s | v0.0.5 median (min..max), s | paired ratio per round | median | label |
|---|---|---|---|---|---|
| page `GET_ALL_NODES`, 500 rows | 0.551 (0.490..0.575) | 0.441 (0.432..0.448) | 0.767, 0.882, 0.814 | **0.814** | MEDIDO |
| fan-out, 69 layouts in one read transaction | 2.069 (2.029..2.335) | 1.861 (1.726..1.871) | 0.801, 0.917, 0.834 | **0.834** | MEDIDO |

Oracle: both arms, all rounds, produced 1,375 edges and the single digest
`88f798f1e8cd1d0b8154cc6467bcde1d57f46a2b5beabc47d86e6894b322165c` (MEDIDO). The read paths agree
byte for byte; only the walls differ, and again in v0.0.5's favour on all three retained rounds.

Last recorded numbers on the **same board and the same harness** (CITADO,
`.grafx-tmp/levantamento2/W1CSE_RESULT.md:102-103`, arm `14da62b`, 6 rounds, minima per round):
page 0.469 0.380 0.544 0.607 0.727 0.655 s; fan-out 1.292 1.159 1.947 1.936 2.097 1.903 s. Today's
HEAD page median (0.551 s) and fan-out median (2.069 s) sit inside both of those bands, so **this
baseline detects no page or fan-out regression against the levantamento-2 arm**, while the v0.0.5
arm measured beside it today is faster on every retained round.

### READ-1: the statement shape question

**Answer: neither form. The production Pulse fan-out statement carries no id list at all.** At the
pinned Community tree `72a2df3` — the tree the transfer Amdahl uses, and `kg_routes.py` is clean at
that sha (MEDIDO: `git status --porcelain` empty for the file; last touched by `be48635`) —
`_fetch_edges_for_nodes` (`src/okto_pulse/community/api/kg_routes.py:569`) issues, per relation
pair (`:606-615`, quoted verbatim from the source):

```python
f"MATCH (a:{from_type})-[r:{physical_rel}]->(b:{to_type}) "
f"WHERE {tpl.code_traceability_visibility_clause('a')} "
f"AND {tpl.code_traceability_visibility_clause('b')} "
f"RETURN a.id, b.id, r.confidence "
f"LIMIT 5000"
```

with `{"include_code_traceability": include_code_traceability}` as the only parameter, and the
page's node ids are applied **in Python afterwards** (`kg_routes.py:625`:
`if src in node_ids or tgt in node_ids`). `tpl.code_traceability_visibility_clause` expands to a
`coalesce(<var>.kind_of, '') <> '<subtype>'` conjunction guarded by `$include_code_traceability`
(`okto-pulse-core-mpulse5-transfer/src/okto_pulse/core/kg/cypher_templates.py:132-146`), so no
term binds the anchor to a key set. A grep for `IN $` across
`okto-pulse-kg-health-grafx-codex/src/okto_pulse/community/` returns **no match at all** (MEDIDO):
this Pulse tree has no anchored-IN read anywhere. The two other fan-out shapes in the same file
(`:709` edge counts, `:984` layer histogram) are likewise unanchored full-table matches.

The harness measures the **older** form. `bench_kg_arm.py:59-63` reimplements `_fetch_edges_for_nodes`
as of `523e759` (CITADO for the provenance of that reimplementation:
`.grafx-tmp/levantamento2/REFRESH_HEAD_a726744.md:48-54`):

```python
f"MATCH (a:{from_type})-[r:{physical}]->(b:{to_type})"
" WHERE (a.id IN $from_node_ids OR b.id IN $to_node_ids) "
"RETURN a.id, b.id, r.confidence LIMIT 5000"
```

That is an anchored pattern with a relationship, but the predicate is a **disjunction over both
endpoints**, not the single-list `a.pk IN $list` that `NODE-IN-SEEK` serves; and it is not the
statement the current Pulse sends.

Consequence for the queue: READ-1's premise — "the fan-out of one hop, the shape
`_fetch_edges_for_nodes` uses, plans a full `NodeScan` because `standalone` is False"
(`.grafx-tmp/perf07/recon/SYNTHESIS.md:129-144`, CITADO) — does not hold against Community
`72a2df3`, because that statement gives the planner no key set to seek with in the first place.
Per the rule the synthesis itself set ("if the Pulse statement does not have the anchored form, the
item falls to the end of the queue", `SYNTHESIS.md:143-144`), **READ-1 falls to the end of the
queue**. The 3.74× shown for it (`SYNTHESIS.md:133-135`, CITADO) remains true of the probe shape
and of the `523e759` harness shape; it has no consumer denominator in this Pulse tree.

## 4. Production vector search A/B

Instrument `vec_arm.py` + `rv_common.py` (unmodified), board `.grafx-tmp/lev2/survey6/VECTOR-verify/boards/b300u`
(DIM 384, the production statement with the seven real tombstone reasons), `read_only=True`,
`descriptor_revalidation="generation"`, 12 queries × 3 passes, 4 rounds with the arm order rotated,
round 0 discarded, pass 0 of each round treated as the warm-up.

Provenance printed by each arm and checked before the numbers were accepted (MEDIDO): HEAD arm
resolved `D:\Projetos\Techridy\okto_grafx\src\okto_grafx\__init__.py`, v0.0.5 arm the worktree's
`src`. `vector_math` is `"numpy"` by default on **both** arms (`runtime/config.py:292` in each
tree), so this is not a numpy-versus-pure comparison; the reservation recorded as F4
(`SYNTHESIS.md:34-36`, CITADO) does not apply between these two commits, because `4b632c0` is an
ancestor of `83cc313` (MEDIDO).

| metric | HEAD (values per round; median) | v0.0.5 (values per round; median) | paired ratio per round | median | label |
|---|---|---|---|---|---|
| per-query median, ms | 68.5, 96.0, 74.9; **74.9** | 69.6, 71.7, 66.4; **69.6** | 1.016, 0.747, 0.886 | 0.886 | MEDIDO |
| per-query minimum, ms | min-of-min **60.5** | min-of-min **51.8** | — | — | MEDIDO |

The band contains 1 (one of three rounds above it); this sample does not establish a difference
between the arms on the vector path. Digest `08e2cded59d0…` identical on both arms and all rounds
(MEDIDO). Last recorded numbers on the same board and harness (CITADO,
`W1CSE_RESULT.md:104-105`, arm `14da62b`): per-query minima 72.3 75.8 95.0 96.1 ms, per-query
medians 85 92 110 117 ms — today's HEAD is at or below that band, again on a differently loaded
machine.

## 5. `verify('all')` A/B on a copied board, cold handle

Instrument `ab_verify.py` → `verify_arm.py` (unmodified). Each arm-round copies
`.grafx-tmp/lev2/survey5/CKPTCERT-verify/board/db` (19 MB; 20 tables, 4,600 rows, 28 indexes,
163 heap pages, `page_size=8192` — CITADO `.grafx-tmp/levantamento2/W2CKPT_RESULT.md:64-65`) into a
fresh temporary directory and opens it there (`verify_arm.py:31-36`), so every pass runs on a cold
handle of a private copy. 6 rounds × (1 dropped warm pass + 5 passes), arm order rotated,
round 0 discarded here.

| arm | minimum wall per round, s (rounds 1–5) | median of those minima | label |
|---|---|---|---|
| HEAD | 0.578, 0.569, 0.610, 0.649, 0.623 | **0.610** | MEDIDO |
| v0.0.5 | 0.603, 0.632, 0.571, 0.551, 0.504 | **0.571** | MEDIDO |
| paired ratio v005 ÷ HEAD | 1.042, 1.111, 0.936, 0.848, 0.808 | **0.936** | MEDIDO |

Oracle: 0 findings in every pass, single report digest `bb0ecf4b76ad…` across both arms, all rounds
and all passes (MEDIDO). The band straddles 1 (2 of 5 rounds above it): **no difference established
between the arms on `verify('all')`**. Last recorded numbers on the same board and harness (CITADO,
`W2CKPT_RESULT.md:73`): base `93c190e` 0.960 1.122 0.983 0.980 0.974 1.071 s and `88b6952`
0.561 0.617 0.575 0.577 0.574 0.572 s. Today's HEAD (0.610 s median of minima) sits in the
`88b6952` band, which is the expected place for it: CKPTCERT-1 and VERIFY-2 are in HEAD
(`SYNTHESIS.md:119`, CITADO).

For CKPT-2 this gives the denominator, not the lever: `verify("all")` is 5.54 s of the 70.19 s
transfer today, 7.9 % (MEDIDO, §1 composition table); the doubled index-page device reads that
CKPT-2 targets (3,870 device reads for 1,950 index pages — CITADO
`.grafx-tmp/perf07/recon/CKPT/REPORT.md:54,252-253`) were not re-counted in this session.

## 6, 7, 8. Native micro-instruments

**`tools/perf_round/baseline_runs.py` was not used as the runner, and could not be.** Two
independent, verified reasons (MEDIDO):

1. `psutil` is not installed in the repository venv (`import psutil` → `ModuleNotFoundError`).
   `baseline_runs.py` samples the process tree through psutil (`:577-618`); without it
   `peak_rss_bytes_tree` is `None`, which appends an error to every run (`:1009`), and runs with
   errors are excluded from the measured set (`:1056`). The runner would publish no aggregate at
   all.
2. The three instruments print their JSON to stdout and take no `--out` argument
   (`round005_native.py:149`, `round005_commit_journal.py:46`, `round005_projection.py:39`), while
   `baseline_runs.py` requires `{out}` in the child's argv (`:332`) and a `_perf_round` provenance
   object inside the child's JSON (`:170-172`). Producing either would have meant editing an
   instrument or interposing an adapter of my own, which the brief forbids.

What ran instead: each instrument four times as an independent process, run 0 discarded, each run
with its own isolated `TMP`/`TEMP` directory under `.grafx-tmp/perf07/baseline/tmp/`, with
`PYTHONPATH=src` and the module form the instruments expect.

```text
PYTHONPATH=D:\Projetos\Techridy\okto_grafx\src TMP=<isolated> ^
  .venv\Scripts\python.exe -u -B -m tools.perf_round.<module>   (x4, run 0 discarded)
```

### 6. `round005_native` — open, populate, reopen, concurrent write (3 measured runs)

| initial nodes | new open, ms | warm selective page, ms | warm unindexed range, ms | 20 preflight reads, ms | stage 11 rows, ms | commit 11 rows, ms | checkpoint, ms | reopen, ms |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 128 | 355.98 (331.6..383.6) | 14.06 (13.2..14.1) | 8.50 (7.4..10.3) | 54.02 (52.5..68.3) | 47.93 (47.2..48.6) | 62.50 (61.9..62.7) | 97.52 (93.7..101.2) | 103.58 (102.7..110.3) |
| 512 | 115.25 (110.0..122.3) | 73.67 (51.4..113.0) | 39.88 (31.6..44.7) | 76.88 (65.7..113.0) | 72.68 (71.8..95.2) | 122.37 (95.0..137.9) | 103.13 (89.2..104.1) | 160.29 (146.2..176.9) |
| 2,048 | 146.66 (135.6..172.7) | 81.07 (75.6..97.4) | 107.63 (99.7..136.0) | 68.28 (67.5..81.3) | 69.70 (69.4..93.0) | 108.52 (103.2..122.9) | 108.09 (98.1..108.9) | 168.78 (166.6..191.2) |

All MEDIDO, medians of runs 1–3 with min..max. The 128-node row's "new open" is the first open of
the process and carries the import and first-touch cost; it is not comparable to the other two.
Different result sizes across these fixtures must not be read as scaling.

| participants | writer ms, median (min..max) | reader ms, median (min..max) | conflict retries |
|---:|---|---|---:|
| 1 | 202.8 (164.7..214.9) | — | 0 |
| 2 | 222.4 (194.7..247.5) | 57.8 (56.8..60.9) | 0 |
| 4 | 403.2 (365.5..560.5) | 83.6 (73.9..125.5) | 0 |

MEDIDO, pooled over the three measured runs. Small workloads with independent handles; not a
throughput or p99 measurement, and not a multiprocess crash test.

### 7. `round005_commit_journal` — DML commit, checkpoint and verify on disk (3 measured runs)

| mode | median ms (min..max of the three run medians) | label |
|---|---|---|
| commit history disabled | **23.82** (23.56..24.90) | MEDIDO |
| commit history enabled | **30.69** (30.48..32.57) | MEDIDO |

Ratio enabled ÷ disabled 1.29 (MEDIDO). The instrument's own scope line: "single-node update,
512-byte pages, 16 samples per mode, alternating order". `txn_manager.py` sha256
`dae8a3130689ed20449bbeb267e89646771cff11191bcfc3a99727e8c46edc73`, identical across the three runs
(MEDIDO). The earlier one-shot observation on this HEAD was 56.38 / 73.64 ms
(CITADO, `.grafx-tmp/perf07/recon/INSTRUMENTS/REPORT.md:210`), measured on a demonstrably busier
machine and with `TMP` on the system drive; today's runs used an isolated `TMP` on `D:`. The gap is
a factor of 2.4 and is unexplained by anything measured here: treat both as observations of their
own machine state, not as a change in the engine.

### 8. `round005_projection` — ordered page with a 384-d vector and overflow (3 measured runs)

| arm | median ms (min..max of the three run medians) | label |
|---|---|---|
| projected scan | **28.56** (26.66..29.06) | MEDIDO |
| full oracle (`_closed_node_scan_projections` patched off) | **33.52** (29.42..33.67) | MEDIDO |

Oracle ÷ projected 1.17 (MEDIDO): 256 rows, 128-row page, 384-d vector, 12,000-character overflow.
The earlier one-shot observation on this HEAD reported full-oracle samples of
95.98 / 136.09 / 73.51 / 74.49 ms (CITADO,
`.grafx-tmp/perf07/recon/INSTRUMENTS/REPORT.md:212`) against today's 29.4 / 33.5 / 33.7 ms medians.
The same caveat applies: two machine states, one instrument, no engine claim.

## 9. Checkpoint bucket-floor replay — attempted and failed

The gate condition was met: checkpoint is 5.26 s of the 70.19 s HEAD transfer, **7.5 %** (MEDIDO),
above the 5 % threshold. The instrument refused to run on HEAD:

```text
python -u -B .grafx-tmp\amdahl\lb3_bench.py D:\Projetos\Techridy\okto_grafx 8 1
→ TypeError: run.<locals>.counted_prepare() got an unexpected keyword argument '_resolved'
  (src/okto_grafx/engine/index_manager.py:7300, apply_partitioned_replay_batch)
  → GrafxConfigurationError: The public checkpoint operation failed because a collaborator
    raised TypeError
```

`lb3_bench.py` wraps `IndexManager._prepare_common_replay_batch` with a counting closure whose
signature matches the pre-0.0.6 call site; HEAD calls it with an added `_resolved` keyword. This is
an instrument defect against the current engine, not an engine defect, and the instrument was not
edited (exit code 1, full traceback in `.grafx-tmp/perf07/baseline/logs/lb3_bench.log`, 37.8 s).
**LB-3 / D-3 therefore have no measurement in this baseline.** Repairing `lb3_bench.py` is a
prerequisite for any checkpoint bucket work.

## What was not measured, and why

- **The 256 MiB buffer-budget arm (LV-1).** The instrument cannot pass connect options and the
  brief forbids editing it. See §1b for the exact one-line change that would unblock it.
- **The checkpoint bucket-floor replay (step 9, LB-3/D-3).** The instrument is stale against HEAD
  (§9).
- **`a726744` itself.** No arm was run at that commit, so the 58.49 s → 70.19 s difference cannot
  be split between code and machine beyond the bound the v0.0.5 arm gives (§1 comparison).
- **A quiet machine.** A live Pulse server and the concurrent Codex agent held 1.4–3.1 cores for
  the whole session. Absolute walls are inflated; paired ratios are the only defended numbers.
- **Syscall and call-count denominators.** No probe counted `validated_versions`, certificates,
  `page_count`, `lstat` or device reads in this session. Every such number in the reconnaissance
  reports (for example CKPT-2's 3,870 device reads for 1,950 index pages,
  `.grafx-tmp/perf07/recon/CKPT/REPORT.md:54`) remains CITADO against probes, not against these
  denominators.
- **`kg_path.py`.** Deliberately out of the minimum set: it measures the same page and fan-out as
  step 3, pays a 47-second build transfer per execution and writes no partial JSON
  (CITADO `.grafx-tmp/perf07/recon/INSTRUMENTS/REPORT.md:154-157`). Its counters remain the tool of
  choice for a follow-up refinement.
- **`python -m bench.harness` (the D5 multiples).** `ladybug` is not installed (MEDIDO); the gate
  would report UNMEASURED and exit 2.
- **Memory.** `round005_lifetime.py` needs `psutil`, which is absent (MEDIDO). PERF-MEM has no
  instrument on this machine, and CKPT-7 therefore has no number.
- **`py-spy` profiles and anything needing an authenticated Pulse home.** Out of scope and not
  installed; no live Pulse database was touched at any point.
- **The write-path A/B ceilings (W-01, W-02, W-04).** Not re-measured; the 1.226× ceiling remains
  CITADO from `.grafx-tmp/perf07/recon/WRITE/REPORT.md:187-188`, measured against a 50-row commit
  probe, not against the transfer.
- **Multi-writer and concurrency denominators.** `tools/measure_concurrency.py` and
  `tools/measure_traversal.py` were not run; they reconfirm published shapes, not this baseline.

## Files written

Everything below `.grafx-tmp/perf07/baseline/` (git-ignored): `RUN_MANIFEST.md`;
`amdahl_{head,v005}_i{1,2,3}.json` plus their `.partial.json` siblings; `amdahl_aggregate.json`;
`kg_v007.json`; `verify_v007.json`; `vec_{head,v005}_r{0..3}.json`;
`micro_round005_{native,commit_journal,projection}_run{0..3}.json`; `rest_aggregate.json`;
`preflight_v005.json`; the five `manifest_*.jsonl`; the drivers `run_amdahl.ps1`,
`run_kg_and_verify.ps1`, `run_vec.ps1`, `run_micros.ps1`, `run_chain.ps1`; the aggregators
`analyze_amdahl.py`, `analyze_rest.py`; and `logs/` with one log per process. The harnesses and
boards copied in step 0 are under `.grafx-tmp/lev2/`. The v0.0.5 worktree stays at
`C:\Users\jpamb\AppData\Local\Temp\claude\D--Projetos-Techridy-okto-grafx\af4999d8-4020-4079-92b8-e72fcf94f04f\scratchpad\grafx-v005`
(the only git state change made by this session).
