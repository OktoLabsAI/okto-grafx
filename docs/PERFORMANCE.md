# Okto Grafx — Performance

Measured numbers, with the conditions that produced them and the instruments that reproduce them.

## Normative gate status — 2026-09-01

Performance measurements are now **informational only**. Throughput, latency percentiles, RSS,
CPU load, syscall counts, the former `7.5/s` floor, D5 ratios, and temporal parity with Ladybug do
not block M-PULSE-7, Pulse compatibility, the integrated audit, or the `0.0.1` release. Historical
thresholds and results remain below for provenance and regression analysis; any statement that they
block a later gate is superseded by this section.

The remaining release gates are quality gates: no unexplained semantic divergence, corruption,
WAL/durability or recovery/reopen failure, concurrency safety violation, query operation timeout,
`verify("all")` failure, source-authority/provenance mismatch, or functional regression. Performance
can still motivate a later optimization, but cannot turn a quality-clean run red.

**How to read this document.** Okto Grafx's test suite asserts *behavior*, never timings — a test
that asserts a duration fails on a loaded machine and proves nothing on a fast one. Performance is
measured by **instruments kept in the tree** (`tools/`), so every number here can be re-run by
anyone, and each section names the instrument, the rig, and the machine state it ran under. Two
project lessons govern this page: a claim nobody can re-run is worse than no claim (L31), and a
measurement taken on a loaded machine is a measurement of the load — every canonical run below was
taken with the machine otherwise idle, and says so.

Version 0.0.1, commit `eaad9c2`. Pre-alpha: these are the numbers of a young engine, recorded
honestly, ceilings included.

---

## 1. Test machine and build

| | |
|---|---|
| CPU | 11th Gen Intel Core i7-11800H @ 2.30 GHz, 8 cores / 16 threads |
| RAM | 32 GB |
| Disk | NVMe SSD |
| OS / filesystem | Windows 11 Home (10.0.26200) / NTFS |
| Python | 3.13.1 |
| Build | `[accel]` installed — native CRC-32C (`google-crc32c`), numpy 2.5.1 present |
| Configuration | `connect()` defaults: `page_size=8192`, `buffer_budget_bytes=64 MiB`, `max_open_files=128`, `partitions_per_table=64`, `identity_lease_size=64`, `descriptor_revalidation="strict"`, `metrics="noop"`, `checksum="auto"` (→ native) |

Cross-platform rows in §5 additionally used Ubuntu (WSL2, ext4) on the same hardware.

---

## 2. Concurrency under load — 4 writer + 3 reader processes

**Instrument:** `tools/measure_concurrency.py` · **machine idle** · one shared table with a
declared `PRIMARY KEY`, real OS processes (`spawn`), 46 s wall clock.

Workload: each writer commits **25 disjoint-range transactions** (5 rows each — the protocol at
rest, conflicts should be rare), then **15 contended updates** over 10 shared rows (the protocol at
work — conflicts expected and retried). Three readers run the whole time, each loop timing three
statement shapes inside one read transaction. Latency is **end to end per committed transaction,
retries included** — the number a caller experiences, not the number the engine flatters itself
with.

The run only counts if correctness holds, and it held: **500/500 acknowledged rows stored, zero
duplicates, zero phantom rows, zero torn reads in 6,207 reader rounds, zero non-`Grafx*` escapes,
`verify()` clean live and after reopen.**

### CE-1 paired concurrency check (2026-08-31)

The two-slot control-record candidate was compared immediately after its pre-CE-1 parent on the
same host, with no other Python test process. This comparison is the CE-1 gate; the older absolute
figures below remain the historical concurrency baseline and must not be mixed with this window.

| build | elapsed | point read median / p99 | write throughput | read throughput | correctness |
|---|---:|---:|---:|---:|---|
| pre-CE-1 `78e05e9` | 68.6 s | 4.15 / 15.31 ms | 7.3 rows/s | 66.3 stmt/s | PASS |
| CE-1 `93a3ee3` | 46.6 s | 4.34 / 16.05 ms | 10.7 rows/s | 86.1 stmt/s | PASS |

Point-read median/p99 moved **+4.6%/+4.8%**, not the apparent 3x obtained by comparing different
machine windows. Both runs stored 500/500 acknowledged rows and had zero duplicate, phantom or
torn rows, zero foreign exceptions, and clean `verify()` live and after reopen. Write latency is
convoy-sensitive, so this single pair is evidence that CE-1 preserved read behaviour and improved
the measured run, not a new throughput SLO.

### CE-2 concurrency acceptance (2026-08-31)

The unchanged instrument was repeated at integrated candidate `565ac37` after the reader-pin
change. This is a correctness and regression gate, not a causal timing comparison with the earlier
machine window.

| elapsed | write throughput | point read median / p99 | read throughput | correctness |
|---:|---:|---:|---:|---|
| 46.6 s | 10.7 rows/s | 3.16 / 12.97 ms | 173.9 stmt/s | PASS |

All 500/500 acknowledged disjoint rows and the 10 contended seeds survived. There were 152
retryable conflicts, zero process crashes, foreign exceptions, duplicate/phantom rows or torn
reads, and `verify("all")` was clean live and after reopen. The 2,700 reader rounds exercised a
point read, aggregate and bounded ordered scan while four independent writer processes committed.

### Reads, under full write load

| statement | n | median | p90 | p99 | max |
|---|---|---|---|---|---|
| point read by primary key | 2,069 | **1.52 ms** | 3.80 ms | 5.74 ms | 7.12 ms |
| `count(*)` over ~510 rows | 2,069 | 10.49 ms | 21.15 ms | 28.77 ms | 36.23 ms |
| `ORDER BY id LIMIT 50` | 2,069 | 12.10 ms | 22.68 ms | 32.41 ms | 38.01 ms |

**134.8 statements/s across 3 readers, while 4 writers hammered the same table.** This is snapshot
isolation doing what it promises: readers never block, writers never block readers, and the
primary-key index holds its ~1.5 ms median under full contention.

### Writes

| phase | n | median | p90 | p99 / max |
|---|---|---|---|---|
| disjoint key ranges, 5 rows/txn | 100 | 313 ms | 3,450 ms | 5,078 ms |
| contended shared rows, 1 update/txn | 60 | 328 ms | 1,282 ms | 2,476 ms |

10.9 rows/s aggregate; 254 retryable conflicts, all retried to success.

**The caller-side lever, measured and mostly refuted.** Single-writer commit on this rig costs
95-112 ms median, so the 313 ms above is ~3x queueing. Three retry-backoff configurations, same
engine, same workload:

| caller backoff | median | p90 | p99 | conflicts | throughput |
|---|---|---|---|---|---|
| uniform 2-20 ms | 313 ms | 3.45 s | 5.1 s | 254 | 10.9 rows/s |
| expo + jitter, cap 1 s | **207 ms** | 3.69 s | 7.2 s | **134** | 10.8 rows/s |
| expo + jitter, cap 0.35 s | 278 ms | **2.52 s** | noisy | 179 | 10.8 rows/s |

A commit-scaled exponential backoff halves the median and the conflict count -- worth doing, and
the instrument and the README's retry example now ship it -- but **throughput is pinned at ~10.8
rows/s in all three and the tail is unfair in all three**: a writer backed off to its ceiling keeps
losing to fresh arrivals, and no client-side backoff adds fairness to a convoy on a serialized
resource. Backoff chooses where on the median-vs-tail curve a caller sits; moving the curve is
engine work.

**Read the write table with its diagnosis, which this measurement produced.** The disjoint phase
has the *worse* tail — writers whose keys never touch conflicted constantly. The cause is
structural and recorded: every row-writing commit declares interest in **heap page 0** (the table
directory, unconditionally, since the E1 fix), so any two write commits in the database intersect
and optimistic validation serializes them. Effective write concurrency is ~1; end-to-end latency is
queueing over this platform's ~300 ms commit cost (the Windows publication gap, §5) with a retry
backoff too small to de-synchronize the queue. The obvious fix — declare page-0 interest
only when the directory changes — is **void, verified against the code**: `next_record_id` lives in
the page-0 directory entry and every insert spends it, so the write is real every time. The real
levers are format/protocol work (identity-range leasing per participant, per-table directory pages,
merge-able directory records) and are laid out with their blast radii in
`docs/architecture/PUNCHLIST.md` under *"Heap page 0 is a global write lock, measured"*.

---

## 3. Traversal — relationship endpoint indexes

**Instrument:** `tools/measure_traversal.py` · **machine idle** · 1,800 `C` nodes, 400 `E` nodes,
3,600 `M` edges (2 per C node, random targets, seeded), default configuration.

| query shape | before CF-17¹ | **now** |
|---|---|---|
| reverse hop into one entity — `MATCH (c:C)-[:M]->(e:E {id: 11})` | 1,462–1,834 ms | **192.9 ms** |
| forward hop from one node — `MATCH (c:C {id: 7})-[:M]->(e:E)` | — | 23.8 ms |
| two hops out and back | — | 45.9 ms |
| hop range `*1..2` | — | 29.0 ms |

¹ 1,462 ms measured on the pre-index tree in the original 2,500-node knowledge-graph smoke; 1,834
ms on this exact rig before the fix. Both predate the in-tree instrument and are kept as the shape
of the defect; the **now** column is the re-runnable claim. The round-6 blind review independently
confirmed the direction on the same tool (1,128.6 → 319.2 ms under concurrent machine load — a
loaded-machine datapoint, retained to show the spread).

Every `CREATE REL TABLE` gets two EXACT indexes over its endpoints (`ef_`/`et_`). Traversal expands
a seek/unknown frontier by index lookup, retaining the 64-start hybrid fallback, while a frontier
known from its plan to be `NodeScan`/`AllNodesScan` performs **one grouped edge scan immediately**.
The distinction avoids speculative probes on a scan-shaped plan, which previously measured
1,830 ms for 1,800 lookups where the grouped scan paid ~220 ms. `QueryResult.statistics` exposes
`edge_lookups` and `edge_scans`, and the instrument proves **index-vs-scan equality** by staling the
indexes and comparing answers.

**Known ceiling, recorded:** a traversal whose target is *unbound* resolves landings by one scan of
the landing table per traversal (edges store record identities; identities carry no index yet) —
that is most of the 192.9 ms above, and the next structural lever. See PUNCHLIST, *"Traversal after
CF-17: the two levers left"*.

---

## 4. Point reads, inserts and edge creation as the graph grows

Measured across the CF-15 work (primary-key indexing) on this machine, default configuration.
The *before* columns were taken with a scratch script against the pre-index tree and are not
re-runnable as-is (recorded per L31); the *after* behavior is what the current tree does, and the
flat-vs-growing shape was independently confirmed by a blind review on a second rig.

| rows in table | point read | insert (per row, in 10-row txns) | one edge via two-pattern `MATCH` |
|---|---|---|---|
| 200 | 5.97 → **0.71 ms** | 16.1 → **11.9 ms** | 292 → **86.5 ms** |
| 800 | 25.4 → **1.10 ms** | 32.5 → **13.6 ms** | 587 → **96.6 ms** |
| 3,200 | 57.4 → **1.96 ms** | 89.3 → **14.1 ms** | 1,953 → **98.1 ms** |

The columns that grew linearly with the table — a scan wearing an index's name — are flat now:
point read ~29× faster at 3,200 rows, bulk load linear instead of quadratic, edge creation ~20×.
The residual ~90–100 ms per single-edge transaction is commit cost (§5), not lookup cost.

---

## 5. Durable commit vs a reference engine (binding decision D5)

D5 sets *relative* ceilings against LadybugDB 0.16.0 on the same machine: durable commit ≤ 10×,
point read ≤ 5×, open-with-replay ≤ 3×. Measured on this hardware, both operating systems, single
writer (the D5 shape), median of 30 commits:

| configuration | Linux (WSL2, ext4) | Windows (NTFS) |
|---|---|---|
| LadybugDB 0.16.0 baseline | 2.45 / 3.61 / 2.75 ms | 0.97 / 0.92 ms |
| Okto Grafx, pure CRC | 17.4× / 12.8× / 17.2× | 103× / 102× |
| **Okto Grafx, `[accel]` (native CRC)** | **5.65× / 4.89× / 5.88× — ceiling MET** | 79× / 82× |
| `[accel]` + lease retained (unsafe default, measured only) | 4.08× / 2.97× / 3.65× | 41× / 43× |

**The D5 commit ceiling is met on POSIX with `[accel]`, and missed on Windows.** The Windows gap is
control-file publication: ~16.5 ms per commit against ~0.13 ms on Linux (of which `CreateFileW` on
the source file is 11.4 ms), with fsync, locking and the directory itself ruled out by measurement.
It is the largest single item in W6 and the denominator behind §2's write latencies. The decision
record — why the ceiling stands unamended — is in `docs/architecture/COMPONENTS.md` under the D5
entry.

### M-7 WAL-only open/replay remeasurement (2026-08-31)

The frozen 30-sample D5 runner was repeated with the integration worktree at
`origin/main@530df34`, an explicit source pin, LadybugDB 0.16.0, pure CRC, 5 discarded warm-ups
and 2,000 records. The open/replay result moved
from the historical `3.51x`/`3.55x` miss to **1.88x**, inside the `<= 3x` ceiling:

| engine | median | p95 | minimum | kept samples |
|---|---:|---:|---:|---:|
| Okto Grafx WAL open + `scan_all()` | 232.351 ms | 261.143 ms | 197.763 ms | 30 |
| Ladybug crashed-database open | 123.633 ms | 147.456 ms | 94.369 ms | 30 |

The parent D5 command exits `1` because the same run also measures durable commit, which remained
outside its independent ceiling (`150.82x`); that exit does not change the M-7 verdict. This is the
historically frozen **WAL-only lower bound**: it constructs `WalManager` directly and never calls
`Database._open`, so it does not certify full product recovery and does not exercise QW-9 or ST-7.
The v1 calibration schema is not self-authenticating: commit, source root, command, dirty state and
the machine-idle assertion are bound by the explicitly retrospective same-session
`provenance.json` sidecar (SHA-256
`ec1ec8ecb09673f01b19a085f0efef67d7b3c2cbeb12b7b6e0a4254f373614e5`). The hash-pinned scratch
outputs are `m7-open-replay-530df34-pure/calibration.json` (SHA-256
`317091b280922e66a68e87dc39c9352b421ceb1e83ad26b641ab8f8431c0d37f`), `metrics.json`
(`067f66683a6c6b5bb0ef1ee547d8e736ddd505cd603099949371bb32d2064351`) and `run.log`
(`68c80b48fe0135c34d2f1dc7f49338f47fef9da0601b1b5a0a54284ba223bc20`).

### CE-1 same-code control-publication gate (2026-08-31)

`93a3ee3` replaces the per-commit rename publication of `writer.lease` and `commit.state` with a
crash-safe inactive-slot page write plus barrier. The H1-H8.1 run used the same Pulse checkouts,
same `continuous/per_family=5` shape and the certified operation digest
`c994255b0bf695040c972ce339cc5d580ec253d2146674664e7722cf6b5a7f81` as the preceding ST-7 run.

| discriminator | pre-CE-1 | first CE-1 run | final `75eac97` | final vs pre |
|---|---:|---:|---:|---:|
| `_windows_posix_replace` per instrumented operation | 4 | **1** | **1** | reader registration remains until CE-2 |
| `os.fsync` per instrumented operation | 9 | **6** | **6** | gate `<= 6` met |
| `LocalStorageDevice.list_files` per operation | 9 | **2** | **2** | gate `<= 7` met |
| `_open_descriptor` after warm-up | — | **2** | **2** | gate `<= 3` met |
| seven simple-family RAW medians, sum | 1,586.60 ms | 1,482.21 ms | **1,289.48 ms** | **-18.73%** |
| seven simple-family commit phase, sum | 674.81 ms | 455.90 ms | **406.52 ms** | **-39.76%** |
| all 12 RAW medians, sum | 3,401.74 ms | 3,539.92 ms | **3,078.01 ms** | **-9.52%** |
| all 12 instrumented medians, sum | 8,341.64 ms | 8,701.84 ms | **7,379.22 ms** | **-11.54%** |
| all 12 phase medians, sum | 3,456.28 ms | 3,577.08 ms | **3,087.96 ms** | **-10.66%** |
| setup, 110 commits | 397.7 s | 458.6 s | **443.6 s** | +11.54%; no gain credited |

The first CE-1 timing window had six of seven simple-family RAW medians improve but the aggregate
move `+4.06%`; it was retained rather than explained away. The final clean-window run improved all
12 families relative to that first run and put the aggregate below the pre-CE-1 window. Absolute
wall movement is still not treated as wholly causal: acceptance remains anchored to the publication
counts and the simple-family commit reduction. Both reports contain 180/180 samples and the same
logical digest. Final artifact `ce1-final-75eac97-64a9da6-pf5-h1h8_1.json`, SHA-256
`4f943009c93ff8c85061519e578b08f0da85e677aa158e4b649f8701029c8d31`; first-run artifact
`ce1-93a3ee3-64a9da6-pf5-h1h8_1.json`, SHA-256
`e031cb4c7ede4859e6513d8614cb625db9fef38ffa0c30960a8852d1b0f2d2ae`.

### CE-2 participant reader-pin gate (2026-08-31)

`565ac37` retains one durable reader registration per open participant and advances its conservative
LSN floor instead of publishing and removing a file for every transaction. The same H1-H8.1
workload, checkouts, logical digest and forensic image were retained.

| discriminator | CE-1 final `75eac97` | CE-2 `565ac37` | CE-2 vs CE-1 |
|---|---:|---:|---:|
| `_windows_posix_replace` per representative operation | 1 | **0** | per-transaction reader rename removed |
| `os.fsync` per representative operation | 6 | **4** | -2 barriers |
| `LocalStorageDevice.list_files` per operation | 2 | **2** | unchanged |
| `_open_descriptor` after warm-up | 2 | **0** | no per-transaction control open |
| seven simple-family RAW medians, sum | 1,289.48 ms | **975.79 ms** | **-24.33%** |
| seven simple-family commit phase, sum | 406.52 ms | **319.20 ms** | **-21.48%** |
| all 12 RAW medians, sum | 3,078.01 ms | **2,363.90 ms** | **-23.20%** |
| all 12 instrumented medians, sum | 7,379.22 ms | **6,428.55 ms** | **-12.89%** |
| all 12 phase medians, sum | 3,087.96 ms | **2,357.02 ms** | **-23.67%** |
| setup, 110 commits | 443.6 s | **392.8 s** | -11.45%; no causal credit |

The run completed 180/180 samples and 12/12 families with zero bad postconditions. Against the
frozen 787.00 ms Ladybug reference, the aggregate RAW ratio is **3.00x**. Acceptance is anchored to
the removed hot-path publications and the process-level WAL-retention proof, not to attributing all
wall-clock movement to CE-2. Artifact `ce2-565ac37-64a9da6-pf5-h1h8_1.json`, SHA-256
`bc305be2af31cd8769481f2ad4906ede3abc71f6e397a63cc3f993e0ad5fef14`. The dedicated three-scenario
instrument and its 47/47 result are documented in
`docs/architecture/CE2_READER_PARTICIPANT_PIN.md`. The final v8 artifact kept the long reader open
for 90.00 s and has SHA-256
`d0285b5197a1ac05133cac3c90ab02ae1bd021f86d2785ad6ad6ffa9c3d29b3e`.

Also measured there: index maintenance costs ~**6.6%** of total suite runtime (572 s → 610 s on an
idle machine; an earlier draft said 52% and was measuring a concurrent agent, kept as a lesson).

### CN-2 authenticated `same-10` disposition

CN-2 was measured on the exact integrated source
`7acb9d869a7a6b9a533033309a3126f4de704f5b`, with the frozen Pulse corpus and provenance hashes
`6dd6cf05316b38b01bade965b5fc5e4b118b1853f28c7aef705a8d283e0f0f0c` and
`655c0ec0a10d4ee272fe6e2e6eea0b584d165b8ae3645148a77cd16bd1c428c8`. The runner only permits
the `official` label for `scenario=all`, so the two valid `same-10` repetitions are authenticated
non-official evidence, not the full CE-3 matrix.

R1b (SHA-256 `b4d1fa1cff1de76f5ea9a5b21452e5f07ea4dea0897b73ed0274aa583d227e95`) measured RAW/H8
effective rates of `5.3178/s` and `4.1577/s`; R2 (SHA-256
`fdf28373c49e11fa4b19420fee8e170c2ebc5a1a2e8bdfe22a49ffd08b4612b6`) measured `1.3218/s` and
`4.5368/s`. Both runs passed their machine-idle assertions, every A workload completed 60/60, no
terminal refusal occurred, live and cold verification passed, finalization removed scratch and the
source remained unchanged. R2 RAW contained one typed pre-durable retry and a genuine B tail
(`p99 7392.2 ms`, maximum `11489.2 ms`); it is retained rather than selected away. An earlier
functionally valid attempt (SHA-256
`a270b74b5efdee630059b2d2211a387ce3a3752202debaa16fe31e8183d84dba`) is diagnostic only because
its initial CPU load was 43.2%.

Across the six valid H8 checkpoints, fenced A/C sections took `1086.6–2059.2 ms`, while all 978
data-barrier calls ran without the writer lease or commit section (`497.8–622.5 ms` summed per
checkpoint). Three ordinary foreign commits completed in those unfenced windows. Against four
historical monolithic fences, the longest continuous exclusion fell 49.26% at the median and 45.80%
at the maximum. CN-2 therefore moved the intended local durability work out of the cross-writer
fence, without changing WAL ordering or reader/writer guarantees. It did not make the whole
checkpoint faster: median root duration increased by about 38%, the two same-SHA RAW rates differed
by 4.02x, and normal-commit phase attribution remained inconclusive (2/62 conclusive sections).
No throughput delta was reproducible, and every pass remained below the former `7.5/s` floor. That
floor was retired as a gate on 2026-09-01; these measurements are historical and no longer block
the CE-3 instrument or the M-PULSE-7 quality run.

### ST-2 dual descriptor revalidation — accepted structural gate

ST-2 was explicitly authorised after the CN-2 disposition. It adds two public modes without
changing durable bytes or concurrency semantics: `strict` remains the Grafx default and proves the
physical identity of every cached descriptor hit; `generation` is an explicit Pulse-oriented
performance opt-in that amortizes proofs only for a closed canonical heap/catalog/index/WAL set.
Every control, metadata, temporary, orphan, malformed and unknown name remains strict. Full
refreshes advance an adapter-local generation; CE-3 partial refreshes invalidate only names proved
changed, while detached certificate reads and fenced page-0 CAS writes revalidate their exact name.
The complete risk and deployment contract is
[`architecture/ST2_DESCRIPTOR_REVALIDATION.md`](architecture/ST2_DESCRIPTOR_REVALIDATION.md).

The first generation-mode DDL F4 exposed a real stale descriptor after another participant moved a
speculative index to `index_orphan`; the targeted CE-3 invalidation made the subsequent proof read
the current canonical name before any WAL append. The final DDL cells passed in both modes with the
loser refused pre-WAL, LSN/byte counts unchanged and live/cold verification clean. The second F4
passed in both modes with a real pinned reader, checkpoint/recycle below its horizon, all newer WAL
segments retained, a writer killed without flushing and all 30 durable rows recovered cold.
The full repository suite, with the pinned Pulse baselines explicitly supplied, traversed 11,357
nodeids to 100% with exit 0. Ruff, compileall and diff-check also passed; an independent differential
review concluded GO with no high/medium generation-only defect.

The ST-2 structural PF5 gate is now closed on the immutable operation set
`c994255b0bf695040c972ce339cc5d580ec253d2146674664e7722cf6b5a7f81`, in `continuous` mode with
`per_family=5`. The authenticated checkouts are Grafx
`f0b55b7b6facc916118f342c774cb06e56bf17e3`, Community
`050ced9b79533d50efed453d53ed450984f75cf3` and Core
`ccc1f345ece1db89a274cfdd634bd4da27028f63`. Across the 12 instrumented family medians,
`_still_names` remained `424` (`<500`), `os.lstat` remained `4,137` (`<8,000`) and `os.stat`
fell from `2,393` to `1,727` (`<2,000`). The logical work is discriminated by unchanged counts:
`_read_page=323`, `read_fresh_page=146`, `write_page=96`, `acquire_board_binding=123` and
`Database._run_statement=148`. The largest family, `delete_edges_by_session`, changed only
`os.stat`, from `752` to `326`.

The full-route control artifact is
`D:\GrafxBenchEvidence\st2-control-batch-20260901-final-a01\st2-control-batch-generation-profile-pf5.json`
(967,588 bytes, SHA-256
`e4845553628ab217bcfbb74694e43103f418bd4c9c3a6b6760f10caf42c2c5a1`). It uses the same Grafx,
Core, operation set, mode and sample count; its Community checkout is the pre-patch
`b07bf3ef8cdd05bc1365a46c2411bca857ab2bb0`.

The production delta does not remove a statement fence. For a Board/Grafx transaction it reuses
the physical-route proof from the freshly authenticated binding while the exact database remains
pool-pinned, then compares the complete route snapshot and re-admits that handle by canonical path
and page size. Generic Board and every Global revalidation retain the full resolver component walk.
A real symlink/junction regression proves the pinned route still fails closed on a physical alias.
The final artifact is
`D:\GrafxBenchEvidence\st2-pinned-route-20260901-final-a01\st2-pinned-route-generation-profile-pf5.json`
(969,630 bytes, SHA-256
`384a7722ff6772a2e89ca95225ab759ec5c7cab5af9939f405ef7b5ec2802aae`). Its
`machine_idle_asserted` value is false, so it certifies the deterministic structural counters only;
it is not a temporal throughput result. Nexus handoff
`hof_566e4a5333b54b55946b5dc7ad416b36` independently recomputed the hash, checkout pins, three
gates and five invariant counters and was verified PASS.

Grafx's default remains `strict`; Pulse certification selects and records
`descriptor_revalidation="generation"`, because it certifies the controlled Pulse deployment that
opts into this policy. A strict control, if repeated, is a separate labelled artifact. Generation
numbers must never be presented as default-strict numbers. No temporal result is a release gate.

The authenticated post-ST-2 `same-10` quality run is preserved at
`D:\GrafxBenchEvidence\st2-same10-20260901-final-a01\ce3-same10.json` (SHA-256
`419d60d64747f1676210b2772b8d0efbb732d72283039aeebb4c89a59f75834c`). It completed A `60/60`
and B `194/194`, with zero conflicts, retries, refusals or reopens; live and cold verification,
generation, source authority and storage identity were stable. Its `4.421593/s` rate and CPU values
are recorded only as observations.

### F1, CE-3 and M-PULSE-7 gate chain

The M-PULSE-7 input ratchet is certified at Pulse Community
`6595abdcfa788dfa2cc8da1a53ff96c378790531` (base
`d44c82155e9884c556813ea96dec829be567c236`, branch
`origin/milestone/grafx-mpulse7-ratchet-530df34`) and pins Grafx
`d39e27435171574ab6f03bc1d17672b26bf163b2`. Its manifest physical/canonical SHA-256 values are
`d1777bb26aee2feae5c8d5f4593840c08bdc37474ad6be4bdfe5334daedd0192` and
`1e6e92fc3bae3b54d3052ca9055b7682a9d518927573e0ffcbfcbb4568cf9f93`; its corpus
physical/logical SHA-256 values are
`0997747ed8bb9172d05781a62e5f81e7694630b173aaa152ac9ea28daec9d13f` and
`b29334edf6e7c1e6b9419a4f3add84ede4baad94fdeaecb0c679261a78f241cc`. The focused gate passed
12/12 and independent audit passed 39/39.

This ratchet authenticates inputs only. The F1 instrument is now accepted: author object
`d87a0c6520683b2d22929165136f718d14a1d795`, squashed and published at
`main@2b8e9006218b9ccf013d914dfe98e32abaa5bdd3`. Its focused suite passed 61/61, static checks were
clean, and an independent read-only audit confirmed the frozen guards plus the exact 312-cell
matrix expansion. The official long matrix has **not** run, so F1 publishes no performance result.
The literal CE-3 instrument is also accepted: author object
`da7f5e41e851a4cb8bc14c404b2f19df3ec9efc7`, squashed and published at
`main@4f6201a1e2f520bfe747a9636af10b036984732d`. Its focused suite passed 70/70 and the combined
integration tools gate passed 136/136. The clean check-only artifact
`ce3-checkonly-da7f5e4/ce3-check-only.json` has SHA-256
`c2a643652caf6bd83748624f5a2808396c40c1467ae39bc190e9021242be1324` and only the expected
`check_only_has_no_measurements` shortfall. It authenticates the instrument but contains no
measurement. The authenticated `same-10` quality disposition is recorded above. ST-2, its A96/CF-12
contract, F4 multiprocess proof and structural PF5 gate are complete. The former ordering through
an official CE-3 performance matrix and a `7.5/s` entry floor was retired on 2026-09-01. M-PULSE-7
now proceeds directly under the quality gates stated at the top of this document; a future CE-3
matrix is optional performance evidence and cannot block it.

### D-26 exclusive-window instrumentation (0.0.2 development)

The 0.0.2 performance round adds bounded-cardinality measurements that locate write-commit cost
before selecting later optimizations. `oktografx_commit_window_duration_seconds` separates wait and
hold for the writer lease and `COMMIT_SECTION`; `oktografx_commit_phase_duration_seconds` partitions
the commit-section hold across `other`, `occ`, `materialize`, `build_records`, `append`, `barrier`,
`apply`, `flush`, `index` and `publish`. The phase durations reconcile to the section hold for a
complete timing sample. A typed coordination timeout closes its wait sample at the failure
boundary, not after participant unwind. An untyped acquisition failure cannot prove that no grant
occurred and therefore suppresses the whole trace.

The counters report page images logged, physical live-WAL bytes successfully appended (including a
segment header on rollover), actual buffer-pool flush calls, resident and retired-pinned frames
traversed by commit-time flush/modified/dirty checks, foreign durable commits completed, and batches
successfully retargeted. Buffer accounting is a data-only probe: it is attached after participant
serialization and detached before that section is released, so consecutive local commits cannot
steal or mix one another's accounting.

Collection is outcome-neutral. The no-op sink constructs no trace object; an enabled trace invokes
neither the sink nor the host-provided `Clock` while an exclusive window is held. It emits after all
three coordination layers are proven settled, and a hostile sink cannot alter the commit result.
Any uncertain acquisition or release suppresses the whole trace rather than invoking host code
while a boundary may remain held. The exact
`time.perf_counter_ns` observation used for diagnostics is the sole narrow G2 exception and is never
an input to liveness, WAL, visibility or durability. If that timer fails, duration samples for the
attempt are discarded while counters and the transaction continue normally.

The internal `retain_lease=True` policy emits no D-26 per-commit trace. Because its writer lease
remains live after the operation, there is no safe complete-trace callback boundary; emitting would
contradict A91. The public/default lease-per-commit policy is fully instrumented. Official probe
effect and disabled-overhead numbers remain pending P0.4 on an isolated machine; no temporal claim
is made from development runs while the live Pulse backfill is active.

---

## 6. Reproducing

```bash
pip install -e ".[accel]"
python tools/measure_concurrency.py    # §2 — ~60 s, prints latency profiles + correctness verdict
python tools/measure_traversal.py      # §3 — ~2 min, prints shapes + index-vs-scan equality
```

Both instruments build their database in a temporary directory, assert correctness before printing
numbers, and delete it afterwards. Absolute figures are machine-dependent; the *shapes* — flat vs
growing, index vs scan, read tails vs write tails — are the claims. If your run contradicts a shape
claimed here, that is a bug report we want: use the `correctness` issue template.

## 7. The honest summary

* **Reads are good, and stay good under write load**: ~1.5 ms indexed point reads at p50, single-digit
  p99, zero torn reads, on an engine that never blocks a reader.
* **Traversal is indexed** and no longer reads every edge per frontier node; unbound landings are
  the next lever.
* **Writes are correctness-first and currently platform-bound**: ~300 ms per durable commit on
  Windows (met ceiling on POSIX with `[accel]`), serialized across writers by the page-0 interest.
  Both halves are measured, recorded, and named as W6 work — not discovered by users.
