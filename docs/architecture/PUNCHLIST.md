# Punch list — non-blocking findings, worked in W6

Carried from W0/W1 reviews. Each entry: component, file:line, what is untested or imperfect, and the
review that raised it. None of these blocks a sign-off (CONTRACT §13).


## C8 — Observability (round-4 review, 2026-08-20)

Raised by the round-4 critic. The four blocking items from that round were fixed; these are the
non-blocking remainder under CONTRACT §13.

- **C8 / `adapters/metrics_openmetrics.py:949-957`** — mutation M61 was reported by file:line only
  and I could not identify which constant it names, so it is recorded rather than guessed at. Pin
  whatever constant that range holds, the way `UNENUMERATED_LABEL_BOUNDS` is pinned. (round 4)
- **C8 / `adapters/metrics_openmetrics.py:148` `escape_label_value`** — a raw carriage return in a
  label value is passed through. This is exactly the 0.0.4 grammar, which requires escaping only
  backslash, double quote and newline, so changing it would alter what a conforming parser reads
  back; the behaviour is pinned by a test rather than changed. Revisit if a real consumer is found
  that frames on CR. (round 4, observation O1)
- **C8 / `runtime/bootstrap.py` `_DEFAULT_PORT_FACTORIES`** — empty until W4, so OR-6's end-to-end
  statement ("the default install exposes GET /metrics") is not yet closed by a test that starts a
  database and scrapes it. Not C8-owned; recorded so nobody reads the C8 suite as covering it.
  (round 4, observation O5)
- **C8 / `tests/observability/test_metrics_publisher.py`** — the battery anchor for mutation M34
  matches two tests that both assert on the specific port, so either placement is caught, but the
  anchor should be made unique. (round 4, self-raised)
- **C3 / mutation tooling (A95 root stamping)** — a battery does not stamp the root it is
  mutating, so a quiet-tree check cannot tell a fork-scoped battery from one editing the shared
  checkout and refuses conservatively for both. Costs a wait, never a wrong answer. Deferred by
  the coordinator for W1 as a convenience rather than a correctness fix. (round 5, self-raised)


## C1 — Storage core (round-8 review, 2026-08-20)

Raised by the round-8 critic as D3, D4, D5, D8 and D9, plus two mutation survivors. The coordinator
scoped this round to D1/D2/D6/D7. These five were already implemented and verified green before that
scope cut arrived, so they are recorded here as DONE rather than reverted -- W6 should spend no time
on them beyond confirming the named test still exists.

- **C1 / `engine/heap_store.py:_append` fitting branch (survivor M05)** — DONE. `page_count=length`
  was interchangeable with `extent.page_count + 1` for every case the suite reached, so the A40.2
  arithmetic ban was unfalsifiable. The two disagree only when the hint is already wrong, which is
  the only time the count is worth writing: after one A22 redo plus one append the entry claimed
  four pages for a chain of two. Pinned by
  `test_the_repaired_count_is_the_length_walked_not_the_stored_count_plus_one`. (round 8, D3)
- **C1 / `engine/heap_store.py` reserved-header-page guards, three sites (survivor M38)** — DONE.
  All three were masked by `_require_table_page`, which refuses the same page a moment later for a
  different reason, so deleting them left the suite green. Each now carries
  `field="reserved_header_page"`, pinned by
  `test_the_reserved_header_page_refusal_is_not_the_page_type_refusal` with the wrong-type case as
  its other side. (round 8, D4)
- **C1 / `engine/heap_store.py:_require_commit_number`, `insert`; `domain/model/record.py:
  _require_unsigned`; `domain/page/file_header.py:__post_init__`** — DONE, A11-revised in both
  directions. Caller arguments (`record_id` of any wrong shape, commit numbers wider than 64 bits)
  reached the header encoder and came back as `corruption_detected`, which FR-8/FR-10 route to
  truncation and quarantine; and a `page_size` read OFF DISK answered `configuration_error`, so the
  one state that most needs quarantining could never reach it. Pinned by
  `test_a_record_id_a_caller_cannot_store_is_not_an_integrity_incident`,
  `test_a_commit_number_too_wide_for_its_field_is_refused_as_a_caller_error` and
  `test_a_page_size_stored_in_a_file_header_is_damage_and_not_configuration`. (round 8, D5)
- **C1 / `engine/heap_store.py:_cache_is_usable`** — DONE. `except GrafxError` swallowed retryable
  device conditions into a silent re-walk; narrowed to `GrafxCorruptionDetected`, which is the only
  class that means "this cache entry no longer describes the file". (round 8, D8)
- **C1 / `tests/storage_core/test_heap_store.py`, 17 setup loops** — DONE ahead of the scope cut,
  and the reason it was not left: a loop gated only on `pages_of()` never ends when that walk
  regresses, `--timeout-method=thread` cannot stop the thread, and the session dies with no junit
  report -- which made three of the critic's own mutations unscorable. Every setup loop is now
  bounded by a count and gates on `pool.storage.page_count()`, which no walk under test computes.
  (round 8, D9)
- **C1 / `engine/heap_store.py:_append`** — the growing branch leaks one allocated page when a pin
  refuses between the page being written and the chain being relinked. G6 forbids reclaiming it, so
  a caller retrying a device failure leaks one page per attempt. Not blocking: no row is reachable
  and no result is wrong, and the sanctioned reclaim path is C6's. Recorded so C6 knows the leak
  exists. (round 8, self-raised)
## C0 — Foundation (round 9 critic)

- `tests/conftest.py:752` `_reads_the_platform` has **no caller anywhere in the repository**; `PLATFORM_READS:591` and conftest's `PLATFORM_NAME_HINTS:602` exist only to feed it. Its docstring asserts a guarantee nothing enforces (A85/A86). Deleting it is acceptable.
- `tests/test_platform_parity.py:81` re-declares `PLATFORM_NAME_HINTS` (used at `:264`) although `:48-66` deliberately imports the family rule from `conftest.py` for single-definition discipline. One shared constant, one shadow copy (A24/A84 family).
- `tests/conftest.py:942` `_test_functions` under-counts dynamically generated tests (`globals()["test_x"] = ...`, `pytest_generate_tests`-only modules). Same root as blocker 3. **Hypothesis — not demonstrated.**
- `tests/conftest.py:1116` `_reconcile` returns `[]` on `session.shouldstop`/`shouldfail`. Both make pytest exit non-zero, so believed benign. **Hypothesis — not demonstrated.**
- **Environment, for whoever re-measures:** the editable install `__editable__.okto_grafx-0.1.0.pth` silently puts the *shared* `src` on `sys.path` for every 3.13 subprocess (A94). Any critic must pin `PYTHONPATH` and assert `okto_grafx.__file__` resolves inside its own fork.

## C3 — Process coordinator (round 4 rework)

- A95 root-stamping deferred: the quiet-tree check refuses while the component's own fork-scoped battery runs. Costs a wait, never a wrong answer.

## C3 — Process coordinator (round-5 final critic; component SIGNED OFF)

1. **Nonce entropy is 32 bits** — `_INSTANCE_NONCE_LENGTH = 8` hex chars. Two simultaneously-live coordinators sharing a configured `owner_id` collide at p ~= 2^-32, restoring the bug the nonce fixed. Widening costs one character of `_MAX_CONFIGURED_OWNER_LENGTH` (79). Not demonstrable.
2. **Reader-identifier budget is exactly 10^6** — `_READER_SUFFIX_BUDGET = 8`; the millionth `register_reader` from one instance raises `GrafxConfigurationError` from the encoder. Typed refusal, but it surfaces far from its cause.
3. **A zero-length `control/writer.lease` fails closed on every door**, including `acquire_writer_lease`. Deliberate and argued in the comment, but the database is unopenable until an operator deletes the file. **Runbook item, not a code change** — and see CF-1, which owns the general question.
4. **Stall-baseline staleness across an out-of-band lineage restart** — if the file is deleted and the same owner re-installs `(owner, 1, 1)`, an observer that never sampled the gap keeps its old baseline. Consequence is an early takeover with correct fencing. Present before this round's edit; not a regression.
5. **`detect_dead_owner` does not exclude the caller** — a holder that has not renewed reports itself. Harmless (self-takeover increments the epoch) and unreachable through `LeaseGuard`.
6. **`validate_epoch(-1)` raises `GrafxConfigurationError`, not `GrafxStaleEpoch`** — taxonomy nicety; both are `Grafx*`, no door leaks.
7. **`_owner_stalled`'s `self._owner_key is None` branch (`:1312`) is genuinely unreachable**, as its own comment states. Masked guard behaving correctly (§13).
8. **Hypothesis, undemonstrated:** if `_publish` fails *after* `atomic_replace` succeeds, the record is installed while `self._held` was never set, so the installer waits out its own stall threshold before taking over. Fail-closed and self-healing; fault injection not built.

## C0 Foundation — carried at sign-off (round 9)

Not defects blocking the component; recorded so a later round can decide deliberately rather
than rediscover them. None was demonstrated to vanish a test.

1. **`_reads_the_platform` (`tests/conftest.py`) has no caller.** The round-9 whitelist replaced
   every use with `_family_shape`, so the function survives with a live-sounding docstring and no
   effect (A85/A86). Deleting it is the cheapest resolution.
2. **`PLATFORM_NAME_HINTS` is shadow-copied** in `tests/test_platform_parity.py` alongside the
   authoritative copy in `tests/conftest.py`. The family rule itself is now shared by import, so
   the copy is inert — but it is a second definition of the A24 shape and should go the same way.
3. **`_test_functions` under-counts dynamically generated tests** (`pytest_generate_tests`,
   hypothesis). A module whose only tests are generated at collection time would not enter
   `_EXPECTED_MODULES`. Undemonstrated; the A69 reconciliation still covers such tests once they
   are collected, so only the disappearance half is affected.
4. **`_reconcile` returns `[]` on `session.shouldstop`/`shouldfail`.** Believed benign — the run
   was cut short deliberately and is already non-zero — but a conftest that sets `shouldstop`
   could suppress the backstop. Undemonstrated.
5. **A `trylast pytest_sessionfinish` in a nested conftest can reset `session.exitstatus = 0`**
   after the gate has spoken. Loud rather than silent: the terminal section still names every
   offender, so the evidence survives even if the status does not.
6. **Environment:** the editable install `.pth` points every subprocess at the shared checkout.
   A future critic measuring C0 must pin `PYTHONPATH` explicitly and assert `okto_grafx.__file__`
   resolves where it intends, or a probe will silently exercise the wrong tree.
7. **Subprocess probes are exposed to sibling churn.** `tests/foundation/test_skip_attribution.py`
   plants projects that import `okto_grafx` from the live `src/`, so a component mid-write
   surfaces inside these probes as `ImportError while loading conftest`. Observed once on 3.11
   (six failures) and not reproducible in three subsequent runs. A single red run from this file
   is not evidence of a C0 regression without a second run.

## C1 — Storage core (round-9 final critic; component SIGNED OFF)

1. **`CatalogStore.__init__` stamps `_loaded_epoch` at construction** (`catalog_store.py:73`), so a store that never read can `save()` its empty catalog over a populated file and silently empty the stored schema. **Demonstrated.** Not reachable through the documented open sequence (`bootstrap()` loads) and no composition root builds these stores yet — but the `__init__` docstring claims the opposite, so either the code or the docstring is wrong (A85). A sentinel epoch until the first `load`/`adopt`/`bootstrap` closes it.
2. **`test_a_retryable_refusal_survives_a_cold_reopen_with_one_live_version` passes vacuously if the injection stops injecting** — it suppresses the exception and never asserts `device.refused_writes`. It stayed green under the hook-disconnect mutation while its sibling went red. One line fixes it (A48).
3. **Sweep padding** — 2 of 12 and 4 of 14 parametrisations sit past the last refusing step. They assert the completed case, so not theatre; trim or document.
4. **A write-numbered sweep is only meaningful at a 2-frame budget** — at budgets >= 3 most operations perform *zero* device writes (measured). The pin seam carries the coverage; say so, or a future budget change silently defangs the write route.
5. **A third page state carries no `field`** — `page_type=HEAP` with zero slots refuses with "carries no page descriptor" and no `field`/`page_type` detail, unlike its two classified siblings, so **C6 cannot route it**. Reachable via `pool.allocate` + a hand relink, or a redo image.
6. **`bootstrap()` on an already-bootstrapped store discards unsaved in-memory tables** (it calls `load()`). Documented; no durable loss.
7. **Typed-object parameters raise `AttributeError`** (`scan(table, object())`, `insert(None, ...)`, and peers); scalars *are* guarded. Uniform across the component, and DoD §11.5 would reject C0/C2/C3 identically — fixing it here alone would raise the standard mid-review (§13). Also `version_chain(None)` returns `()` instead of refusing. **Cross-component; decide once in W6.**
8. **`DatabaseConfig` accepts `buffer_budget_bytes` below one page** (0 frames). C1 refuses correctly at store construction with a typed error naming the field; the composition root could validate against `MINIMUM_FRAMES` earlier. **Cross-component (C0/C11).**
9. The unwritten/page_type distinction is asserted at 2 of the 7 doors — acceptable (one shared guard), noted for completeness.

## C8 — Observability (round-5 review, 2026-08-20)

The blocking item from that round (the accept loop as the blocked party) and P7 were fixed. These
are the non-blocking remainder under CONTRACT §13.

- **C8 / `adapters/metrics_json.py` `RotatingFileWriter._rotate`** — P1: the rotation slots get no
  ownership check. The live path is claimed and re-checked, but `<path>.1` .. `<path>.N` are
  renamed onto and removed without asking whose bytes are there, and the `.N` namespace is the one
  place foreign bytes actually die. Claim the slots the same way the live path is claimed.
  (round 5)
- **C8 / `adapters/metrics_json.py` `_adopt`** — P2: a destination that exists but is empty or all
  whitespace is adopted, and then rotated like one of ours, while the class docstring says only a
  file this writer created is ever rotated (A85). Either narrow the adoption or correct the
  sentence. (round 5)
- **C8 / `adapters/metrics_json.py` `RotatingFileWriter.__call__`** — P3: a TOCTOU window between
  `_claim()` and `open(path, "a")`. Append through the fd the claim opened and verify it with
  `fstat` rather than re-opening by name. (round 5)
- **C8 / `adapters/metrics_json.py` `RotatingFileWriter.__call__`** — P4: `ValueError` and
  `LookupError` can escape (a closed file object, a bad encoding name). No port leaks them today
  because `JsonMetricsSink.publish()` converts both, but the writer is public and should convert
  them itself. (round 5)
- **C8 / `adapters/metrics_openmetrics.py`** — P5: a comment still describes a teardown helper
  thread that A91 removed. (round 5)
- **C8 / `tests/observability/test_metrics_publisher.py`** — P6: the two 200-connection
  parametrizations of `test_stop_reclaims_every_parked_connection` cost up to 25.7 s under load
  against the suite-wide 60 s cap, and when pytest-timeout's thread method fires it hard-exits
  with no junit, destroying the evidence of what failed. Give those two an explicit
  `@pytest.mark.timeout`. (round 5)

## C0 — Foundation (round 10 critic)

Recorded at the coordinator's instruction: append, do not fix. The three round-10 blockers (B1
support-matrix domains, B2 single-binding names, B3 collection read from pytest) were landed and
probed; these are the remainder.

- **Dead rules and their constants.** `tests/conftest.py:846` `_resolve_family_name`,
  `tests/conftest.py:908` `_reads_the_platform` and `tests/conftest.py:1003` `_names_a_family`
  are unreachable from the gate as it now stands. Their docstrings still assert guarantees that
  nothing enforces (A85/A86), and they keep two constants alive that disagree with each other:
  `PLATFORM_READS:594` (fed to `_reads_the_platform`, with `PLATFORM_NAME_HINTS:605`) against
  `PLATFORM_READINGS:636` (fed to the live `_platform_comparison` at `:645`). Two spellings of
  "what counts as reading the platform", only one of which decides anything. Deleting the dead
  three and their constants is the cheap resolution; keeping either one means reconciling the
  two lists deliberately.
- **The two halves disagree about membership.** `tests/test_platform_parity.py:269` normalises
  `Eq`/`NotEq`/`Is`/`IsNot` and a leading `not`, but not `In`/`NotIn`, while the static
  `ADMITTED_SHAPES` blesses `sys.platform in ("win32", "linux")`. So a condition the static half
  admits is one the parity half cannot pair with its negation, and an A32-style mismatch between
  the halves is exactly the shape of hole the last five rounds kept finding. Either teach the
  normaliser membership, or stop admitting it.
- **Aliased spellings are refused with no diagnostic.** `from sys import platform` and
  `import os.path as _p` resolve to nothing and the skip is simply reported as unattributed. The
  refusal is correct; the message does not say the reading was aliased, so an author reads
  "carries neither" and has no way to learn that renaming the import is what broke it. A named
  reason costs one branch.
- **`tests/foundation/test_suite_integrity.py` may charge itself for an unreadable file.** If a
  module under `tests/**` cannot be parsed, the duplicate-definition gate is believed to attribute
  the failure to the gate module rather than to the file. **Hypothesis — not demonstrated.**
  (Carried from round 9's style: recorded so the next reader does not have to rediscover it,
  explicitly not verified.)

## C2 — Storage adapters (CF-5 round, corrected in the B1 round, 2026-08-20)

- **RETRACTED by its author — C2 / `adapters/storage_local.py` `_rename_request`.** I recorded
  mutation CF5b (`FileNameLength` counted in code points) as **"measured equivalent"** on a 2x2
  grid of ASCII roots. Two things were wrong with that. The grid covered only the region where a
  NUL happens to terminate the buffer, so it could not see the failing cell (A93); and by
  declaring the sizing of this struct harmless I implicitly blessed the ADJACENT line, the name
  array, which was sized the same wrong way and was the B1 blocker. The claim is withdrawn. What
  replaces it is below, with its cells named.
- **C2 / `adapters/storage_local.py` `_rename_request` (B1b)** — counting `FileNameLength` in code
  points instead of UTF-16 units still survives. Measured identical in **12 cells**: {ASCII root,
  one non-BMP character, two non-BMP characters} x {four residues of the physical path length
  modulo four}, delivered against mutant, every cell publishing the record and leaving one
  directory entry. **Not covered**: a Windows build that honours `FileNameLength` over the
  terminating NUL (this build does not — probed directly), non-Windows families where the branch
  does not exist, and paths longer than those tested. The field is set correctly in the delivered
  code regardless; this records that no test can currently tell. (B1 round, self-raised)
- **C2 / `adapters/storage_local.py` `_windows_posix_replace` (CF5f)** — opening the rename SOURCE
  without `FILE_SHARE_DELETE` survives. Measured identical in four cells: {nobody holds the
  staging file, another process holds it} x {delivered, mutant}, all publishing correctly. Not
  covered: a holder that denies delete sharing on the SOURCE, and non-Windows families. (CF-5
  round; cells stated in the B1 round)
- **RESOLVED in the C11 round — C2 / `adapters/storage_local.py` `_is_permanent`.** Mutation P1c
  deleted the transient-wins precedence branch and survived the whole suite: no failure can reach
  it, because the two tables are disjoint and CPython derives errno from winerror. Rather than
  leave an untestable branch, the branch is gone and the property that made it unnecessary is now
  asserted by `test_the_two_classifications_of_a_failure_cannot_both_claim_it`, which fails the
  moment an editor lists a number in both tables. (C11 round, self-raised)
- **C2 / `adapters/storage_local.py` `_PERMANENT_WINERRORS`** — the table classifies nothing the
  errno table does not. Measured on CPython 3.13/Windows: every entry maps to an errno already
  listed (183 and 80 to EEXIST, 2, 3, 161 and 206 to ENOENT, 87 and 123 to EINVAL, 267 to
  ENOTDIR), and CPython derives errno from winerror, so no constructed or real failure can reach
  the winerror branch alone. Kept as a guard in case that mapping changes, and recorded here so
  nobody reads it as covered. (C11 round, self-raised)
- **C2 / `adapters/storage_local.py` classification, `ERROR_WRITE_PROTECT`** — winerror 19 maps to
  `EACCES`, which the transient table claims, so a write-protected volume is reported retryable
  although it does not clear on its own. Not changed: transient precedence is deliberate, the
  condition is outside the paths this engine drives, and flipping it would make an ordinary
  sharing violation permanent. Worth a decision in W6 rather than a unilateral change now.
  (C11 round, self-raised)
- **C2 / `domain/errors.py` `GrafxDeviceFull` and `EFBIG`** — a file that hit a file-size limit is
  reported with the frozen class default `retryable=True`, and freeing space does not help. Not
  changed because §2's table is FROZEN and the class flag is contract, not code. Cross-component:
  belongs with whoever revisits the taxonomy. (C11 round, self-raised)
- **CROSS-COMPONENT / C8 `adapters/metrics_json.py:211,253,267,284`** — the same defect C11 raised
  against me: `failure.strerror` is concatenated into four Grafx messages, so a non-English host
  puts a localized, non-ASCII sentence inside an exception G1 requires to be en-US. Raised by C2
  while fixing its own instance; not fixed here because the file is not mine. (C11 round, raised
  by C2 against C8)
- **C2 / mutation hygiene, self-raised (B1 round)** — one mutation in my own battery (`CF5a`)
  replaced a block in a way that left the module syntactically invalid, and the driver counted the
  resulting collection error as a kill. A mutation that cannot import is not a measurement. I
  re-ran it as a valid edit and it is genuinely killed by four tests, but the driver should
  refuse to score a mutant whose module does not parse. (B1 round, self-raised)

## C4 — Write-Ahead Log (round-1 critic; component SIGNED OFF)

1. **P1 — `recycle()` drops the durability obligation of a segment it KEPT.** `engine/wal_manager.py:1033` removes deferred names from `_unflushed` although the A27 branch **retains** the segment, so a kept segment stays volatile forever and a crash destroys its records while the manager reports the higher `last_lsn`. `truncate_after` gets the symmetric case right. Non-blocking because `recyclable_prefix` only offers segments below the horizon, so only records the same call declared reclaimable can lose their flush, and the reopened log is contiguous. Fix: `gone = (set(recycled) | set(deferred)) - {s.name for s in surviving}`. **Mutant M25 (applying that fix) SURVIVED all 242 tests** — no test has an unbarriered non-tail segment when `recycle()` runs.
2. **P2a — a checksum-valid record with `descriptor_len > MAX_DESCRIPTOR_BYTES` makes the log PERMANENTLY UNOPENABLE.** `decode_record` raises from inside a function documented as never raising, `_rebuild` never sets `_opened`, and `truncate_after` is then unreachable. Also a taxonomy smell (`configuration_error` for damaged bytes). **Routed to C4 as a C6 dependency.**
3. **P2b** — a segment past `MAX_SEGMENT_READ_BYTES` reaches the same dead end; the delivered bound makes the real trigger absurd, so shape only.
4. **P3** — a failure mid-pass can leave `_segments` describing files already removed; retry does not recover, though `open()` does.
5. **P4** — `recycle()` takes no durable barrier after its unlinks (`truncate_after` does). Hardening for filesystems that do not order unlinks.
6. **P5** — `barrier()`'s BR-4 guarantee depends on the metrics context manager not suppressing; not reachable today (both sinks return `False` / use `try/finally`).
7. **P6** — `RecycleReport` lists one segment as both `deferred` and `retained` in the A27 branch.
8. **P7** — raw non-`Grafx*` exceptions on four public domain doors under hostile arguments (65 cases). The critic **measured the same class against signed-off C1** (`page.checksum`, `model.value`, `model.record`) and correctly declined to charge C4 alone: §13 forbids a standard rising mid-review. **House-wide W6 item.**
9. **P8** — segment numbers can be reused across a reopen although the docstring says never; harmless in practice, docstring overstates.

**RETRACTION (A93):** C2's punch-list claim that mutation CF5b was "measured equivalent" is withdrawn. The 2x2 covered only the cells where a NUL happens to terminate the rename buffer; the cell where it does not (a non-BMP path character with `len(target) % 4 == 1`) is where the defence fails, and it is a live blocking defect. An equivalence proven over a subset of the input space is not an equivalence.

## C8 — Observability (round-6 review, 2026-08-20)

The blocking item (a non-deterministic test premise) was fixed. These are the non-blocking
remainder under CONTRACT §13, each with a demonstrated 2x2 from the round-6 critic.

- **C8 / `adapters/metrics_openmetrics.py` `start()`** — M12: the `server_close()` on a failed
  `thread.start()` is masked by CPython refcounting rather than redundant. Holding a reference to
  the server: unmutated the port is free, mutated it is still bound (WSAEADDRINUSE). The existing
  test passes either way because nothing holds the server and finalisation closes the socket. Give
  it the shape `test_closing_a_server_releases_its_port_while_a_reference_is_still_held` already
  uses for the ordinary path. (round 6)
- **C8 / `adapters/metrics_openmetrics.py` `_may_be_waited_for`** — M3: the accept-loop half is
  unpinned. Unmutated a non-owner accept-loop `stop()` returns in 0.000 s (12/12); mutated it takes
  2.002-2.015 s, exactly `shutdown_timeout` (12/12) -- and the suite passes both ways. The handler
  half asserts `waited < budget / 2`; the accept-loop half asserts only eventual return, so give it
  the same shape. (round 6)
- **C8 / `adapters/metrics_openmetrics.py` `_tear_down`** — `server.shutdown()` is the one wait in
  the teardown that `shutdown_timeout` does not bound; demonstrated at 3.68 s against a 0.5 s
  setting. Noted in the `stop()` docstring; bounding it needs a change to how the accept loop is
  broken. (round 6)
- **C8 / `tests/observability/test_metrics_publisher.py`** — the round-6 critic saw
  `test_an_unterminated_request_head_is_answered_from_the_size_bound` fail a second way once
  ("never answered at all", 3/400 in its repro, 0/500 in a near-identical variant) and recorded it
  as observed-but-unexplained. The sender now stops as soon as the connection is readable, which
  removes the unread-bytes-at-close path that produces it, and 400 trials here reproduced it 0
  times -- but the original was never explained, so it is recorded rather than declared fixed.
  (round 6)

### C1 — round-9 cross-component asks (C6/C7), 2026-08-20

- **C1 / `engine/buffer_pool.py:apply_page_image`** — survivor with an argument, not a gap in the
  suite. The door stamps `decoded.page_index` so a decoded page does not claim to be page 0 while
  being applied to page 7, but nothing in C1 surfaces that object: `replace_with` keeps the target
  page's identity by design, and the decode-failure path already re-raises with the right
  location. A mutation removing the stamp therefore survives. Recorded rather than covered by a
  test that would only appear to prove it. (round 9, self-raised)
- **C1 / `engine/heap_store.py` locator guards** — `ref.page == HEADER_PAGE_INDEX` (three sites)
  and `ref.slot < FIRST_RECORD_SLOT` (three sites) still answer `corruption_detected`, and they
  are decided from a caller-supplied locator before any byte of the target is trusted, which is
  the same shape as the freed-slot defect. They were NOT changed: unlike a freed slot, neither
  state is reachable with a reference C1 ever hands out -- every `RecordRef` C1 produces has
  `slot >= 1` and `page >= 1` -- so no ordinary operation can route itself into quarantine
  through them. Left as a deliberate decision rather than an oversight; if C6 wants them
  reclassified for symmetry it is a one-line change per site plus its test. (round 9, self-raised)
- **C1 / mutation tooling** — the fork sync copied only git-tracked files, so it broke the moment
  a sibling added an uncommitted package that `okto_grafx/__init__.py` imports: the fork could not
  collect at all and the baseline read as red. It now mirrors the working tree. Worth copying into
  any other component's battery driver that syncs from `git ls-files`. (round 9, self-raised)

## C8 — Observability (first POSIX run, C13 CI matrix, 2026-08-20)

Seven C8 failures on the first real POSIX leg. All were fixed; recorded here because the causes
are lessons, not just defects, and two of them were product bugs my Windows-only verification
could not have found.

- **Cause 1 (product, fixed): the accept loop ended on a Windows behaviour.** The teardown closed
  the listening socket and relied on the `select` that followed to fail. Measured: on Windows the
  loop ends; on POSIX closing a descriptor another thread is selecting on wakes nothing and the
  thread was still alive half a second later. The loop now ends on a flag the server owns, which
  is the same statement on both families, and `shutdown()` waits on an event that loop sets --
  which also bounds the one wait in the teardown that `shutdown_timeout` did not cover.
- **Cause 2 (product, fixed): file identity by stat is not portable.** The rotation-ownership
  guard compared `(st_dev, st_ino)`. Measured on Linux, a file unlinked and recreated at the same
  path reused the inode AND matched on `st_ctime_ns` and `st_mtime_ns` as well, while an ordinary
  append changed two of them -- so no stat-only scheme distinguishes a swap there. The writer now
  holds the descriptor it claimed: on POSIX that pins the inode so the reuse cannot disguise a
  swap, and on Windows it prevents the swap outright. This also closes P3 (the TOCTOU between
  claiming and opening) since the write goes to the descriptor that was checked.
- **Cause 3 (tests, fixed): probes bound the port the way a bare socket does.** Three assertions
  used `socket.socket().bind(...)`, which on POSIX is refused while a parked handler holds an
  established connection on that port, even though the publisher itself -- which sets
  `SO_REUSEADDR` there per A30 -- binds without trouble. The probes now bind with the options the
  product uses, so they measure the release rather than the platform.
- **Lesson worth keeping**: every one of the six earlier review rounds measured Windows only. The
  lifecycle work was correct there and half-verified overall, and no amount of depth on one family
  substitutes for the second. A30 exists because the right answer differs per platform; a guard
  that holds on one family and not the other is worse than one that fails on both, because it
  will be believed.

### C1 — round-10, the per-table row-identity allocator, 2026-08-20

- **C1 / `engine/heap_store.py:insert`** — `insert` now reaches `_extent_for` twice on the path
  that allocates a table's first page: once through `observe_record_id` and once through
  `_store_version`. The second is a `_find_extent` hit on a resident page, so the cost is a dict
  walk over the directory slots, not a device read. Recorded rather than optimised because
  collapsing them would mean threading the extent through the identity door, which is what made
  the tail hint hard to reason about in the first place. (round 10, self-raised)
- **C1 / mutation tooling** — one round-10 mutation (`I1-hand-out-then-spend`) was written to move
  the counter write after the hand-out and turned out to be a no-op: the inserted branch was
  always true and assigned an unused local. It "survived", which was correct and meaningless. A
  mutation that changes no behaviour is INVALID in the same sense as L7's unimportable one, and a
  battery should say so rather than counting it. Replaced with `I1-returns-the-unspent-id`, which
  is behavioural and is killed. (round 10, self-raised)

## C8 — Observability (G1/A7 sweep and its aftermath, 2026-08-22)

- **C8 / `adapters/metrics_json.py`** — fixed: four Grafx messages concatenated `failure.strerror`,
  which is neither en-US nor ASCII on a non-English system (G1, A7). The message is now ours and
  the platform's text travels in `details["platform_message"]` beside `errno` and `winerror`,
  matching the pattern C2 shipped. The source gate cannot catch this -- the literal is ASCII in
  the file and only becomes localized when the OS fills it in -- so a test forces a non-ASCII
  `strerror` across the create, write and read paths and asserts the rendered error is ASCII.
- **C8 / `adapters/metrics_openmetrics.py` `_MetricsServer.serve_forever`** — fixed: the accept
  loop carried two `_stopping` checks around the same wait, one in the loop condition and one
  after `select`. The second answered first, so the first was unpinned and a mutation of it
  changed nothing observable -- the masked-sibling shape (A83) inside a single function. Now one
  decision point, after the wait, and its mutation dies on both families.
- **C8 / `adapters/metrics_json.py` `_rotate`** — the rotate-time ownership check is provable only
  on POSIX. On Windows the held descriptor stops the swap outright, so the precondition the guard
  exists for cannot be created there and its mutation survives a Windows-only battery. Measured:
  caught on Linux, survives on Windows. The guard is load-bearing and proven where it is
  reachable; recorded so nobody reads the Windows survivor as dead code. (G4)

## C6 — Recovery, ledger, quarantine, verify (round-3 rework, 2026-08-22)

The round-3 critic's twelve punch-list items, all recorded here per the coordinator's scope cut
(CONTRACT section 13.1) whether or not they were closed. The two BLOCKING defects are not listed:
`verify()` reporting an allocated-but-never-written page as `page_checksum`, and retirement
destroying a healthy control record on six non-damage classes -- both fixed, each with a test that
fails against the old code.

### Closed in round 3 (kept per the scope cut; listed so W6 spends no time re-doing them)

- **1. `engine/quarantine.py` `_require_two_files` was an unfalsifiable L5 backstop** — DONE.
  Replacing the device listing with the operation's own answer (`found = {payload_file,
  manifest_file}`) left the suite green, and the test its docstring named is actually satisfied by
  `_payload_file_name`'s prefixing -- two mechanisms alibiing each other (A62/A67a). Pinned by
  `test_a_device_that_reports_a_write_it_did_not_perform_fails_the_capture`, which drives a device
  that publishes the payload under a garbled name and answers every read-back about the garbled
  name, so only the directory listing can see it. The docstring now says which test proves it and
  which does not.
- **2. `engine/ledger_store.py` `_require_unrecorded` raw-door guard untested** — DONE. Behaviour
  was correct; the test was missing, and a test through a TYPED door would have been satisfied by
  `_record_once` short-circuiting above it (A62). Pinned by
  `test_appending_the_same_damage_twice_through_the_raw_door_is_refused` (asserting `field`,
  `entry_id`, `file` and `offset`, which only this guard sets) with
  `test_a_raw_entry_describing_a_different_range_is_still_accepted` as its A85 other side.
- **3. `engine/recovery_manager.py` `refuse` skipping the ledger repair on a CLEAN log** — DONE.
  The existing test drives the DAMAGED-log path, where the refusal is raised before the repair
  could run at all, so it is satisfied whatever the branch does. Pinned by
  `test_a_clean_log_under_refuse_leaves_a_damaged_ledger_exactly_as_it_was`.
- **4. `engine/quarantine.py` `_find_by_suffix` manifest re-check untested** — DONE. `endswith` is
  a filter over names, and `sanitize_name` makes a genuine suffix collision ordinary
  (`1.wal-0-16` is a string suffix of `wal_1.wal-0-16`). Pinned by
  `test_an_entry_whose_name_merely_ends_with_the_suffix_is_not_that_range`.
- **5. `domain/ledger/textform.py` was LOSSY for non-BMP characters** — DONE, both halves.
  `format(ord(c), "04x")` emitted five digits for an astral code point and the reader consumed
  four, so an emoji-prefixed name round-tripped to a different name -- LESSONS L5's second lesson
  at a serialisation boundary. The writer now emits the surrogate PAIR, and per L14 the guard is on
  DECODE as well: a lone surrogate is refused as `corruption_detected` rather than parsed into a
  character that cannot be encoded back to UTF-8. Pinned by five tests including a parametrized
  lone-surrogate set and a manifest round trip; the corpus deliberately leaves the ASCII plane.
- **6. `engine/quarantine.py` digest-mismatch refusal named the REQUESTING origin** — DONE. It now
  names the origin the entry actually holds, carries the requester in `details["requested"]`, and
  is pinned by `test_the_refusal_names_the_origin_the_entry_holds_not_the_one_that_was_asked_for`.
- **9. `engine/verifier.py` identity comparison on a `str`** — DONE, and recorded because the fix
  is NOT behavioural. `kind is not UNCLASSIFIED` became `kind != UNCLASSIFIED`; the two agree on
  every reachable input, because `route_page_refusal` returns the module constant itself rather
  than an equal copy. Its mutation survives, correctly and meaninglessly -- an L18 no-op, so no
  test can pin it and none was written.
- **10. `retire_control_record` reported `ledger_entries_created=1` unconditionally** — DONE. It is
  now counted across the append (`len(ledger.entries())` before and after), so a retry after an
  interrupted retirement -- where `record_retirement` deduplicates on the damage identity and
  creates nothing -- reports 0. Pinned by
  `test_a_retirement_that_was_already_recorded_creates_no_second_ledger_entry`.
- **11. `RecoveryManager.__init__` took `policy=` while section 5 says `recovery_policy`** — DONE.
  `recovery_policy` is now the spelling; `policy` still works because `api/assembly.py` (C11's
  file, which C6 may not edit) passes it, and the two may not DISAGREE -- naming the setting twice
  with two different words is refused rather than resolved by an invisible ordering rule. Pinned by
  `test_the_policy_is_named_recovery_policy_the_way_the_configuration_names_it` and
  `test_naming_the_policy_twice_with_two_different_words_is_refused`.

### Recorded, NOT fixed

- **7. `engine/quarantine.py` `capture`/`list` — an interrupted capture leaks an orphan directory
  for ever.** The bytes go down before the manifest on purpose, so a crash between the two leaves a
  directory the listing skips; `list`'s docstring says "the next capture of the same range
  completes it", and that is **false with any real clock**. The entry name is `<stamp>-<suffix>`,
  so a later capture of the same range gets a different stamp, creates a new directory, and never
  touches the abandoned one. Nothing sweeps it, nothing reports it, and `count()` cannot see it.
  Either the docstring stops promising a completion that does not happen, or the store grows a door
  that reports and reclaims manifest-less entries -- and reclaiming is the destructive direction, so
  it needs the same evidence rule the retirement door has. (round 3, critic)
- **8. `engine/quarantine.py` `_read_entry` — an entry whose MANIFEST becomes unreadable is
  invisible with no recovery door.** `_read_entry` returns `None` for a manifest that will not
  parse, so `list`, `count` and `inspect` behave as though the entry does not exist while its bytes
  sit on the device. Failing closed is deliberate and defensible (deleting it would be this
  component destroying evidence to tidy its own listing), but there is no sanctioned way to see it,
  export it or retire it. This is CF-1's shape one layer in: whatever rule answers for a damaged
  reader record must answer for a damaged manifest. (round 3, critic)
- **12. No component ships a `ControlRecordProbe`.** The protocol is declared in
  `engine/recovery_manager.py`, and the only `read_control_record` in the tree is in
  `tests/recovery/conftest.py`. The evidence for retiring a damaged reader or lease record is
  INJECTED by design (G2 keeps the coordination decoders out of the engine), so C6 cannot ship the
  implementation -- but nobody else has, and until W4 wires one, C6's retirement door is
  unreachable in a real database. **Whoever wires it owns the predicate this round narrowed:** only
  a non-retryable `GrafxCorruptionDetected` is evidence of damage, so a probe that reported a stale
  epoch, a stolen lease or an access failure under that class would defeat the narrowing from
  outside. (round 3, critic)

### Raised by this round's own battery

- **C6 / `domain/verify/findings.py` `findings_at` (mutation survivor)** — dropping the
  `location.page` term leaves the suite green, because the report it is asked about carries
  findings of that kind on only one page. It is a convenience READER rather than a guard, and the
  property it serves (one page image is reported once) is independently pinned by four mutations of
  the per-walk ledger itself. Tighten it in W6 with a report carrying the same kind on two pages.
  (round 3, self-raised)
- **C6 / mutation tooling and the shared scratchpad** — two instrument faults worth carrying.
  (a) The first battery run read files as BYTES and matched newline-bearing anchors against them;
  five of the six targets are CRLF and one is LF, so nineteen anchors matched zero times. A70
  caught it and reported nineteen HARD ERRORS rather than nineteen phantom survivors, which is the
  mechanism working -- but the fix is to compare in ONE normalisation (A59), as the driver now
  does. (b) The scratchpad directory named as session-private in the harness prompt is **shared
  between components**: another component's driver overwrote a file named `battery.py` there while
  this battery was running. No measurement was harmed (the module was already imported and the
  other fork was a different directory), but a driver kept under a generic name in that directory
  is not safe. C6's now lives in `scratchpad/c6-round3-private/`. (round 3, self-raised)

## C8 — Observability (scope cut to CONTRACT §13.1, 2026-08-22)

Recorded, not fixed, under the round cap.

- **C8 / battery coverage** — the 88-mutation battery was mid-run when the round closed: 59 of 88
  applied, no survivors among them, on a private fork that never touches the shared checkout.
  Any survivor the remaining 29 produce is missing coverage, not a defect, and belongs here.
- **C8 / `adapters/metrics_json.py`** — the four `strerror` concatenations were fixed this round
  before the scope cut arrived. Recorded for the ledger that they were, by the coordinator's own
  test, punch-list rather than blocking: every one of them already wrapped the OSError into a
  `GrafxConfigurationError`, so nothing non-`Grafx*` escaped and no operating-system text ever
  reached a metric name, label or value. The defect was G1/A7 in the message text alone.
- **Not C8** — `src/okto_grafx/engine/buffer_pool.py` fails
  `test_no_string_literal_in_the_source_reads_as_portuguese` on both families (the marker
  `indice` inside an en-US sentence). It is the only failure in the 2,037 shared gate tests and
  it is C1's file; noted because it will show up in anyone else's gate run too.

## C9 — Vector subsystem (round-two battery, 2026-08-22)

34 mutations against a private fork, **21 killed / 13 survivors**. Every survivor named a real
missing or deleted test; none is an equivalent mutant and none is claimed as one. Under CONTRACT
§13.1 exactly one was promoted to blocking — **R20**, where the reverted guard is reachable by an
ordinary caller and its absence returns a deleted row. That test is written and verified
(`R20 KILLED, 299 tests, 1 failed`). The rest are recorded here.

Per-file kill rate: `domain/vector/key.py` 6/6 · `adapters/vectormath_pure.py` 2/3 ·
`engine/vector_engine.py` 13/25.

### Coverage this component's own rewrite deleted (A82)

These four guards HAD tests. Moving the suite onto C7's index framework removed them, the suite
stayed green, and only the battery saw it. The tests were rewritten before the §13.1 scope cut
arrived and are green, so they are recorded as DONE rather than reverted — deleting working
coverage to comply with a scope cut would repeat the harm the entry describes.

- **C9 / `engine/vector_engine.py:_search_exactly` (survivor R26)** — DONE. The duplicate-version
  refusal in the exact regime. Covered again by
  `test_two_visible_versions_of_one_record_are_refused_rather_than_ranked_twice[1000-exact]`.
- **C9 / `engine/vector_engine.py:_search_approximately` (survivor R27)** — DONE. The same
  invariant on the other path (A66: two doors, one rule). Same test, `[0-approximate]`.
- **C9 / `VectorHnswIndex.search` ranking (survivor R28)** — DONE. The record-id tie-break. The
  graph orders equal scores by internal node number, which is insertion order, not the caller's
  rule. Covered by `test_a_tie_is_broken_by_the_record_and_not_by_the_order_versions_entered`.
- **C9 / `VectorHnswIndex.search` beam width (survivor R29)** — DONE. `ef` raised to `k`, without
  which a narrow beam silently caps the answer below what was asked for. Covered by
  `test_the_beam_is_never_narrower_than_the_requested_neighbour_count`.

### Survivors written up before the cut, kept green

- **C9 / `VectorHnswIndex.stage_delete` (survivor R08)** — DONE. The key-shape check existed on
  `stage_insert` only. Verified `R08 KILLED (299 tests)`.
- **C9 / `VectorHnswIndex.search` staleness refusal (survivor R16)** — DONE. The engine door also
  refuses, so the index's own refusal was unobservable through the engine; a caller holding the
  index directly (verifier, maintenance pass, query planner) reaches it. Verified
  `R16 KILLED (299 tests)`. A67: each layer is now independently observable.
- **C9 / `VectorHnswIndex.apply` graph update (survivor R19)** — DONE. Redo reaching an
  already-built graph. The earlier idempotence test could not see it because a graph that was
  never built is rebuilt from the store and looks correct however `apply` behaved. Verified
  `R19 KILLED (299 tests)`.
- **C9 / `_note` on `IndexOperation.RESET` (survivor R21)** — DONE. The rebuild path had no
  coverage at all; a graph surviving a reset would rank versions the store no longer holds.
- **C9 / `_install` dimension check (survivor R30)** — DONE. A row whose vector has the wrong
  dimension. The arithmetic refuses it one layer down as a length mismatch, but that error names
  neither the index nor the page, so the guard carries `field` and `index` details only it
  produces (A62).

### Not fixed, recorded for W6

- **C9 / `domain/vector/hnsw.py:_search_layer_counted` — a selective filter disables pruning.**
  The stopping rule needs the result set FULL, and only admitted nodes are kept, so a filter
  admitting fewer than `ef` candidates makes the traversal visit the whole graph. Measured on 800
  nodes: 666 visited unfiltered, 800 under a one-in-seven filter, 800 under one-in-ninety-seven.
  It is why recall is 1.0 in exactly those cases and the opposite of what a regime chosen to
  avoid an O(n) scan is for. **Bounding it is a calibration decision, not a patch**: the same
  exhaustiveness is what makes the two regimes provably agree at the threshold, so a visit budget
  trades that proof and its recall for latency. Stated in the traversal docstring at the site.
- **C9 / `SCORE_TOLERANCE` is not a promise about ordering.** On catastrophically cancelling
  input the oracle (exact `fsum`) and numpy (pairwise) disagree by the whole magnitude of the
  answer: `dot([1e16, 1, -1e16], [1,1,1])` is 1.0 and 0.0, moving that candidate from first place
  to last. The docstring claimed 1e-9 was "far below any difference that could reorder a
  ranking"; corrected, and pinned by
  `test_the_tolerance_does_not_promise_agreement_on_cancelling_input`. numpy is optional and not
  the default, so no shipped configuration is affected.
- **C9 / a vector a dot-product space cannot rank is refused at graph-build time, not at write.**
  `(1e308, 1e308, -1e308)` is finite, in range, and storable; the overflow appears only when it
  is scored against another vector. The refusal is typed and names the entry, the store is
  unaffected, and the space recovers once the row is tombstoned and reconciled — but the space is
  unqueryable until then. Refusing at the write door would need a rule about magnitudes that
  depends on the other operand, which is not well defined; recorded rather than invented.


## C1 — Storage core, the catalog staging door (scope cut to CONTRACT §13.1, 2026-08-22)

Delivered this round: `CatalogStore.stage(catalog) -> tuple[(page_index, image), ...]` and its
offline chain builder `buffer_pool.build_chain_images`. Recorded here, not fixed, under the round
cap.

- **Sharper diagnosis than the one routed, and it is on the record because half of it is not about
  refusals at all.** The reported symptom -- *"declares 125 bytes but its chain carries 63"* --
  needs BOTH halves of `save()` to reproduce, and only one of them was named. `save()` writes
  through the pool (so a refused statement's pages are already reachable), **and `save()` returns
  the CHAIN pages only**. A caller that staged exactly what `save()` handed it therefore staged
  everything except page 0 -- the page carrying `root_page` and `payload_length` -- so **even a
  DDL that COMMITTED put its file header on the device through an ordinary pool flush, covered by
  no log record and unreachable to redo.** A crash between the two loses the schema and recovery
  cannot repair it, with no refusal anywhere. Reproduced through C1's own surface before anything
  was built on the diagnosis; `stage()` closes both halves by returning page 0 unconditionally.
- **C1 / `engine/catalog_store.py:save()` is still the old shape, deliberately.** It is left as the
  door for a change that is already settled (`bootstrap()`, and recovery adopting what it
  replayed), and its docstring now says so and names `stage()`. **Any future caller that calls
  `save()` inside a transaction re-opens both halves of the defect above.** A W6 option is to make
  `save()` return the header page too, or to route `bootstrap()` through `stage()` and delete
  `save()`; both are behaviour changes to a signed-off, heavily-tested path and are out of scope
  under a one-round cap.
- **C10 / `engine/query_engine.py:_schema` — CLOSED (CF-16).** The migration below landed, with
  the per-transaction working copy that the three-line sketch did not anticipate (same-transaction
  statement chains and the vector attach both needed it). Kept for the record: it used to say
  `catalog = self._catalog.catalog` (mutating the LIVE catalog) then `self._catalog.save()` then
  `self._stage(txn, file, pages)`. The replacement is three lines and is written out in the
  `stage()` docstring: `read_from_pages()` for the copy, mutate the copy, then
  `txn.stage_page_image(store.file, page_index, image)` for each pair `stage()` returns.
  `_stage()`'s pin-and-encode loop is not needed on the catalog path any more -- the images are
  already encoded, and pinning would read the OLD page, since staging deliberately writes nothing.
  Until this lands, the defect is still live end to end: C1 has built the door, not walked through
  it.
- **C10 / `engine/query_engine.py:_attach_vector_columns` — CONFIRMED and CLOSED (CF-16).** It was
  the same defect class: attach() installs registry and per-space state at statement time, and a
  rollback left both. `settle_schema` prunes the registry by table id and `discard_unknown` prunes
  the vector engine's map. Originally recorded as: it
  runs on the `CREATE NODE TABLE` path, before the commit, and calls `vectors.attach(table, space)`.
  If that installs index state that is not staged on the transaction, a refused DDL leaves an index
  attached to a table that does not exist. C1 did not measure it and does not own it; C7/C9/C10
  should confirm it is transaction-scoped or record it as the same defect in a second component.
- **C5 / `engine/txn_manager.py:_build_records` writes page 0 of `catalog.dat` before its chain
  pages**, because `staged` is `sorted(set(...))`. That is safe -- the log is the authority and the
  redo is idempotent, so a crash between two page writes is repaired by replaying the batch -- and
  it is recorded only so it is not re-discovered as a suspected ordering bug. C1 states the
  dependency in the ORDER `stage()` returns (chain, freed pages, then page 0 last); what the
  device sees is C5's business.
- **C1 / `buffer_pool.write_chain` and `buffer_pool.build_chain_images` are two routes to one
  answer.** The three list-and-file refusals are now written once and called from both, but the
  chain-building itself is written twice (one writes into live frames, one into fresh pages), and
  the only thing stopping them drifting is
  `test_the_images_a_chain_would_write_carry_the_chain_the_chain_would_have` plus
  `test_applying_a_staged_change_produces_what_a_save_would_have_produced`. A67(b) is satisfied by
  those two tests rather than by construction. Merging the two onto a single plan is a W6 option;
  it was not done here because it changes `write_chain`'s allocation behaviour, which is signed off.
- **C1 / `stage()` cannot see a foreign commit, and does not pretend to.** It reads the chain and
  the file header through the pool, so a participant with stale frames stages against a stale
  picture. That is not closed here and must not be: the protection is the write set -- page 0 is in
  every catalog change's images, so two participants always intersect and optimistic validation
  refuses one -- plus `BufferPool.begin_read_view(token)`, which C5's commit already calls with the
  published LSN. The honest statement of the limit is L22's, and it is unchanged by this delivery.


## C13 — Bench, calibration, coverage matrix and CI (blind-critic round, CONTRACT §13.2, 2026-08-22)

FIXED this round, each with a proving test that fails against the old code:

- **B1** — `bench/coverage/matrix.py::_covers_module` resolved a collection-level entry by dotted
  prefix, so a module that ran on NO family was certified COVERED by a different module living in
  a directory of the same stem. It now resolves against the source file pytest records under
  `junit_family=xunit1`, and an entry that carries no file is UNMEASURED rather than resolved by
  name. Test: `test_a_sibling_directory_named_after_a_module_does_not_cover_it`, on two real
  pytest sessions over a planted tree, through the real command line. The new UNDECIDABLE arm --
  a report whose collection-level entry carries no file, which is pytest's DEFAULT junit family --
  gets its own test rather than being left to the battery to find:
  `test_a_module_entry_with_no_source_file_is_unmeasured_and_not_a_violation`. It is neither
  covered (the hole) nor a violation (a false red on every default-family report); it is exit 2,
  and the message names the option that fixes it.
- **B2** — `.github/workflows/ci.yml` took each leg's verdict from junit `failures`/`errors` only,
  discarding C0's per-session gate, which writes no junit and fails a session through
  `session.exitstatus`. The suite step now records pytest's exit status and the verification step
  fails a leg on a non-zero status junit does not explain. Test:
  `test_a_leg_whose_session_failed_without_writing_junit_is_not_green`.
- **Item 1** (imminent, promoted by the coordinator) — `evaluate` returned COVERED with population
  0: "every reported node ran somewhere" is true of the empty set, and the vacuous reading was a
  green certificate over nothing. Reachable through the CLI by a matrix whose every module failed
  to import, since collection errors are excluded from the population by design. Now UNMEASURED,
  exit 2. Test: `test_a_matrix_that_reported_no_readable_node_is_unmeasured_and_never_covered`.
- **Item 5** (imminent, promoted by the coordinator) — `bench/harness/gate.py::read_multiples`
  raised `AttributeError` on valid JSON that is not an object (`[]`, `null`, `3`) despite a
  docstring saying "Never raises". Nothing caught it, so the interpreter exited 1 — this gate's
  code for CEILING EXCEEDED — on a document that was never read. The shape is refused before
  `.get`, and `main` now catches a stray exception and returns 2, so the exit-2 guarantee is
  structural rather than a list of anticipated shapes. Test:
  `test_valid_json_that_is_not_an_object_is_unmeasured_and_not_a_ceiling_failure`.

Everything below is RECORDED and NOT fixed, under the two-round cap.

**Consequence to record first, because it spans two components.** Blockers 1 and 2 compose: a test
that ran on NO family passed the whole pipeline, because the coverage check certified its module
from a sibling and the leg's verdict never looked at the status C0's gate sets. **C0's recorded
risk must not be treated as closed until both fixes are live on the default branch.** Both landed
in this round; the note stands so that a reader of C0's sign-off knows which two changes it depends
on, and so that a future edit to either file is understood as re-opening C0's risk.

- **C13 / `bench/coverage/junit.py::JobReport.declared_tests`, `matrix.py::format_verdict`** — the
  `tests` attribute pytest writes on the suite is parsed, carried through `merge_reports`, stored in
  `per_job_totals["declared_by_pytest"]` and then used by nothing and printed by nothing. It is the
  one cross-check that would catch a UNIFORM collapse: `MINIMUM_JOB_SHARE` is relative to the
  LARGEST job, so a matrix in which every leg reports 3 `<testcase>` elements beside its own
  `tests="6756"` reads COVERED on both legs with no floor tripped. Compare `declared_tests` with the
  entry count per job, make a mismatch UNMEASURED, and print the number that already exists.
- **C13 / `bench/coverage/matrix.py::evaluate`, the new fileless-entry reason** — it is appended
  once per offending entry PER JOB, so a matrix run without `junit_family=xunit1` on a tree with
  many optional-dependency modules prints the same sentence once per module per leg. The verdict
  is right and the message is actionable; only the volume is wrong. Dedupe to one line per module
  in W6. Recorded rather than fixed because it is cosmetic and the round is capped.
- **C13 / `bench/coverage/__main__.py` "What this check CANNOT catch"** — deselection through
  `pytest_collection_modifyitems` is invisible to BOTH gates: the node is never reported, so it is
  not in the coverage population, and C0's `_reconcile` skips deselected ids by construction. Either
  widen the stated limitation or count deselected ids into the report so they can be intersected.
- **C13 / `bench/coverage/__main__.py` "What this check CANNOT catch"** — a conftest
  `pytest_runtest_protocol` that fabricates a passed report defeats this check outright: junit is
  pytest's record of what it was TOLD, not of what ran. This is the same class as A69's backstop and
  it belongs in the stated limits, spelled out, rather than being discovered by the next critic.
- **C13 / `bench/coverage/matrix.py::evaluate` `per_job_totals`** — a collection ERROR is counted
  under `executed` in the per-job counts, because it carries no `<skipped>` child. The verdict
  itself is right (errors are excluded from the population and reported separately in
  `collection_errors`), but the printed `executed=` number for that leg is inflated, so the report
  contradicts its own offender list. Count them in their own column.
- **C13 / `pyproject.toml` `[bench]` vs `bench/calibration.json`** — the manifest pins
  `ladybug==0.16.0`; the recorded calibration was measured against `0.16.1`
  (`calibration.json:/environment/ladybug`). Measured impact 2.184 ms vs 2.304 ms, about 5 %, inside
  this machine's noise — so it is a REPRODUCIBILITY defect, not a wrong result. The test that should
  see the drift, `test_the_reference_engine_is_the_one_the_extra_pins`, asserts only
  `startswith("0.16")` and therefore cannot. Re-measure under the pinned version and tighten the
  assertion to exact equality with the pin.
- **C13 / the durable-commit number is a reading of a RISING CURVE, and now says so.** Disclosed,
  not re-baselined, and nothing in `bench/calibration.json` was re-measured or changed except the
  addition of one note. Each sample is one commit against a database that is NOT reset between
  samples, so kept sample k pays for `warmup + k` commits already in the log: the recorded run
  spans 5 to 34 prior commits, its first kept sample is 161 ms and its last 364 ms, and the mean
  of the second half is 1.34x the mean of the first. `measure_durable_commit`'s `detail` and a new
  `notes` entry now state the span, and `calibrate()` emits the same disclosure for every future
  run. An independent reproduction reached 123.63x against this run's 109.40x with the same
  verdict; a commissioned profile attributes the cost to a missing WAL LSN index (commit cost
  quadratic in commits already made) plus a pure-Python CRC-32C at 1.3 MiB/s, with fsync at 0.2%
  and group commit measured at ~1.002x and rejected. **The D5 ceiling stands and must not be
  amended**; the fixes belong to C4 and C1. What remains for W6 is on this component: a number to
  be compared ACROSS builds must hold the prior-commit count fixed, and this harness does not — it
  measures the operation as a caller meets it and states the span instead. Fixing that means
  either resetting the database per sample (a different operation) or sweeping the count and
  reporting the curve; both change what the ceiling means and neither may be done in a rework
  round.
- **C13 / battery tooling — a generically-named driver in the bare scratchpad is shared mutable
  state between agents, and this round proved it twice.** (a) A `determinism.sh` written to the
  scratchpad ROOT was replaced by another component's script of the same name before it could be
  run; it was caught only because it was read before launching. (b) Worse: **this component's
  `battery.py` overwrote C7's driver at the same path.** No wrong number resulted, and only
  because the two were argument-incompatible — mine takes its fork from `argv[1]` and C7's did
  not, so the collision failed loudly. Had they been compatible, one component's mutants would
  have been scored against the other's suite and a kill rate reported for it, which is a number
  indistinguishable from a real measurement. **C7 must restore its driver; the file at that path
  is not theirs.** C13's driver, journal, report, forks and reproductions now live under
  `scratchpad/c13/` with component-prefixed names (`c13_battery.py`,
  `c13-battery-journal.jsonl`, `c13-fork-b2`, `.c13-battery-running`), and the driver refuses a
  second run against the same fork. L3 made fork ROOTS unique; the general rule this round adds
  is that TOOLS need the same treatment, and it belongs in LESSONS for W2+ rather than only here.
- **C13 / `bench/calibration.json:/environment/workspace`** — the recorded workspace is a path
  inside the SHARED session scratchpad, which is L3's exact hazard, for the one artefact in this
  component whose whole point is reproducibility. Record a repository-relative or clearly synthetic
  location, or drop the field. **The hazard was then observed live during this round**, which is
  why the entry is not theoretical: a `determinism.sh` written to the scratchpad ROOT was
  overwritten by another component's script of the same name before it could be run, and the
  replacement was discovered only because it was read before launching. Every C13 artefact now
  lives under `scratchpad/c13/`; the root is a shared namespace with no owner.
- **C13 / `bench/coverage/__main__.py` `--require-family`** — the flag replaces the pinned
  `REQUIRED_FAMILIES` wholesale, so `--require-family windows` lets a one-family matrix certify
  itself: precisely what the constant's docstring says pinning exists to prevent. It is a legitimate
  escape hatch for a local run and it is undocumented as one. Say so in the help text and in the
  module docstring, and consider making it able to add families but never to drop below two.
- **C13 / `bench/coverage/junit.py::_outcome_of`** — xfail is undecidable from junit alone: pytest
  writes `<skipped type="pytest.xfail">` both for `xfail(run=False)`, which never executes, and for
  a strict xfail that RAN and failed as declared. The check calls both DECLARED_DEBT, which is the
  conservative reading (debt must still be listed), but it means an xfail that genuinely runs
  everywhere is reported as running nowhere. State the limit; there is no fix from junit alone.
- **C13 / `bench/harness/gate.py:112,187` and `.github/workflows/ci.yml`** — VTS-10's publish half
  is unrealised: `require_recall` defaults to `False` and the calibration job does not pass
  `--require-recall`, so a build that publishes no recall gauge at all passes the gate. Pass the
  flag in the workflow once the vector wave publishes the metric, or record why it stays optional.
- **C13 / battery survivors M11 and M12** — both behavioural, both naming a missing test, neither
  reachable as one of §13's five outcomes, so both are punch-list by default under §13.1.
  **M11**: `Measurement.ok` does not require `len(self.samples) > 0`, so a measurement with an empty
  sample tuple can report itself as good; every route that builds one today sets `unmeasured`
  instead, which is why the suite does not see it. **M12**: removing `calibrate()`'s
  unmeasured-status short-circuit turns exit 2 into exit 1 — UNMEASURED read as CEILING EXCEEDED,
  the same A75.2 conflation `read_multiples` carried and that this round closed. Each needs one test that pins the
  guard, not a change of behaviour.


## C7 — Index framework (round-2 rework, 2026-08-22)

The round-1 blocking defect (`mark_stale` writing the STALE bit into the page cache and never
flushing it, so a crash let a complete replay declare a short index fresh) was fixed and is not
listed here. P1, P2 and P3 were closed this round with the tests the critic named. P5 and P6 were
also closed, and are recorded below with what closed them so W6 spends no time re-deriving them.
P7 and P8 are recorded only, as asked.

- **C7 / `engine/index_manager.py` `IndexStore.commit` (P5) — CLOSED, and the window was REACHABLE.**
  The round-1 critic could not demonstrate it: arming the conftest device's write refusal at every
  write number across four buffer budgets always landed the refusal inside `flush`, after every
  change had been applied. It is reached at a budget small enough that applying a change forces an
  eviction. **Measured at `budget_pages=2` with the refusal armed at write 1: 1 of 24 entries
  applied, `stale` False, `built_through` unmoved, and 23 lookups answering EMPTY for rows the heap
  holds** — a wrong result through a public door, in the window between the failed commit and the
  next `open()`. Leaving the claimed position alone is not enough on its own, because freshness is
  re-checked only at `open()` and a caller that catches the failure and carries on meets the short
  structure first. `commit` now marks the index stale before re-raising. The mark is durable, and
  it is lifted by exactly one thing: a retry of the SAME transaction that gets all the way through,
  which is the only evidence a short index can offer that it is whole again. Pinned by
  `test_a_commit_that_failed_part_way_refuses_instead_of_answering_short`,
  `test_abandoning_a_part_applied_commit_keeps_the_refusal` and
  `test_a_commit_that_fails_on_an_already_stale_index_still_needs_a_rebuild`.
- **C7 / `engine/index_manager.py` `clear_stale`, `advance_built_through`, `note_reconciled`
  (P6) — CLOSED, and one of the three was NOT the conservative case.** P6 framed all of these as
  costing "a needless rebuild, never a wrong answer". That holds for `clear_stale` (a repair that
  never reached the device leaves the file still saying stale) and for `advance_built_through` (the
  claimed position falls back, and a position that is behind is what marks an index stale). It does
  **not** hold for `note_reconciled`. The REMOVALS a reconciliation pass made are covered by
  `INDEX_RECONCILE` records and reach the device with the commit that carries them; the HORIZON
  that authorised them is covered by nothing, and replaying those records does not restore it —
  `apply()` erases entries and never calls `reconciled_to`. Keep the removals, lose the horizon,
  and `_verify_coverage` reads a horizon of zero and turns every correctly reclaimed row into a
  `missing_entry` finding. **Measured on a cold pool over the same device: `verify()` reports
  `missing_entry` on an index that is perfectly clean** — the cries-wolf failure `create()` already
  flushes to avoid, arriving through a different door. All three now flush, each with its own
  reason stated at the site.
- **C7 / `engine/index_manager.py` `IndexStore.apply` (P7)** — RECORDED, not fixed. `apply` does not
  flush, unlike `commit`, so a replay leaves index pages dirty in the pool and the device
  disagreeing with the cache until something else flushes the file. This is safe and deliberate:
  every change `apply` makes came from a log record, redo is idempotent on the entry, and a replay
  that flushed per record would turn recovery into one device write per record for state the log
  already guarantees. It is worth stating because the four header writers around it now DO flush,
  and the next reader should find the asymmetry explained rather than have to reconstruct it. The
  position `apply` advances is in the same category — `_advance` on the commit and redo paths is
  deliberately not flushed, while the public `advance_built_through` door is.
- **C7 / `engine/index_manager.py` `IndexStore.commit`, `reconcile` (P8) — cost inherited by C9.**
  `commit` calls `_tombstone_backlog()` on every commit of a versioned index, and that is a full
  walk of every entry in the index, so a commit costs O(entries) regardless of how many changes it
  carried — paid once per commit purely to publish a gauge. `reconcile` + `commit` is
  O(entries x bucket chain), because the pass stages one REMOVE per reclaimable entry and each
  staged REMOVE re-walks its whole bucket chain in `_find_entry` when it is applied. Both are
  acceptable for a reference implementation at M1 sizes and neither is a correctness problem, but
  C9's vector index inherits them at a scale where they will show. The backlog gauge could be
  maintained incrementally as entries are tombstoned and reclaimed; the reconcile pass could carry
  the location it already found during the walk instead of looking it up again.
- **C7 / mutation tooling — three guards fired, and each would have produced a confident wrong
  number without them.** Recorded because they are properties of any battery on this platform, not
  of this component. **(a) A76's journal-sha check caught a TEXT-MODE journal.**
  `engine/index_manager.py` is CRLF and `domain/index/records.py` is LF, so a driver that journals
  with `read_text`/`write_text` round-trips the first losslessly and rewrites all 366 line endings
  of the second -- the check refused before mutating, and without it the "restore" would have left
  a file 366 bytes different from the one it journalled. Journals must be `read_bytes`/`write_bytes`.
  **(b) A70's exactly-once check caught untranslated anchors.** Once the driver was byte-exact, every
  multi-line anchor written with `\n` stopped matching the CRLF file; A70 reported seven
  ANCHOR-MISSes rather than seven survivors, which is the difference between "the driver is wrong"
  and "seven tests are missing". Anchors must be translated to the line ending the target file uses.
  **(c) A80.1 applies to two batteries over ONE fork, not only to forking from a moving tree.** A
  backgrounded run of this driver was mid-mutation while a foreground run read the same fork, and
  the foreground run reported a 7-failure "baseline" that was simply R01 applied -- indistinguishable
  from a real defect, and it cost a determinism investigation to identify (the junit byte-size of the
  phantom run matched the R02 mutant's exactly, which is what identified it). The driver now writes a
  `.battery-running` stamp in the fork root and refuses a second run against the same root.
- **C7 / mutation tooling — L3 applies to the DRIVER path, not only to the fork path.** L3 made
  fork roots unique because the scratchpad is shared. The tools in it are shared too: a sibling
  component wrote its own `A31` driver to `scratchpad/battery.py` mid-round and silently replaced
  mine, so the next launch died on a `SyntaxError` inside a driver for a different component's
  files. Nothing was damaged -- the sibling's driver takes its fork from `sys.argv[1]` and my fork
  was never touched -- but a whole battery run was lost, and had the two drivers happened to be
  argument-compatible the failure would have been a battery scoring the WRONG COMPONENT's mutants
  against my suite and reporting a kill rate for it. **A battery driver, its journal, and its
  fork-path file belong in a per-component subdirectory of the scratchpad, not in its root**, the
  same way A95 makes the fork root unique. Recorded because every component in this build writes
  its driver to the shared scratchpad today.


## W6 — D5 durable_commit on Windows (owner: C2 + C3 + C5)

The ceiling is met on POSIX with `[accel]` and safe defaults (5.65x / 4.89x / 5.88x, 3 runs). It is
missed on Windows by ~8x. SPEC-M1 `ac_7f69d7dc` requires BOTH families green, so the gate is not
green and the ceiling is not amended. In priority order:

1. **Explain the `CreateFileW` asymmetry (C2).** A coordinator control-file publication costs 16.5 ms
   on Windows, of which `atomic_replace` is 14 ms, of which `CreateFileW` on the source is 11.4 ms.
   The txn manager's `commit.state` publication -- the same seven-step sequence through the same
   device -- costs 2.65 ms. Ruled out by measurement: the directory (a probe `CreateFileW` in
   `control/` is 1.2 ms), the device's cached `FILE_SHARE_DELETE` handles (releasing all of them
   before each commit changes nothing), fsync (0.07 ms per commit for all nine), the advisory lock
   (the reader publication holds only an in-process lock and costs the same), and the per-call
   `ctypes.Structure` (0.07 ms). Unexplained. Start here: a 5x difference between two callers of one
   function is a fact about the caller, not about Windows.
2. **One reader registration per MANAGER, not per `begin` (C5 + C3).** Measured at ~13 ms per commit
   on Windows, 0.14 ms on Linux. It is not part of section 8.5 at all -- it is `begin`/CF-2 -- so it
   costs nothing frozen to change.
3. **A lease YIELD protocol (C3 + C5).** Retention alone is measured harmful: another writer waits
   5.3 s for its first commit, and two retaining writers did not finish ten rows each inside the
   suite timeout. The safe form is a holder that releases at its next commit boundary when C3 reports
   a waiter, which needs C3 to expose "someone is waiting" cheaply (a waiter file, or a flag in the
   lease record). `retain_lease` stays internal until that exists. SPEC-M1 calls the unsafe form a
   change to D1, and it is.
4. **The redundant temp-file fsync in `_publish_once` / `_publish` (C3 + C2).** The target is fsynced
   after the rename on the same volume; halves the control fsyncs from 8 to 4.

Not on this list, and deliberately: group commit. It amortises the WAL barrier, which is 0.76 ms of
the commit, and it cannot be done under FROZEN section 8.5 without changing what `CommitReport.csn`
means for each member of a batch. Measured at ~1.002x.


## The checksum slot is process-global and `connect()` writes it (C0/C1; recorded, not a defect)

Opening a database installs the CRC-32C implementation its configuration selects, process-wide.
That is deliberate and load-bearing: every component of one database must compute the same
checksum, so it is installed once rather than injected per object.

The consequence a caller should know: two databases in one process do not get independent
checksum implementations. `connect(a, checksum="pure")` followed by `connect(b)` (whose default
`auto` finds the accelerator) leaves BOTH on the accelerator. No digest changes -- `install_crc32c`
refuses a candidate that disagrees with the reference on any input of the acceptance corpus before
installing it, so the two implementations are byte-identical by construction -- but a caller who
asked for `pure` and reads `crc32c_implementation()` afterwards sees `native`.

Surfaced when a machine first had a native provider installed: two test modules asserted the slot
held `pure` at entry, which was true only while `pure` was the single reachable answer (L28). Both
now snapshot and restore the slot rather than assuming it.

Worth deciding in W6: either honour the strictest selector across live databases in a process, or
refuse the second `connect()` whose selector disagrees with what is installed, or document the
process-wide semantics on `connect()`. The current behaviour is safe; it is the SURPRISE that is
worth closing.


## A refused append can still leak one page, rarely (C1/C5; CLOSED in round 2)

An append that has already grown the file and is then refused by optimistic validation cannot give
the page back to the file: G6 forbids shrinking a data file. The pool now hands such a page out
again on the next allocation from the same process, which removes the steady-state leak -- measured
over six runs of three processes appending to one table, findings fell from 10-24 per run to 0 in
five runs and 1 in the sixth, and the heap file for the same data went from 20-26 pages to 15.

What remains: a process that abandons an append and then EXITS without appending again leaks that
one page for the life of the database, because the reclaim list is process-local and in memory.
`verify()` reports it as `page_unwritten`, so a healthy database can occasionally report itself
unclean and `oktografx verify` can exit 1 with nothing wrong.

**CLOSED, and by neither of the two routes this entry expected.** No format change and no narrowing
of `verify()`. The pool now WRITES the page out as the free page it is, at the one moment the leak
becomes permanent: `BufferPool.settle_abandoned()`, called from `Database.close()`. A FREE page with
a valid checksum and an even sequence counter carries no table descriptor, so `_page_owners` never
claims it and it is not an orphan; `PageType.FREE` is a type the layout defines, so it is not a
`page_type` finding; and it is not all zeros, so it is not `page_unwritten`. It stops being a
finding because it stops being a question -- which is the opposite of teaching verify to look away.

A process KILLED between abandoning an append and closing still leaves the page all zeros, and that
is correct: that is the crash state, and `page_unwritten` is exactly what should be reported for it.

Measured with the instrument that is IN THE TREE, so the next reader can re-run it. All figures are
`tests/smoke/test_concurrent_writers.py` (3 writers, 16 rounds of 8 rows, ~16 s), reverting one
thing at a time:

| tree | result |
|---|---|
| whole change reverted | 5 of 5 runs fail, 6-9 `page_unwritten` each |
| `settle_abandoned()` removed from `close()` only | 1 of 6 runs fails, on `assert ['page_unwritten'] == []` |
| as shipped | 6 of 6 green (13 consecutive across two sessions) |

An earlier draft of this entry quoted a "4 writers, 3 readers, 45 s" harness and the figures 4 -> 1
-> 0. That harness was a scratch script and is not in the repository; the numbers were real but
nobody else could re-run them, which is the exact harm L31 is about. Replaced above.



## Three mutation survivors on the append fix, kept deliberately (C1/C5)

Each is a guard whose reversion the suite does not notice. Recorded under 14.1.5 rather than
covered, because in each case I could not construct a production path that reaches the reverted
behaviour, and a test that reaches it only by driving internals into a state no caller produces
proves the test, not the guard.

- **`_write_back` withdrawing the page from the reuse list.** A page on that list is not resident,
  so no flush or eviction can write it back, and `settle_abandoned` pops the list before writing.
  The withdrawal is defence against a future path that makes such a page resident again.
- **`_reusable_index` skipping a resident-and-pinned candidate.** Nothing should be able to pin a
  page that was discarded and never written. The alternative to the guard is `allocate` deleting a
  frame whose holder still has the object, and that holder's next unpin decrementing a stranger's
  pin count -- which is why it is a guard and not a proof.
- **`IndexStore._grow_buckets` passing `reuse=False`.** Without it the loop still terminates and
  still reaches its target; the cost is one abandoned page spent per round, not a wrong result. The
  parameter itself IS covered, by `test_allocating_with_reuse_disabled_never_spends_an_abandoned_
  page`; what is uncovered is this one caller's use of it.


## `_modified` is not charged against the buffer budget (C1; recorded)

`BufferPool._modified` holds the keys of pages written back since the last `forget_modified()`, and
that door is called only from a write commit. A process that only reads, checkpoints or recovers
never clears it, so the set grows until close -- bounded by the number of distinct pages written
since the last write commit, which is not unbounded but is not charged against `budget_bytes`
either. A pool configured with a one-page budget can hold an arbitrarily large key set.

Two ways to close it in W6: clear the set at the same points a checkpoint settles pages, or charge
its size against the budget the way resident frames are. Neither changes any answer; both change
what a small-budget pool costs in memory.

## Mutation survivors on the append fix after round 4 (C1/C5; recorded under 14.1.5)

A 34-mutant battery over the changed surface, one mutant at a time, each survivor then re-run
against the whole suite. Three survivors from the round-3 report were closed by tests
(`_reusable_index` removing its candidate, the mark subtraction, and the relinked page being
declared). These are what remain, each with the reason no test holds it. None has a production path
I could construct to the reverted behaviour; that is the condition 14.1.5 sets for recording rather
than covering, and if a path is ever found the entry becomes a defect.

- **`_reusable_index` skipping a resident-and-pinned candidate.** Nothing should be able to pin a
  page that was discarded and never written. The guard exists because the alternative is `allocate`
  deleting a frame whose holder still has the object.
- **`_reusable_index` scanning backward rather than forward**, and **`allocate` dropping its
  `del self._frames[key]`** — both equivalent-class: a different page index or a different LRU
  position, same bookkeeping.
- **`modified_pages(file=...)` and `settle_abandoned(file=...)`** have no production caller, so
  their filter has one reachable value and is unmeasured however green the suite is (L28).
- **`settle_abandoned` popping rather than reading the list**, and **`_write_back` withdrawing from
  the reuse list** — an A93 2x2 pair: each survives alone, both together are killed by
  `test_a_page_settled_as_free_is_not_handed_out_again_by_this_pool`. They are two spellings of one
  guarantee and either alone is enough.
- **`_reclaim` keeping the key in `_grown`.** Needs a second `_reclaim` of the same page with no
  hand-out or write-back between, which no caller produces.
- **`write_chain` and `IndexStore._grow_buckets` passing `reuse=False`.** The parameter itself is
  covered; what is not covered is these two callers' use of it. `write_chain`'s harm needs a
  caller-supplied reuse list overlapping a reclaimed index, which `chain_pages()` cannot produce
  because its pages are device-backed. `_grow_buckets` costs a wasted page, never a wrong result.
- **`_abandon_rows` discarding every heap page rather than the measured set.** Over 2165 instrumented
  calls across six test packages, the measured set never contained a non-heap page, so the two are
  the same value on every input the suite reaches.
- **`_abandon_rows`'s explicit header-page entry.** Now redundant with the measured set, which is
  two mechanisms answering one question (A67). Worth removing in W6 rather than keeping a guard whose
  only evidence is that another one covers it.

Also recorded, and it is the more useful number: `tests/storage_core/test_buffer_pool.py` and
`test_heap_store.py` kill NONE of the 34. The whole new surface is held by one file,
`tests/txn/test_chain_relink_regressions.py`.


## Mutation survivors on the primary-key index (C7/C10; recorded under 14.1.5)

A 33-mutant battery over the CF-15 surface. 13 killed, 20 survived; 6 of the survivors change no
behaviour on any reachable input and are neither kills nor survivors (L18), so the kill rate is
13 of 27 = 48%, reported and not gated. Two of the survivors were the blocking defects of CF-15
round 2 and are closed. These are what remain.

**Unreachable through `connect()` — guards on an operator door, not on a caller path.** Each is a
validation on `db.indexes.commit` or on `QueryEngine(indexes=None)`, which `assemble_database` never
produces: `_tables_written_by` returning None for absent intents; the same for an intent whose table
cannot be named; accepting a `bool` table id; treating "cannot say" as "wrote nothing"; the
`callable()` guard in `_rows_carrying_key`; and the "no framework" branch that answers "no row
carries this key". They stay because the doors are public.

**Reachable and measured correctness-neutral.**

- **Keying the index on column 0 instead of the declared primary key.** Reachable with a `PRIMARY
  KEY` on a non-first column, and measured over 400 differential observations with 0 mismatches:
  the seek moves to the wrong column and the uniqueness check falls back to a scan, so the answers
  stay right and only the acceleration is lost. **A real coverage gap sits behind it**: no test
  declares a primary key on a non-first column end to end. `test_parser.py` and `test_planner.py`
  cover the syntax; neither executes DDL against an engine.
- **Advancing an index's position UNCONDITIONALLY** in `IndexManager.commit`, rather than only when
  the transaction wrote no row of that index's table. The narrowness is what keeps defect E3's alarm
  alive, and it was verified by counterfactual — neuter the staging seam and an index whose table
  WAS written still goes stale — but nothing in the suite asserts it. Same for the mutant that never
  records a written table.
- **`advance_built_through` → `_advance`,** losing the flush. L23 shape: the un-logged advance costs
  a rebuild nobody needed, never a wrong answer, which is what the method's own docstring argues.
- **Dropping either half of the `table_id`/`positions` re-check** in `_rows_carrying_key`. The
  `table_id` half is now reachable only through the state CF-15 round 2 closed; the `positions` half
  is redundant with it plus the definition digest an index file carries.

**Coverage gaps named by the review and not yet closed.**

- No test declares a `PRIMARY KEY` on a column other than the first, end to end.
- No test drives a long-lived reader's index seek against a concurrent delete or key rewrite from
  another process. The superset property was verified by hand and holds; it is not in the suite.
- `stale` is a process-local flag over a durable header, refreshed only at `open()`. Another process
  marking an index stale mid-life is invisible to a long-lived reader, which goes on planning seeks
  on it. Reaching a WRONG ANSWER that way needs a device or budget failure in the other process, so
  it is a fault-injection regime; the design predates CF-15, but CF-15 is what put a primary-key
  index on that path. Worth a fault-injection test in W6.
- A transaction that creates a table and then rolls back leaves the table in the live in-memory
  catalog, and now also leaves an index registered and its file created. Pre-existing (it reproduces
  on the parent commit), and the sharp edge of it was CF-15 round 2's defect 1. The blunt edge
  remains: a retry of the same `CREATE` fails with `GrafxConfigurationError`.
- `IndexManager.register` flushes a header during a DDL statement, which puts a device write outside
  the transaction that may then roll back.


## Traversal after CF-17: the two levers left (C7/C10; recorded)

- **The landing scan.** A traversal whose target is FREE resolves each landing table once per
  traversal by a full scan, because edges store record IDENTITIES and nothing maps an identity to a
  heap location. That is the 29 ms in a forward hop whose index work is microseconds, and the bulk
  of the 221 ms reverse case. The structural fix is an identity index (a third automatic index per
  node table) or storing refs with a repair protocol for the update case; both are format-adjacent
  and W6-sized.
- **The planner does not reorder a pattern to start from its seekable side.**
  `MATCH (c)-[:M]->(e {id: k})` walks from `c` -- a whole-table frontier -- when seeking `e` and
  traversing INCOMING would touch a handful of rows. The traversal's fan limit caps the damage; the
  ordering decision itself is planner work with its own review burden.
- **Nothing asserts the traversal actually USES the index path.** The equality tests compare
  index-vs-scan answers and the populate test walks entries, but a mutant that silently always took
  the grouped scan would pass the suite -- correctness-neutral by construction, visible only in the
  CF-17 measurement. Same survivor class as "re-adopt only the first table's index" was.

## Schema transactionality after CF-16: residues (C10; recorded)

- **CLOSED in CF-18** — the statement/transaction journal now records the files its registrations
  created and the unwind removes exactly those, frames first. Kept for the record; it originally
  said: **Index FILES of a rolled-back DDL stay on the device.** The registration is pruned; the file an
  `IndexStore.create()` wrote at statement time is not removed (a device operation inside an
  unwind). Harmless: the next registration under the name either adopts the empty file or declines.
- **`QueryEngine._working` entries leak for callers that drive the TransactionManager directly**
  (test harnesses): the wrapper's commit/rollback is what settles them. Memory, bounded by such a
  caller's DDL transaction count; txn ids are never reused, so a stale entry can never be read.
- **Two threads of one process running DDL concurrently** share the live registry side effects;
  DDL-vs-DDL commit conflicts are refused by the page-0 intersection (both stage it), but the
  working copies are per-transaction and the registry prune on one rollback can drop the OTHER
  open transaction's freshly registered index. Pre-existing regime (concurrent DDL was racier
  before CF-16 than after); recorded, not closed.
- **`CatalogStore.save()` keeps its old shape for `bootstrap()` and recovery adoption**, per the
  original W6 record. No transactional caller remains.


## Round-6 mutation survivors on the schema/traversal surface (C10; recorded under 14.1.5)

From the delta critic's 15-mutant battery (8 killed): `_txn_stages_catalog` forced True (a
defensive guard with no reachable divergence); prune by table_name instead of id (equivalent in
every reachable state — the case-fold decline blocks the divergence upstream); fan limit 64→63
(cost-only, same class as the recorded "nothing asserts the index path is USED"); `by_index`
skipping the `ended` filter, both directions (investigated as blocking and refuted: `ended` holds
only DELETEd refs, edge deletion refuses, and an in-transaction SET keeps the old version visible
in both regimes — **the day edge deletion lands, those two lines have no witness**).

## C12 — the mutation battery is an open obligation (14.1.5) — DISCHARGED, see COMPONENTS

The exit-code battery ran against the archive of 51c469c: six of six mutants killed by tests/cli.
Scope named there. Originally recorded as:

The sign-off audit found no code defect across every documented command, exit code, and
concurrency shape — and found that the battery recorded as "20/42 killed" mid-session was never
completed or reported. Until a battery over the CLI surface is run and reported with survivors
dispositioned, C12 is not DONE under the frozen criterion. Everything else about C12 in that audit
is a sign-off in waiting.

## C8/C12 observations from the sign-off audit (recorded, not defects)

- `VectorEngine.retire_space` increments a metric outside both the enabled guard and containment;
  `_EXACT_FALLBACK`'s increment runs its lambda before the enabled check (C9's file, cost only).
- `LoggingEventSink` bounds key and value lengths but not the number of payload keys; unreachable
  through any engine payload today.
- `ledger export` / `quarantine read` without `--output` answer 3 rather than 2 (the parser does
  not model required options); scripts should know.
- ci.yml pins Python 3.13 only while the metadata claims 3.11-3.13; 3.11 was verified manually
  (C0 round 10) and is not continuously verified.
- The registered `bench` marker is carried by no test; the [bench]-absent behavior rides
  `optional_dependency`. Use it or retire it.
