# Okto Grafx — Performance

Measured numbers, with the conditions that produced them and the instruments that reproduce them.

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
| Configuration | `connect()` defaults: `page_size=8192`, `buffer_budget_bytes=64 MiB`, `max_open_files=256`, `partitions_per_table=64`, `metrics="noop"`, `checksum="auto"` (→ native) |

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

### CE-1 same-code control-publication gate (2026-08-31)

`93a3ee3` replaces the per-commit rename publication of `writer.lease` and `commit.state` with a
crash-safe inactive-slot page write plus barrier. The H1-H8.1 run used the same Pulse checkouts,
same `continuous/per_family=5` shape and the certified operation digest
`c994255b0bf695040c972ce339cc5d580ec253d2146674664e7722cf6b5a7f81` as the preceding ST-7 run.

| discriminator | pre-CE-1 | CE-1 | result |
|---|---:|---:|---:|
| `_windows_posix_replace` per instrumented operation | 4 | **1** | expected reader registration remains until CE-2 |
| `os.fsync` per instrumented operation | 9 | **6** | gate `<= 6` met |
| `LocalStorageDevice.list_files` per operation | 9 | **2** | gate `<= 7` met |
| `_open_descriptor` after warm-up | — | **2** | gate `<= 3` met |
| seven simple-family RAW medians, sum | 1,586.60 ms | **1,482.21 ms** | **-6.58%** |
| seven simple-family commit phase, sum | 674.81 ms | **455.90 ms** | **-32.44%** |
| all 12 RAW medians, sum | 3,401.74 ms | 3,539.92 ms | +4.06%; no gain credited |

Six of the seven simple-family RAW medians improved; the source-deleted tombstone family moved
`+17.98%`. The all-family instrumented and phase sums moved `+4.32%` and `+3.50%`, respectively,
so CE-1 is credited only for the causal publication/commit reduction, not for an aggregate hot-path
claim. Setup took 458.6 s (397.7 s in the preceding window). The report contains 180/180 samples;
artifact `ce1-93a3ee3-64a9da6-pf5-h1h8_1.json`, SHA-256
`e031cb4c7ede4859e6513d8614cb625db9fef38ffa0c30960a8852d1b0f2d2ae`.

Also measured there: index maintenance costs ~**6.6%** of total suite runtime (572 s → 610 s on an
idle machine; an earlier draft said 52% and was measuring a concurrent agent, kept as a lesson).

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
