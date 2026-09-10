# 0.0.6 native history / catalog / search round

Fixed authorized scope: items 1–8 after `204bd2e`, branch `feature/v0.0.6`.
This report records development evidence, not publication, merge or installation
in Pulse. Source and all stores used for tests are isolated from production.

**All eight fixed items meet the local development DoD together.** Feature tests,
broad regression with fully reconciled corrections, final grouped acceptance and
consumer documentation validation are complete. Exact limits and platform exclusions
remain below; no wider CAP-2/CAP-3/CAP-5 or release claim is implied.

## Implementation mapping

| Item | Implementation | Consumer contract |
| --- | --- | --- |
| 1 | `system_history_publication.py`, native manager/redo, catalog bit 15 | [Native history](../SYSTEM_TIME_HISTORY.md) |
| 2 | `system_history_reader.py`, temporal DTOs, Database/Transaction APIs | [As-of/versions](../SYSTEM_TIME_HISTORY.md#read-contracts) |
| 3 | Native controls/retention, verify, physical backup, explicit logical transfer | [Retention and operations](../SYSTEM_TIME_HISTORY.md#retention-and-explicit-protection) |
| 4 | CLI parser/discovery/commands, native catalog/workspace APIs | [JSON CLI](../CLI.md#explicit-catalogs-and-workspace-resolution-006-development) |
| 5 | `catalog_copy.py`: RID selection, skip nodes, policy-bound receipt counts | [Copy](../CATALOG_COPY.md) |
| 6 | Canonical positional chunks, derivation-aware bucket routing, phrase evidence | [FTS positions](../FULL_TEXT_SEARCH.md#durable-positional-postings-006-development) |
| 7 | Immutable complete-content decode LRU in `posting_hash.py` | [Memo bounds](../POSTING_HASH.md) |
| 8 | `check_v006_upgrade.py`, isolated real wheels and platform CI | [Compatibility](../V006_COMPATIBILITY.md) |

## Evidence recorded during implementation

- Native history initial crash cuts: 14 passed (59.65 s), including activation,
  update and two recoveries per cut. Later retention integration is separately tested.
- Temporal/pin/codec batch: 41 passed (3.70 s).
- Updated positional/history operations batch: 13 passed (34.51 s), including
  retention cuts, physical backup/restore and current-only logical export.
- Existing copy regression: 22 passed (31.34 s); new subset/skip cases: 2 passed.
- Catalog/workspace CLI: 3 passed. Posting memo: 2 passed.
- Existing phrase/prefix regression: 19 passed (5.71 s).
- Adversarial temporal inputs/damage/concurrent retention: ten initial focused
  cases passed; the added positional damage test exposed a public-verifier gap.
  The verifier now checks the complete multi-key live FTS set and duplicate
  physical postings rather than asking a multi-key definition for one scalar key.
  Corrective positional/verifier regression: 93 passed (2.72 s).
- Real installed-wheel matrix: all ten cells passed on Windows 11/Python 3.13.1,
  pure/native-CRC+NumPy writers, opposite-selector readers, required-bit refusal
  and unchanged payload hashes. The 0.0.5 wheel hash matches the published receipt:
  `4f09d7e3ba1b8c7b716aa0b278bea2f39428db68fea71b27f56521f3b4bffcc5`.
  Rebuilt candidate 0.0.6 hash, independently rerun through all ten cells:
  `0b2b4843c843bf586b60f5fdfe6be71b3b5b7f0c88e773174f0074aeab5403d8`.
- Public-contract/CLI/documented-example corrective batch: 2,143 passed (35.06 s).
  Explicit additions cover temporal errors/DTOs, catalog/workspace commands and
  the existing logical-view facade. Public annotations and export declarations
  were completed, including affected modules from the preceding checkpoint;
  validation rules were not disabled.
- Final observability correction: 680 passed (22.65 s); both temporal error
  labels are now in the existing bounded metrics domain (cardinality cap unchanged).
- Final code-language/documentation correction: 931 passed (18.40 s), including
  the previously missing temporal pin collector docstring.

Counts overlap; do not add them. Logs are local ignored artifacts under
`.grafx-tmp/v006-*`.

## Broad regression and corrective reconciliation

The full suite completed on Windows/Python 3.13.1 in **2,130.18 s**:
**16,825 passed, 3 failed, 19 skipped**. This is the original result, not rewritten
as a clean single invocation. The three failures were:

1. Metrics label coverage omitted `history_unavailable`/`history_expired`.
2. Emitting those omitted codes was therefore refused by the metrics sink.
3. The temporal-pin collector lacked a code docstring.

All three were corrected. The complete observability batch (680 passed) and
language/documentation batch (931 passed) above revalidated them. JUnit identifiers
from the full run were matched against passed cases in those corrective runs:
**zero unresolved failures**. These are 16,828 distinct successful cases after
reconciliation, not the sum of overlapping regression batches.

The 19 skipped entries are **18 POSIX-only cases** on Windows and **one optional
Ladybug benchmark module** absent from this environment. No new feature test was
skipped. This does not claim a POSIX run or a Ladybug performance comparison.

Reproduction (PowerShell, from the repository; dependency paths are test-local):

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src') + [IO.Path]::PathSeparator + (Join-Path (Get-Location) '.grafx-tmp/v006-optional-test-deps')
$env:PULSE_CORE_BASELINE = 'D:/Projetos/Techridy/okto-pulse-core-corpus-baseline'
$env:PULSE_COMMUNITY_BASELINE = 'D:/Projetos/Techridy/okto-pulse-community-corpus-baseline'
python -m pytest --tb=short --maxfail=30 --junitxml=.grafx-tmp/v006-full-regression-final.xml
python -m pytest tests/observability --junitxml=.grafx-tmp/v006-observability-final.xml
python -m pytest tests/test_language_surface.py --junitxml=.grafx-tmp/v006-language-final.xml
python tools/check_documentation.py
```

The corpus baselines used were core `f602c7cc2f6a9f5ef446d4c991309196bd4667c7`
and Community `befaf1e4f9da9d0cff7cfc0f4aee177ef0a3e595`.
Source/module parity of the rebuilt wheel: **203 Python modules, byte-for-byte**.

## Final grouped acceptance

The final source passed **1,306 tests in 151.05 s**, with **zero failures, errors or
skips**, in one grouped run of native history/crash cuts, typed temporal queries,
retention/backup/adversarial/docs tests, selected copy, CLI inventory, positional
FTS, posting memo, metric catalog and code-language contracts. Receipt:
`.grafx-tmp/v006-delivery-final.xml`.

```powershell
python -m pytest tests/api/test_system_history_native.py tests/api/test_system_time_queries.py tests/api/test_system_history_operations.py tests/api/test_system_history_adversarial.py tests/api/test_system_history_documentation.py tests/api/test_catalog_copy.py tests/cli/test_catalog_workspace.py tests/api/test_fulltext_positions.py tests/api/test_posting_hash_memo.py tests/observability/test_metrics_catalog.py tests/test_language_surface.py -o addopts= -q --tb=short --timeout=60 --timeout-method=thread --junitxml=.grafx-tmp/v006-delivery-final.xml
```

The final wheel above independently passed all ten compatibility cells. API
reference regeneration, documentation checks (links/anchors, 39 connection
fields, public signatures/DTOs and 11 preserved plans), Ruff E4/E7/E9/F and
`git diff --check` passed. Roadmap, README capabilities, configuration, CLI,
copy, temporal, FTS, backup/transfer, operations and API consumer docs are updated.

## Exact limitations retained

Temporal activation is bounded; reads fold retained history rather than using
temporal secondary indexes. Retention redacts expired payloads but does not shrink
files or erase old WAL/backups. No valid time, bitemporal diff, universal historical
embedding-space service or time-travel Cypher is claimed. Copy `skip` preserves
existing nodes, not relationship deduplication across different request keys.
Positional FTS retains heap/visibility validation and has no measured universal
speedup. Memo reuse is pure decoded structure, never publication authority.
Configured Windows/Ubuntu Python CI is not evidence that remote jobs executed.
