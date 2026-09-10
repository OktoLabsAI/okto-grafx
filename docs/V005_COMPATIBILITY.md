# 0.0.5 compatibility matrix

This document is an execution matrix, not a claim of publication or release-wide
certification. No existing Pulse data participates in these checks.

September 10 default-acceleration amendment: the latest 0.0.5 revision installs
NumPy and google-crc32c as base dependencies. `[accel]` remains an empty compatibility
alias. New `DatabaseConfig` instances default to `codec="numpy"` and
`vector_math="numpy"`; explicitly saved `pure` or `auto` values are preserved.
This is a constructor/packaging default change, not a format migration. NumPy codec
bytes remain interchangeable with the pure codec; vector arithmetic follows the
existing tolerance contract (near-tie rankings can differ). Native checksum
acceptance, WAL, OCC and multi-reader/writer guarantees are unchanged.

The continuation after `970aa1e` adds detached weighted algorithms, opt-in NumPy
PageRank and optional Pandas/Parquet adapters. Default Python algorithms and Arrow
APIs remain; no connection field, WAL effect or required format bit is introduced.
The tagged Parquet vector representation is an optional interop encoding, not a
native database format. See [contracts](TABULAR_AND_PARQUET.md) and
[current local evidence](reports/V005_AFTER_970AA1E.md); other platforms and minimum
dependency versions are not certified by one installed Windows environment.

The continuation after `7dde256` adds only transient projected scans/algorithms and
explicit Arrow vector interop. No new persistent capability or connection default.
Malformed encapsulated vector parameters now receive native target-space validation.
Current Windows source, optional-Arrow and bare-source checks are recorded in the
[eight-item receipt](reports/V005_AFTER_7DDE256.md); they do not certify other platforms.

| Axis | Local status | Reproduction |
| --- | --- | --- |
| Windows / Python 3.13 / explicitly pure selectors | 2 feature-transition tests passed (strict and generation descriptors) | `tests/api/test_v005_compatibility.py` |
| Windows / Python 3.13 / NumPy codec+math and native CRC → pure reopen | 1 test passed | Same test module |
| Isolated real 0.0.4 wheel → current 0.0.5 source | Four upgrade cases passed; default reopened by 0.0.4, three new capability opt-ins refused by 0.0.4 | `python tools/check_v005_upgrade.py --legacy-wheel <0.0.4-wheel>` |
| Windows / Python 3.11 and 3.12 | Not run locally | New CI compatibility workflow |
| POSIX / Python 3.11, 3.12 and 3.13 | Not run locally | New CI compatibility workflow |
| Bare installation without optional dependencies | Fresh wheel and sdist installed-consumer smoke passed on Windows/Python 3.13; additional bare-wheel search/backup smoke passed. Full bare-profile suite not claimed | [Pre-Pulse receipt](reports/V005_PRE_PULSE_VALIDATION.md); CI bare profile remains separate |
| Bare source, Windows / Python 3.13, `-I -S` with site packages disabled | Passed scalar/sparse/verification and typed missing-Arrow refusal; no optional imports | Local isolated source probe, recorded in round receipt |
| Optional Arrow scalar batches | 3 local feature tests passed | `tests/api/test_arrow_export.py` |
| Local regression coverage with corrective verifier reruns | 15,904 distinct passes, 18 platform skips, no unresolved failures; grouped runs, not one clean uninterrupted invocation | [Round receipt and exact reports](reports/V005_NEXT_EIGHT_PROGRESS.md) |
| Latest pre-Pulse candidate, `8c3f6f2` | One complete local regression: 16,228 passed, 18 attributed POSIX skips, zero failures; installed-wheel recipes and cross-selector checks: 11 passed | [Exact artifacts, execution conditions and storage caveat](reports/V005_PRE_PULSE_VALIDATION.md) |

`.github/workflows/v005-compatibility.yml` runs Windows/Ubuntu × Python 3.11–3.13 ×
bare/extras. It retains JUnit evidence even on failure; extras installation fails
visibly rather than quietly changing the profile. The existing whole-suite CI and
cross-family gates remain. A checked-in workflow is not evidence that remote jobs
have run or passed. Missing environments remain explicitly unmeasured.

## Persistent upgrade boundaries

Local legacy artifact: tag-build 0.0.4 wheel SHA-256
`bb44971934a5cadb77e1621e2ad202451a7450f277240ec86e55562aa763a532`.
Both interpreters explicitly checked their imported versions; subprocess `-I`
prevents the source checkout's PYTHONPATH from masking the older package.

Default opens do not activate the new opt-ins. A default 0.0.4 store can be opened
and appended by 0.0.5 without these capabilities; this does not promise arbitrary
downgrade after other 0.0.5 features have been enabled.

| Opt-in | Required capability | Prior readers |
| --- | --- | --- |
| `statistics_history_entries>0` | bit 8, plus FTS bits 5/6 | Must refuse |
| `vacuum(index_free_pages=True)` | bit 9, plus reclaim bit 1 | Must refuse |
| `layout="sparse_hash"` | bit 10; header format 4; large bit 7 when applicable | Must refuse |
| `prefix_max_characters>0` | bit 11, plus FTS bit 5 and configured statistics/history bits | Must refuse; known-bitmap refusal and native replay tested in this continuation |
| Full-text relationship STRING properties | bit 12, plus FTS bit 5 | Must refuse; backup/logical transfer/native replay tested |
| HNSW memory budgets, scalar registry, Arrow import/export, repeated-key decoding, detached projections | No persistent capability | Stored ordinary values/bytes unchanged |

The earlier real-wheel matrix above predates bits 11/12. Its four-case result is
not evidence of a real-wheel test for these two additions; their current local
evidence is separately recorded in [this round](reports/V005_AFTER_69ED311.md).

Activation is deliberate and persistent; turning a runtime option off does not
remove capability bits. Back up before activation. Physical backup preserves
capabilities; logical transfer re-creates declared destination indexes. Do not
manually clear bits or edit headers to force an older binary to open a newer store.
Current refusal tests mask only the reader's known bitmap and separately exercise
real older-wheel opens; these are distinct pieces of evidence.

## Continuation after a4dd85a

Prepared analytics, simple topology, label propagation, NetworkX/Arrow graph exchange,
Polars and CSV/JSONL import introduce **no persisted capability or connection default**.
Optional CI profiles now include `polars` and `networkx`; the new tests run in both
bare/extras profiles. Local Windows/Python 3.13.1 checks use NumPy 2.5.2, NetworkX
3.6.1, Polars 1.44.2 and PyArrow 19.0.1. Polars is in an ignored local test directory,
not installed into Pulse. Bare-source `-I -S` checks cover missing optional selections.
These observations do not certify minimum dependency versions or the remote OS/Python
matrix. [Current acceptance evidence](reports/V005_AFTER_A4DD85A.md).
