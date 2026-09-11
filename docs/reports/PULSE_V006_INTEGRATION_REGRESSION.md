# Pulse compatibility follow-up: Grafx 0.0.6

September 10–11, 2026. Follow-up to the
[eight-item language acceptance](V006_QUERY_LANGUAGE_ROUND.md), not another Grafx
feature round or a release. The engine remains unchanged by this follow-up.

## Compatibility work

The paired consumer sources are Community `okto-pulse-v003-kg-load-codex` and Core
`okto-pulse-core-kg5-codex`. Community pins `okto-grafx[accel]==0.0.6`. Core source
ownership queries use neutral `split` and zero-based list indexing, preserving
card aliases and child-source identity. No driver import or Grafx branch enters
production Core, and no new Settings option or persistent-data migration is needed.

Regression now executes actual cancellation/restoration handlers through the
Community native transaction provider, covering score clamping/original-score
restore, retries, timestamp conversion and unchanged unrelated sources. Owner
enumeration exercises native keyset pagination, including malformed references.

The broader tests exposed five obsolete fixture failures: one logical-transfer
test manufactured an orphan with plain `DELETE`, now correctly refused, and four
recovery tests referenced a removed Ladybug test factory. These were migrated,
not skipped. The endpoint test asserts deletion refusal and then injects a missing
scan endpoint to retain fail-closed export coverage. Recovery tests now use actual
Grafx directory storage, certified manifests and pointer cutover, checking live
byte preservation, authority loss and safe terminal reconciliation.

The shared Core conftest no longer eagerly imports the removed Ladybug bootstrap;
schema initialization uses the neutral schema port and an explicit temporary
`DATA_DIR`. Two natural-search tests were migrated from native Ladybug seeding to
the actual Community routed composition and fenced, durably completed transaction.
Other historical Core modules still import old test helpers directly; they are not
represented as a completed full Core-suite regression.

## Follow-up evidence

Final KG integration regression: **passed**, with zero failures/errors/skips in
the final batches below (Windows/Python 3.13.1):

| Check | Result | Receipt in `.grafx-tmp/` |
|---|---|---|
| All Community `*grafx*.py` adapter/integration files | 881 passed; 833.53 s | `pulse006-all-grafx-final.xml` |
| Routed graph, global recovery, API contracts, cognitive/outbox persistence and related workflows | 694 passed; 672.91 s | `pulse006-workflows-final.xml` |
| Core query contracts, safety, async I/O/cancellation, stale sweep and timestamps; normal conftest | 81 passed; 47.21 s | `pulse006-core-final.xml` |
| Core logical-transfer architecture boundary | 30 passed; 2.64 s | `pulse006-core-boundary.xml` |
| Corrected recovery/transfer cases and actual migration queries/writes | 17 passed; 45.75 s | `pulse006-recovery-transfer.xml` |
| KG/Settings component regression | 252 passed, 22 files; 74.37 s | `pulse006-frontend.xml` |
| Installed Community/Core/Grafx wheels, actual migration queries and writes | 6 passed; 12.57 s | `pulse006-installed-wheels.xml` |

These batches overlap; do not sum repeated tests into a unique coverage count.
The first broad passes retained five failures in `pulse006-all-grafx.xml` and
`pulse006-workflows.xml`; final reruns have separate receipts. No tests were
waived or timeout limits increased to close those failures.

The two broad Community selections are disjoint (1,575 tests). Core's two final
selections cover 111 tests; React covers 252. The 17 focused backend cases and
six installed-wheel cases are deliberately repeated validation, not additional
unique source coverage. The native batch emits one existing `record_property`
warning about JUnit formatting, not a test failure. Ruff on touched Python files,
diff whitespace checks, Grafx's documentation validator and Community's offline
local-wheel lock check also passed.

Native adapter tests use real isolated graphs; contract/route/component tests also
use declared doubles. React/jsdom checks use mocked HTTP and emit existing `act`
and missing-canvas warnings; they do not constitute a production browser session.
This follow-up is the affected KG integration regression, not all Pulse tests.
The previously completed **17,174-pass Grafx full regression** remains separately
recorded in the language acceptance report.

Community 0.3.3 wheel SHA-256:
`7e16b5a27ac61640bf0120a43f74fb6c2988005f8dfd75b08ea86d5d2a62c6de`.
The Grafx/Core wheel hashes remain those in the preceding report. All three module
origins were asserted under `.grafx-tmp/language-venv313`; installed tests disabled
conftest and source checkout injection. This venv uses system-site-packages for
transitive dependencies: this is wheel/code-origin validation, not a clean-room
dependency-resolution certification.

The consumer repository's `docs/GRAFX_V006_INTEGRATION_REGRESSION.md` records the
file selection and reproduction commands. Its migration guide links that report.
No global install/restart, production graph operation, publication, commit or push
was performed as part of this follow-up.
