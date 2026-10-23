# Structural Pulse selectors for RF02–RF12

These selectors support the formally revised, non-temporal qualification
scenarios. Each profile is scoped to one existing Spec, scenario, board and
Grafx source reference. Run it against an immutable Git commit through Pulse's
external unittest runner; register only after the revised scenario edition is
current. No selector below approves an old temporal requirement or provides a
latency ratio. All selected product cases run against real Grafx databases.

The following aliases expand to full `file.py::Class.test_method` selectors:

- `S.method` = `tests/pulse/downstream_structural_evidence.py::DownstreamStructuralEvidence.method`
- `A.method` = `tests/pulse/rf0205_authority_evidence.py::RF0205AuthorityEvidence.method`
- `Q.method` = `tests/pulse/rf0608_functional_evidence.py::RF06FunctionalEvidence.method`
- `U.method` = `tests/pulse/rf0608_functional_evidence.py::RF08FunctionalEvidence.method`
- `F.method` = `tests/pulse/rf0912_functional_evidence.py::RF0912FunctionalEvidence.method`
- `X.method` = `tests/pulse/rf1112_supplemental_evidence.py::RF1112SupplementalEvidence.method`
- `N.method` = `tests/query/test_unwind_scalar_evidence.py::UnwindScalarEvidence.method`
- `C.method` = `tests/query/test_rf08_crash_evidence.py::RF08CrashEvidence.method`

| Spec / scenario | Pulse profile selectors | Product proof |
| --- | --- | --- |
| RF02 `ts_95175411` | `S.test_rf02_budgeted_scalar_seek`, `N.test_1024_nodes_sixteen_occurrences`, `A.test_rf02_authority` | Exact 1,024 node/16 key oracle, multiplicity, physical probes, quota 4 KiB applied per operator, global logical peak reported, zero retained baseline and fresh statement; old/new snapshots and authority fallback. |
| RF03 `ts_2e25b7aa` | `S.test_rf03_budgeted_connected_anchor`, `S.test_rf03_anchor_work_and_oracle`, `A.test_rf03_authority` | Exact connected oracle and multiplicity, physical anchor/edge work against a scan, quota 4 KiB per operator, global logical peak reported, fresh statement after close, stale/missing authority and retained snapshot. |
| RF04 `ts_0acc8871`, `ts_263e138f` | Use the separate RF04 structural profile from `bench/harness/rf04_structural_evidence.py` after its integration; do not use RF03's connected-only proof as the RF04 four-arm interaction. | Four arms, 32 real cases, uniform/hub and authority/fallback. |
| RF05 `ts_abbccf2d` | `S.test_rf05_ordered_work_and_memory`, `A.test_rf05_authority` | Six real ordered-page/control cases, five additional 1,024-node pages under positive quota, NULL order control, exact ordered oracle, 1,024-node target, physical and ordered counters, 4 KiB target quota and fallback authority. |
| RF06 `ts_62140a44` | `S.test_rf06_split_memory_and_quota`, `Q.test_quota_main`, `Q.test_quota_boundaries`, `Q.test_quota_compatibility`, `Q.test_quota_authority`, `Q.test_quota_consumption` | Same-query batch versus scalar certificate attempts in all three traversal quotas, exact two-group/128-candidate counters, refusal order and multiplicity, plus five budgeted scalar reads per quota with covered logical peak, zero retained bytes and zero open transactions. |
| RF08 `ts_275cf910` | `S.test_rf08_certificate_ack_and_reopen`, `U.test_uniqueness_main`, `U.test_uniqueness_boundaries`, `U.test_uniqueness_concurrency`, `U.test_uniqueness_authority`, `C.test_before_commit_has_no_batch_or_ack`, `C.test_after_barrier_recovers_entire_batch_without_ack`, `C.test_rollback_and_lazy_consumption_preserve_prior_work` | Two certified groups for 128 CREATE, fewer physical certificate attempts than scalar, both 128-CREATE arms under the same 1 MiB quota with measured logical peak/zero retention, ACK, read-only reopen, no visible prefix on failure, cross-process uniqueness, lazy generator consumption and pre/post barrier crash semantics. |
| RF09 `ts_159337d5` | `S.test_rf09_sequence_memory`, `F.test_rf09_main`, `F.test_rf09_boundaries`, `F.test_rf09_compatibility`, `F.test_rf09_integration` | Sequence ownership, four shape/transaction semantics, exact ACK counts, failure and retry rules, durable prefix, reopen, per-writer memory. |
| RF10 `ts_781be228` | `S.test_rf10_sequence_memory`, `F.test_rf10_main`, `F.test_rf10_boundaries`, `F.test_rf10_compatibility`, `F.test_rf10_integration` | Sequence memory observer, snapshot and reader ownership, first-row/close behavior, four real read recipes and sixteen-key transaction count. |
| RF11 `ts_80e6a214` | `S.test_rf11_write_ack_and_reopen`, `F.test_rf11_main`, `F.test_rf11_boundaries`, `F.test_rf11_compatibility`, `F.test_rf11_integration`, `X.test_rf11_default_and_optin_exact_ranking`, `X.test_rf11_setup_observation_count`, `X.test_rf11_setup_observation_refusals` | Eight real WAL/durable write cases with exact ranking, ACK, reopen and verification; cache/source, retained reader, setup provenance and refusal guards. Does not represent the old 25-case timed partition coverage. |
| RF12 `ts_c4150cdf` | `F.test_rf12_main`, `F.test_rf12_boundaries`, `F.test_rf12_compatibility`, `F.test_rf12_integration`, `X.test_rf12_foreign_checkpoint_with_retained_reader`, `X.test_rf12_bounded_read_only_hash_skew_diagnosis` | Snapshot/reader lifecycle, quota and memory on capacity recipes, fixed-total ACK/reopen, foreign checkpoint at 128/512/2048, bounded read-only diagnosis. Does not represent the old 15-name/18-participation timed partitions. |

The RF02 and RF03 logical observer reports a **global simultaneous tariff peak**
that can exceed the configured 4 KiB **per-operator** query quota. The real
target controls observed 29,356 and 32,920 bytes, respectively, on this
revision. These numbers are memory accounting, not latency measurements. The
selectors check that the quota is configured, the result is exact, the global
peak is present and the next statement starts with zero retained bytes. They do
not assert the false inequality `global_peak <= per_operator_quota`.

For each scenario, register one local Pulse profile with its expanded selectors
(`--test` repeated), correct `--source-ref`, board/spec/scenario IDs, and a
bounded timeout. `--allow-long-running` is needed only when the chosen timeout
exceeds 120 seconds. The source revision in the MCP replay must contain these
selectors; they cannot be run against a pre-integration revision. An authenticated
Pulse run still needs the normal `update_test_scenario_status` step and Delivery
association. The external runner receipt authenticates the selected unittest
count and source revision, while each wrapper validates exact native pytest
case IDs, rejects skips/failures and checks the product behavior itself.
