"""The F1 instrument must fail loudly on a broken run, and never quietly report a good one.

Most of these drive ``evaluate`` directly. That is the point: the observations that matter most
-- a dead child, a torn read, a durable index refusal -- appear only intermittently in a short
real run, so a criterion exercised solely by running the tool is a criterion nobody has ever
seen fail. Feeding synthetic observations is how each judgement gets proven.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tools.measure_multiclient_matrix import (
    FROZEN_FOREIGN_COMMIT_RATES,
    FROZEN_READER_TARGETS,
    FROZEN_WRITER_COUNTS,
    QUIET_TABLE,
    UNAVAILABLE_METRICS,
    build_parser,
    evaluate,
    gather_metrics,
    main,
    percentile,
    plan_tables,
    profile,
    run_child,
    select_cells,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = PROJECT_ROOT / "src"

CLEAN_WALK = {"clean": True, "findings": 0, "pages_checked": 40, "records_checked": 12,
              "index_entries_checked": 12}
LEDGER = {"stored": ["Item1:1", "Item1:2", "Item1:3"],
          "owner": {"Item1:1": [5, "n"], "Item1:2": [5, "n"], "Item1:3": [9, "n"]},
          "superseded": ["Item1:1"], "edges": [["Links1", 1, 2, 1]]}
LIVE_MARKS = {"held_open_through_run": True, "opened_before_writers": True,
              "saw_done_flag": True, "pid": 4242, "waited_seconds": 1.0}
HEALTHY_METRICS = {
    "captured": {
        "per_process_snapshots": 2,
        "by_process": [
            {"role": "writer", "slot": 1, "metrics": {"oktografx_x": 1},
             "document": {"publications": 2, "final": {"metrics": {"oktografx_x": 1}}}},
            {"role": "reader", "slot": 1, "metrics": {"oktografx_x": 1},
             "document": {"publications": 2, "final": {"metrics": {"oktografx_x": 1}}}},
        ],
    },
    "capture_errors": None,
}


def observations(**overrides: object) -> dict:
    """A cell that passed, so each test can spoil exactly one thing and watch it fail."""
    baseline = {
        "readers": 1, "rate": 1.0, "reopen_workaround": False,
        "child_failures": [], "missing": [],
        "live": {**CLEAN_WALK, **LEDGER, **LIVE_MARKS},
        "cold": {**CLEAN_WALK, **LEDGER},
        "acknowledged": ["Item1:1", "Item1:2", "Item1:3"],
        "stored_list": ["Item1:1", "Item1:2", "Item1:3"],
        "torn": [], "escapes": [], "durable": [], "reopens": 0,
        "statements": 40,
        "expected_owner": {"Item1:1": [5, "n"], "Item1:2": [5, "n"], "Item1:3": [9, "n"]},
        "expected_superseded": ["Item1:1"],
        "expected_edges": [["Links1", 1, 2, 1]],
        "readers_stopped_early": [],
        "writers": 1,
        "participants_never_ready": [],
        "metrics": HEALTHY_METRICS,
        "family_gaps": [],
        "commit_span": (1000.0, 1039.0),
        "reader_window": 40.0,
        "writer_span": 39.0,
        "commits": 40,
    }
    baseline.update(overrides)
    return baseline


def help_text() -> str:
    """The parser help with whitespace collapsed.

    argparse re-wraps to the terminal width, which is not the same under pytest as in a
    shell, so a phrase can straddle a line break and a plain substring test fails for a
    reason that has nothing to do with the text being right.
    """
    return re.sub(r"\s+", " ", build_parser().format_help())


def failed(criteria: list[dict]) -> list[str]:
    return [item["name"] for item in criteria if not item["pass"]]


def test_a_healthy_cell_passes_every_criterion() -> None:
    """The control. Without this, a test that expects failure proves nothing."""
    assert failed(evaluate(**observations())) == []


# --- the honesty rules -------------------------------------------------------------------


def test_an_empty_sample_is_reported_as_absent_and_never_as_zero() -> None:
    empty = profile([])

    assert empty == {"n": 0, "p50": None, "p90": None, "p99": None, "max": None}
    # The distinction that matters: a real zero-latency sample would be 0.0, not None.
    assert profile([0.0])["p50"] == 0.0
    assert percentile([], 0.5) is None


def test_percentiles_are_nearest_rank_over_the_real_samples() -> None:
    samples = [float(value) for value in range(1, 101)]

    assert percentile(samples, 0.50) == 50.0
    assert percentile(samples, 0.90) == 90.0
    assert percentile(samples, 0.99) == 99.0
    assert profile(samples)["max"] == 100.0
    # A single sample is its own every percentile, and must not index out of range.
    assert percentile([4.2], 0.99) == 4.2


def test_a_case_where_nothing_happened_fails_instead_of_passing_vacuously() -> None:
    """Every 'nothing bad happened' criterion is satisfied by a run that did nothing."""
    idle_writers = evaluate(**observations(commits=0))
    assert "writers_did_work" in failed(idle_writers)

    idle_readers = evaluate(**observations(statements=0))
    assert "readers_did_work" in failed(idle_readers)


def test_a_curve_point_whose_writer_went_quiet_early_is_refused() -> None:
    """At 10/s a writer that exhausts its rounds halfway leaves the reader in silence.

    The point would still be labelled 10/s while the reader felt about half of it, which is
    the one failure that makes the whole curve wrong rather than merely noisy.
    """
    name = "foreign_traffic_covered_the_reader_window"

    # The writer committed at its labelled rate for ten seconds and then stopped, while the
    # reader kept measuring for twenty.
    truncated = evaluate(**observations(rate=10.0, commits=100, writer_span=10.0,
                                        reader_window=20.0,
                                        commit_span=(1000.0, 1010.0)))
    names = failed(truncated)
    assert name in names
    observed = next(item["observed"] for item in truncated if item["name"] == name)
    # 10s of commits against a reachable window of 20 - 0.1, so a hair over half.
    assert 0.5 <= observed["coverage"] < 0.51
    assert observed["labelled_rate"] == 10.0
    # The number a reader of the report should trust is the one over the reader's window.
    assert observed["effective_rate_over_reader_window"] == 5.0
    # And the label itself is refused, independently of the coverage figure.
    assert "the_labelled_rate_was_delivered" in names

    covered = evaluate(**observations(rate=10.0, commits=200, writer_span=19.8,
                                      reader_window=20.0,
                                      commit_span=(1000.0, 1019.8)))
    assert failed(covered) == []

    # With no foreign traffic commanded there is no coverage question to ask.
    idle = evaluate(**observations(rate=0.0, commits=0, writer_span=0.0,
                                   reader_window=20.0, commit_span=(None, None)))
    assert name not in {item["name"] for item in idle}


def test_a_commanded_idle_writer_must_actually_be_idle() -> None:
    """Rate 0 is the baseline of the frozen curve: a commit there means it did not hold."""
    assert failed(evaluate(**observations(rate=0.0, commits=0))) == []
    assert "commanded_idle_held" in failed(evaluate(**observations(rate=0.0, commits=5)))


def test_a_verify_walk_that_checked_nothing_is_refused_although_it_found_nothing() -> None:
    """A75.2: an empty findings tuple is not evidence; coverage is."""
    vacuous = {"clean": False, "findings": 0, "pages_checked": 0, "records_checked": 0,
               "index_entries_checked": 0}

    assert "verify_clean_live" in failed(evaluate(**observations(live=dict(
        vacuous, stored=[1, 2, 3]))))
    assert "verify_clean_cold" in failed(evaluate(**observations(cold=vacuous)))


def test_a_dead_or_missing_child_fails_the_case() -> None:
    dead = evaluate(**observations(child_failures=[{"child": "writer-1", "code": 1}]))
    assert "every_child_completed" in failed(dead)

    absent = evaluate(**observations(missing=["reader-1"]))
    assert "every_child_completed" in failed(absent)


def test_a_failed_oracle_never_certifies_the_ledger() -> None:
    """With no oracle there is no stored set, so silence must not read as 'nothing lost'."""
    criteria = evaluate(**observations(live={"child_failed": True}))
    names = failed(criteria)

    assert "oracle_ran" in names
    assert "no_acknowledged_row_lost" in names
    assert "verify_clean_live" in names


def test_lost_phantom_duplicate_and_torn_rows_each_fail_by_name() -> None:
    lost = evaluate(**observations(acknowledged=[1, 2, 3, 4], stored_list=[1, 2, 3]))
    assert "no_acknowledged_row_lost" in failed(lost)

    phantom = evaluate(**observations(acknowledged=[1, 2], stored_list=[1, 2, 3]))
    assert "no_phantom_row" in failed(phantom)

    duplicated = evaluate(**observations(stored_list=[1, 2, 3, 3]))
    assert "no_duplicate_row" in failed(duplicated)

    torn = evaluate(**observations(torn=[{"kind": "long_snapshot_moved"}]))
    assert "no_torn_read" in failed(torn)

    escaped = evaluate(**observations(escapes=["ZeroDivisionError: boom"]))
    assert "no_non_grafx_escape" in failed(escaped)


def test_the_ledger_notices_a_row_that_changed_table_or_lost_a_property() -> None:
    """Flattening ids across tables let a row move from Item1 to Item2 and read as unchanged.

    The same hole hid a lost body and an edge whose weight changed, because neither the
    property nor the edge table was part of what was compared.
    """
    moved = {**LEDGER, "stored": ["Item2:1", "Item1:2", "Item1:3"],
             "owner": {"Item2:1": [5, "n"], "Item1:2": [5, "n"], "Item1:3": [9, "n"]}}
    criteria = evaluate(**observations(live=dict(CLEAN_WALK, **moved, **LIVE_MARKS),
                                       cold=dict(CLEAN_WALK, **moved),
                                       stored_list=moved["stored"]))
    names = failed(criteria)
    assert "no_acknowledged_row_lost" in names
    assert "no_phantom_row" in names
    # And the owner ledger notices it too, independently of the id sets.
    assert "update_node_effect_stored" in names

    lost_body = {**LEDGER, "owner": {**LEDGER["owner"], "Item1:2": [5, None]}}
    assert "update_node_effect_stored" in failed(evaluate(**observations(
        live=dict(CLEAN_WALK, **lost_body, **LIVE_MARKS))))

    other_edge_table = {**LEDGER, "edges": [["LinksOther", 1, 2, 1]]}
    assert "create_edge_effect_stored" in failed(evaluate(**observations(
        live=dict(CLEAN_WALK, **other_edge_table, **LIVE_MARKS))))

    reweighted = {**LEDGER, "edges": [["Links1", 1, 2, 99]]}
    assert "create_edge_effect_stored" in failed(evaluate(**observations(
        live=dict(CLEAN_WALK, **reweighted, **LIVE_MARKS))))


def test_a_label_the_run_did_not_deliver_is_refused() -> None:
    """200 commits over 20s is 10/s only if 20s is the window; over 10s it is 20/s.

    The slack is one pacing slot per writer, which is the quantisation a paced writer can
    actually incur -- not a percentage chosen to make the number fit.
    """
    name = "the_labelled_rate_was_delivered"

    exact = evaluate(**observations(rate=1.0, commits=40, reader_window=40.0))
    assert name not in failed(exact)

    doubled = evaluate(**observations(rate=1.0, commits=80, reader_window=40.0))
    assert name in failed(doubled)
    observed = next(item["observed"] for item in doubled if item["name"] == name)
    assert observed["expected_commits"] == 40.0
    assert observed["effective_rate"] == 2.0

    # One slot per writer is tolerated; two writers may each be one slot out.
    assert name not in failed(evaluate(**observations(
        rate=1.0, commits=42, reader_window=40.0, writers=2)))
    assert name in failed(evaluate(**observations(
        rate=1.0, commits=44, reader_window=40.0, writers=2)))


def test_coverage_is_measured_from_the_real_first_and_last_commit() -> None:
    """Staggered writers start at different instants; a nominal span hides the gap."""
    name = "foreign_traffic_covered_the_reader_window"

    late = evaluate(**observations(rate=1.0, commits=40, reader_window=40.0,
                                   commit_span=(1000.0, 1015.0)))
    assert name in failed(late)
    observed = next(item["observed"] for item in late if item["name"] == name)
    assert observed["measured_from"] == "first and last aggregate commit"
    # 15s of commits against a reachable window of 40 - 1.
    assert observed["reachable_span_seconds"] == 39.0
    assert 0.38 <= observed["coverage"] < 0.39

    # A healthy paced run cannot span the whole window -- at 1/s over 40s the commits land at
    # 0..39 -- and must not be failed for the edge the pacing itself creates.
    edge_effect = evaluate(**observations(rate=1.0, commits=40, reader_window=40.0,
                                          commit_span=(1000.0, 1039.0)))
    assert name not in failed(edge_effect)


def test_a_participant_that_never_reached_the_barrier_fails_the_cell() -> None:
    """It was recorded in the JSON and judged nowhere, so such a cell still passed."""
    name = "every_participant_reached_the_barrier"

    assert name not in failed(evaluate(**observations()))
    assert name in failed(evaluate(**observations(
        participants_never_ready=["ready-reader-1.json"])))


def test_metrics_that_came_back_empty_fail_the_cell() -> None:
    """gather_metrics was folded into the report after the judgement, so an empty capture
    could not fail anything -- which is how a run with no counters at all read as complete."""
    name = "every_process_published_real_metrics"

    assert name not in failed(evaluate(**observations()))

    nothing = {"captured": {"per_process_snapshots": 0, "by_process": []},
               "capture_errors": None}
    assert name in failed(evaluate(**observations(metrics=nothing)))

    errored = {**HEALTHY_METRICS, "capture_errors": [{"role": "writer", "error": "boom"}]}
    assert name in failed(evaluate(**observations(metrics=errored)))

    hollow = {"captured": {"per_process_snapshots": 2, "by_process": [
        {"role": "writer", "slot": 1, "metrics": {"x": 1},
         "document": {"publications": 1, "final": {}}},
        {"role": "reader", "slot": 1, "metrics": {"x": 1},
         "document": {"publications": 1, "final": {}}},
    ]}, "capture_errors": None}
    assert name in failed(evaluate(**observations(metrics=hollow)))

    # EXACTLY one snapshot per process: a duplicate would satisfy a >= check while some
    # process reported nothing at all.
    duplicated = {**HEALTHY_METRICS, "captured": {
        **HEALTHY_METRICS["captured"],
        "by_process": HEALTHY_METRICS["captured"]["by_process"]
        + [HEALTHY_METRICS["captured"]["by_process"][0]],
    }}
    assert name in failed(evaluate(**observations(metrics=duplicated)))

    # MetricsSnapshotView is a Mapping with no as_dict; stringifying it wrote a truthy repr
    # that passed every "did we capture metrics" check while holding no counter.
    stringified = {**HEALTHY_METRICS, "captured": {
        **HEALTHY_METRICS["captured"],
        "by_process": [{**entry, "metrics": "MetricsSnapshotView(entries=(...))"}
                       for entry in HEALTHY_METRICS["captured"]["by_process"]],
    }}
    assert name in failed(evaluate(**observations(metrics=stringified)))


def test_the_children_convert_the_snapshot_mapping_rather_than_stringifying_it() -> None:
    """A repr is truthy and contains no counter, so it must never reach the report."""
    from tools.measure_multiclient_matrix import READER_CHILD, WRITER_CHILD

    for child in (WRITER_CHILD, READER_CHILD):
        assert "collections.abc.Mapping" in child
        assert "METRICS-NOT-A-MAPPING" in child
        # The old fallback is gone.
        assert "default=lambda item: getattr(item" not in child


def test_a_family_with_no_samples_cannot_certify_itself() -> None:
    """An empty profile is not a measurement of the family it is named after."""
    name = "every_writer_sampled_every_family"

    assert name not in failed(evaluate(**observations()))
    assert name in failed(evaluate(**observations(
        family_gaps=[{"writer": 1, "family": "create_edge"}])))
    # With no foreign traffic commanded there is no family coverage question to ask.
    assert name not in {item["name"] for item in evaluate(**observations(
        rate=0.0, commits=0, family_gaps=[{"writer": 1, "family": "create_edge"}]))}


def test_the_serial_replay_must_verify_clean_and_start_from_the_same_state() -> None:
    """A replay from an empty database is not the same run, and one that never verified
    itself is not fit to be compared against."""
    baseline = {"identical": True, "source": {"digest": "aa"}, "copy": {"digest": "aa"}}
    healthy = {**LEDGER, "replayed": 3, "clean": True, "initial_state": baseline,
               "pages_checked": 40, "records_checked": 12, "index_entries_checked": 12}

    assert failed(evaluate(**observations(serial=healthy, serial_operations=3))) == []

    unclean = {**healthy, "clean": False}
    assert "verify_clean_serial" in failed(evaluate(**observations(
        serial=unclean, serial_operations=3)))

    # clean with no coverage is the A75.2 trap again: a walk that examined nothing.
    uncovered = {**healthy, "pages_checked": 0, "records_checked": 0,
                 "index_entries_checked": 0}
    assert "verify_clean_serial" in failed(evaluate(**observations(
        serial=uncovered, serial_operations=3)))

    different_start = {**healthy, "initial_state": {"identical": False,
                                                    "source": {"digest": "aa"},
                                                    "copy": {"digest": "bb"}}}
    assert "serial_started_from_the_same_state" in failed(evaluate(**observations(
        serial=different_start, serial_operations=3)))


def test_a_reader_that_stopped_before_its_window_closed_fails() -> None:
    """A long reader refused on its second scan used to end early and pass anyway."""
    name = "every_reader_ran_its_whole_window"

    assert name not in failed(evaluate(**observations()))
    for why in ("refused", "torn", "escaped"):
        stopped = evaluate(**observations(readers_stopped_early=[{"slot": 1, "why": why}]))
        assert name in failed(stopped), why


def test_a_non_retryable_refusal_is_an_escape_not_a_legal_refusal() -> None:
    """Treating every GrafxError as legal was a false pass: only a retryable one is legal."""
    from tools.measure_multiclient_matrix import READER_CHILD

    # The classifier is the code that decides, so its rule is asserted where it lives.
    assert "if not refused.retryable:" in READER_CHILD
    assert '"kind": "non_retryable_refusal"' in READER_CHILD
    # And a non-retryable refusal lands in escapes, which already fails the case.
    assert "escapes.append({\"kind\": \"non_retryable_refusal\"" in READER_CHILD


def test_a_durable_index_refusal_fails_unless_the_workaround_was_asked_for() -> None:
    """INDEX_FLAG_STALE is on disk. Retrying it is a spin, so it must not read as a conflict."""
    refusal = {"family": "update_node", "attempt": 0, "error": {
        "type": "GrafxIndexError", "code": "index_error",
        "details": {"field": "index_view_unavailable", "index": "pk_Item1"}}}

    refused = evaluate(**observations(durable=[refusal]))
    assert "no_durable_index_refusal" in failed(refused)

    # Opt-in only, and the report still carries the count so nobody mistakes a worked-around
    # run for a clean one.
    worked_around = evaluate(**observations(durable=[refusal], reopens=1,
                                            reopen_workaround=True))
    assert failed(worked_around) == []
    observed = next(item["observed"] for item in worked_around
                    if item["name"] == "no_durable_index_refusal")
    assert observed["count"] == 1
    assert observed["reopens"] == 1
    assert observed["workaround_enabled"] is True


def test_every_process_keeps_its_own_metrics_snapshot_labelled_by_role_and_slot() -> None:
    """F1 asks for per-process counters; one writer standing in for the fleet is a different
    question with a more flattering answer."""
    folded = gather_metrics([
        {"role": "writer", "slot": 1, "metrics": {"commits": 10}, "metrics_error": None},
        {"role": "writer", "slot": 2, "metrics": {"commits": 3}, "metrics_error": None},
        {"role": "reader", "slot": 1, "metrics": {"reads": 99}, "metrics_error": None},
    ])

    assert folded["captured"]["per_process_snapshots"] == 3
    by_process = folded["captured"]["by_process"]
    assert [(entry["role"], entry["slot"]) for entry in by_process] == [
        ("writer", 1), ("writer", 2), ("reader", 1)]
    # The second writer's numbers survive; they are not collapsed into the first one's.
    assert by_process[1]["metrics"] == {"commits": 3}


def test_a_metric_the_product_does_not_emit_is_named_with_its_reason_never_zeroed() -> None:
    folded = gather_metrics([
        {"role": "writer", "slot": 1, "metrics": {"entries": []}, "metrics_error": None},
        {"role": "reader", "slot": 1, "metrics": None, "metrics_error": "boom"},
    ])

    assert folded["captured"]["per_process_snapshots"] == 1
    assert folded["capture_errors"] == [{"role": "reader", "slot": 1, "error": "boom"}]
    for name in ("commit_hold_seconds", "writer_lease_publish_seconds",
                 "index_view_hold_seconds"):
        assert name in folded["unavailable"]
        # A reason, not a number and not an empty string.
        assert isinstance(folded["unavailable"][name], str)
        assert len(folded["unavailable"][name]) > 20
    assert not any(isinstance(value, (int, float))
                   for value in UNAVAILABLE_METRICS.values())


def test_a_child_that_dies_or_prints_garbage_becomes_a_failure_not_an_empty_result() -> None:
    died = run_child("import sys; sys.exit(3)", [])
    assert died["child_failed"] is True
    assert died["code"] == 3

    garbled = run_child("print('not json at all')", [])
    assert garbled["child_failed"] is True
    assert "unparseable" in garbled["stderr"]

    silent = run_child("pass", [])
    assert silent["child_failed"] is True


def test_the_oracle_certifies_the_effect_of_all_four_writer_families() -> None:
    """A node id proves create_node only. The other three families need their own evidence."""
    stale_owner = evaluate(**observations(
        live={**CLEAN_WALK, **LEDGER, **LIVE_MARKS, "owner": {"1": 5, "2": 5, "3": 0}}))
    assert "update_node_effect_stored" in failed(stale_owner)

    not_superseded = evaluate(**observations(
        live={**CLEAN_WALK, **LEDGER, **LIVE_MARKS, "superseded": []}))
    assert "mark_superseded_effect_stored" in failed(not_superseded)

    # A row flagged superseded that no committed transaction named is just as wrong.
    invented = evaluate(**observations(
        live={**CLEAN_WALK, **LEDGER, **LIVE_MARKS, "superseded": [1, 2]}))
    assert "mark_superseded_effect_stored" in failed(invented)

    missing_edge = evaluate(**observations(
        live={**CLEAN_WALK, **LEDGER, **LIVE_MARKS, "edges": []}))
    assert "create_edge_effect_stored" in failed(missing_edge)

    duplicated_edge = evaluate(**observations(
        live={**CLEAN_WALK, **LEDGER, **LIVE_MARKS, "edges": [[1, 2], [1, 2]]}))
    assert "create_edge_effect_stored" in failed(duplicated_edge)


def test_the_live_pass_must_have_held_a_handle_open_through_the_run() -> None:
    """Two fresh opens after everyone closed are one measurement wearing two names."""
    name = "live_pass_used_a_handle_held_through_the_run"

    for spoiled in ("held_open_through_run", "opened_before_writers", "saw_done_flag"):
        marks = dict(LIVE_MARKS)
        marks[spoiled] = False
        assert name in failed(evaluate(**observations(
            live={**CLEAN_WALK, **LEDGER, **marks}))), spoiled

    # A plain reopen -- exactly what the old implementation did -- carries none of the marks.
    assert name in failed(evaluate(**observations(live={**CLEAN_WALK, **LEDGER})))

    # The decisive case: a handle opened DURING the run satisfies the loose "before the end"
    # test and must still fail, because that is the test any post-hoc reopen also passes.
    late = {**LIVE_MARKS, "opened_before_writers": False,
            "opened_before_end_of_run": True}
    assert name in failed(evaluate(**observations(live={**CLEAN_WALK, **LEDGER, **late})))


def test_a_live_and_cold_disagreement_is_caught() -> None:
    disagree = evaluate(**observations(
        cold={**CLEAN_WALK, **LEDGER, "stored": [1, 2]}))
    assert "live_and_cold_ledgers_agree" in failed(disagree)

    edges_differ = evaluate(**observations(
        cold={**CLEAN_WALK, **LEDGER, "edges": []}))
    assert "live_and_cold_ledgers_agree" in failed(edges_differ)


# --- the frozen dimensions ---------------------------------------------------------------


def test_the_reader_target_decides_whether_the_reader_shares_the_writer_table() -> None:
    """The CE-3 discriminant: the unrelated reader must not read what the writer writes."""
    written, _edges, (read_same, _) = plan_tables(2, "disjoint", "same-table")
    assert read_same == written == ["Item1", "Item2"]

    written, _edges, (read_apart, apart_edges) = plan_tables(2, "disjoint",
                                                            "unrelated-table")
    assert read_apart == [QUIET_TABLE]
    assert apart_edges == [f"{QUIET_TABLE}Links"]
    assert not set(read_apart) & set(written)

    hot_written, _edges, (read_hot, _) = plan_tables(4, "hot", "same-table")
    assert hot_written == ["Item"] and read_hot == ["Item"]


def test_the_ce3_subcase_is_finite_two_process_and_sweeps_both_frozen_dimensions() -> None:
    opts = build_parser().parse_args(["--src", str(SOURCE_ROOT)])
    cells = select_cells(opts)

    assert len(cells) == len(FROZEN_READER_TARGETS) * len(FROZEN_FOREIGN_COMMIT_RATES)
    assert {(writers, readers) for writers, readers, *_ in cells} == {(1, 1)}
    assert {target for *_, target, _rate in cells} == set(FROZEN_READER_TARGETS)
    assert {rate for *_, rate in cells} == set(FROZEN_FOREIGN_COMMIT_RATES)


def test_the_matrix_case_covers_the_frozen_counts_without_multiplying_readerless_cells() -> None:
    opts = build_parser().parse_args(["--src", str(SOURCE_ROOT), "--case", "matrix",
                                      "--regime", "hot"])
    cells = select_cells(opts)

    assert {writers for writers, *_ in cells} == {1, 2, 4, 8}
    assert {readers for _writers, readers, *_ in cells} == {0, 1, 3}
    # The frozen rate curve belongs to the matrix too. Sweeping N and M at a single unpaced
    # rate and calling it the full matrix would claim an axis it never ran.
    assert {rate for *_, rate in cells} == set(FROZEN_FOREIGN_COMMIT_RATES)
    # With no readers the reader shape and target are unobservable, so they must NOT multiply
    # the run into identical cells. The rate axis still varies, because writer throughput
    # against a paced commit rate is observable with no reader present.
    readerless = [cell for cell in cells if cell[1] == 0]
    assert len({(shape, target) for *_, shape, target, _rate in readerless}) == 1
    assert len(readerless) == len(FROZEN_WRITER_COUNTS) * len(FROZEN_FOREIGN_COMMIT_RATES)
    assert {rate for *_, rate in readerless} == set(FROZEN_FOREIGN_COMMIT_RATES)


def test_the_frozen_rate_is_the_aggregate_the_reader_sees_split_across_the_writers() -> None:
    """Handing 10/s to each of 8 writers delivers 80/s and labels it 10/s.

    That would make the N=1 and N=8 rows of the matrix incomparable at the same point of the
    curve, which is the one thing the curve exists to allow.
    """
    from tools.measure_multiclient_matrix import per_writer_rate

    assert per_writer_rate(10.0, 1) == 10.0
    assert per_writer_rate(10.0, 2) == 5.0
    assert per_writer_rate(10.0, 8) == 1.25
    # The zero of the curve stays zero however many writers share it.
    assert per_writer_rate(0.0, 8) == 0.0
    # Unpaced is not a rate and must not be divided into one.
    assert per_writer_rate(None, 4) is None


def test_help_describes_the_rate_option_as_it_actually_behaves() -> None:
    """Omitting --foreign-commit-rate sweeps the frozen curve; the help must not claim
    otherwise, because a reader who believes it would mislabel every run they did."""
    text = help_text()
    rate_help = text[text.index("--foreign-commit-rate"):]

    assert "AGGREGATE" in rate_help
    assert "sweeps the frozen curve" in rate_help
    # The old text promised omission meant unpaced. select_cells never did that.
    assert "omitting the option entirely means unpaced" not in text


def test_help_separates_the_frozen_dimensions_from_the_parameterised_gaps() -> None:
    text = help_text()

    assert "FROZEN BY ROADMAP SECTION 6.5" in text
    assert "PARAMETERISED BECAUSE THE REPORT LEFT THE VOLUME OPEN" in text
    for frozen in ("create_node", "reader target", "foreign commit rate"):
        assert frozen in text
    for gap in ("--seconds", "--rows-per-txn", "--txns-per-writer", "--warmup-seconds"):
        assert gap in text


# --- the option guards -------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--seconds", "0"],
        ["--rows-per-txn", "0"],
        ["--txns-per-writer", "-1"],
        ["--warmup-seconds", "-1"],
        # A warmup that swallows the whole phase would discard every sample and report
        # nothing, which is worse than refusing to start.
        ["--warmup-seconds", "20", "--seconds", "20"],
        # The long reader has its own duration; a warmup that swallows it would leave the
        # long shape with zero samples while every other shape still reported.
        ["--warmup-seconds", "70", "--seconds", "120", "--long-reader-seconds", "60"],
        ["--foreign-commit-rate", "-1"],
        ["--writers", "0"],
        ["--readers", "-1"],
    ],
)
def test_an_impossible_option_is_refused_before_any_process_is_spawned(
    argv: list[str],
) -> None:
    with pytest.raises(SystemExit) as refused:
        main(["--src", str(SOURCE_ROOT), *argv])

    assert refused.value.code == 2


def test_provenance_describes_the_source_checkout_separately_from_the_script(
    tmp_path: Path,
) -> None:
    """--src can point at another worktree; one commit field would credit code that never ran."""
    from tools.measure_multiclient_matrix import provenance

    here = build_parser().parse_args(["--src", str(SOURCE_ROOT)])
    same = provenance(here)
    assert same["source_repository"]["commit"] == same["script_repository"]["commit"]
    assert same["script_and_source_are_the_same_checkout"] is True
    # The measured tree leads: grafx_commit describes what --src named.
    assert same["grafx_commit"] == same["source_repository"]["commit"]

    foreign_src = tmp_path / "elsewhere" / "src"
    foreign_src.mkdir(parents=True)
    elsewhere = build_parser().parse_args(["--src", str(foreign_src)])
    apart = provenance(elsewhere)
    assert apart["script_and_source_are_the_same_checkout"] is False
    assert apart["source_repository"]["path"] == str(foreign_src.resolve())
    assert apart["script_repository"]["commit"] == same["script_repository"]["commit"]


def test_the_cleanliness_check_sees_a_real_change_from_inside_the_source_directory() -> None:
    """`git status -- src tests` run from src/ asks about src/src and src/tests.

    Those do not exist, so it always answers 'clean'. This test is here because that false
    clean is exactly the kind of provenance that certifies a dirty tree as pristine.
    """
    from tools.measure_multiclient_matrix import describe_repository

    from_src = describe_repository(SOURCE_ROOT)
    from_top = describe_repository(PROJECT_ROOT)

    assert from_src["toplevel"] == from_top["toplevel"]
    # Asked from either place, the answer about the product tree is the same one.
    assert from_src["src_tests_clean"] == from_top["src_tests_clean"]
    assert from_src["commit"] == from_top["commit"]


def test_a_certification_board_is_refused_by_name_at_any_depth(tmp_path: Path) -> None:
    """Those boards are forensic evidence: opening one replays its WAL over what it certifies.

    The refusal is fail-closed and by name, because the alternative is trusting whoever runs
    the tool to remember which directory they pointed at.
    """
    from tools.measure_multiclient_matrix import refuse_a_certified_board

    for forbidden in ("m7-cert-2026", "m7-gate-run3"):
        board = tmp_path / forbidden / "db"
        board.mkdir(parents=True)
        with pytest.raises(SystemExit) as refused:
            refuse_a_certified_board(board)
        assert "forensic evidence" in str(refused.value)

    innocent = tmp_path / "m7-something-else" / "db"
    innocent.mkdir(parents=True)
    refuse_a_certified_board(innocent)


@pytest.mark.parametrize("option", ["--board-template", "--workspace"])
def test_the_cli_refuses_a_certification_board_before_spawning_anything(
    option: str, tmp_path: Path
) -> None:
    board = tmp_path / "m7-cert-abc"
    board.mkdir()

    with pytest.raises(SystemExit) as refused:
        main(["--src", str(SOURCE_ROOT), option, str(board)])

    # SystemExit(2) is argparse's usage error; this must be the named refusal instead.
    assert "forensic evidence" in str(refused.value)


def test_a_synthetic_run_is_labelled_not_official(tmp_path: Path) -> None:
    """A smoke on a synthetic board must never be mistaken for the section 6 official run."""
    from tools.measure_multiclient_matrix import run_case

    opts = build_parser().parse_args(["--src", str(SOURCE_ROOT)])
    assert opts.board_template is None

    # The labelling is decided before any process runs, so a bootstrap failure still carries it.
    opts.workspace = str(tmp_path)
    opts.src = str(tmp_path / "no-such-source")
    cell = run_case(opts, 1, 1, "disjoint", "autocommit", "same-table", 1.0)

    assert cell["official"] is False
    # Every reason is named, not just the first one, so nobody has to re-derive the rest.
    reasons = cell["not_official_because"]
    assert any("relocated copy" in reason for reason in reasons)
    assert any("machine-idle-asserted" in reason for reason in reasons)
    assert cell["pass"] is False


def test_the_serial_oracle_catches_an_answer_no_serial_run_could_have_produced() -> None:
    """Section 6 asks for a serial run of the SAME operation list, with zero wrong answers."""
    serial = {**LEDGER, "replayed": 3, "clean": True, "pages_checked": 40,
              "records_checked": 12, "index_entries_checked": 12,
              "initial_state": {"identical": True, "source": {"digest": "aa"},
                                "copy": {"digest": "aa"}}}

    agreeing = evaluate(**observations(serial=serial, serial_operations=3))
    assert failed(agreeing) == []

    # The concurrent database kept an owner value the serial replay never produces.
    disagreeing = evaluate(**observations(
        serial={**serial, "owner": {"1": 5, "2": 5, "3": 0}}, serial_operations=3))
    assert "serial_oracle_agrees_on_every_answer" in failed(disagreeing)

    lost_serially = evaluate(**observations(
        serial={**serial, "stored": [1, 2]}, serial_operations=3))
    assert "serial_oracle_agrees_on_every_answer" in failed(lost_serially)

    edges_differ = evaluate(**observations(
        serial={**serial, "edges": []}, serial_operations=3))
    assert "serial_oracle_agrees_on_every_answer" in failed(edges_differ)


def test_a_serial_oracle_that_did_not_run_the_whole_list_cannot_certify_it() -> None:
    """A replay that stopped early agrees with everything it never got to."""
    partial = evaluate(**observations(
        serial={**LEDGER, "replayed": 1, "clean": True}, serial_operations=40))
    assert "serial_oracle_replayed_every_acknowledged_operation" in failed(partial)

    empty = evaluate(**observations(
        serial={**LEDGER, "replayed": 0, "clean": True}, serial_operations=0))
    assert "serial_oracle_replayed_every_acknowledged_operation" in failed(empty)

    # Unless the writer was COMMANDED idle, where an empty list is the right answer rather
    # than an oracle that certified nothing while appearing to agree with everything.
    idle = evaluate(**observations(
        rate=0.0, commits=0,
        serial={**LEDGER, "replayed": 0, "clean": True}, serial_operations=0))
    assert "serial_oracle_replayed_every_acknowledged_operation" not in failed(idle)

    died = evaluate(**observations(serial={"child_failed": True}, serial_operations=3))
    names = failed(died)
    assert "serial_oracle_agrees_on_every_answer" in names
    assert "serial_oracle_replayed_every_acknowledged_operation" in names


def test_the_serial_replay_order_is_a_valid_serialisation() -> None:
    """Concatenating per-writer logs is only sound because writers never share a key.

    Each writer derives its keys from base = slot * 1_000_000 and touches nothing else, so
    operations of different writers commute and each writer's own order survives the
    concatenation. If that premise ever changes, the serial oracle silently starts comparing
    against a state no execution could reach -- so the premise is pinned here.
    """
    from tools.measure_multiclient_matrix import WRITER_CHILD

    assert "base = slot * 1_000_000" in WRITER_CHILD
    # Every key a writer writes is derived from its own base.
    assert "key = base + index * rows_per_txn + offset + 1" in WRITER_CHILD
    # And the rows it later updates or supersedes come from that same list.
    assert "update_key, update_owner = made_nodes[-1]" in WRITER_CHILD
    assert "supersede_key = made_nodes[0]" in WRITER_CHILD

    slots, rows = range(1, 9), 5000
    ranges = [range(slot * 1_000_000 + 1, slot * 1_000_000 + rows) for slot in slots]
    seen: set[int] = set()
    for span in ranges:
        assert not seen & set(span), "two writers would share a key"
        seen |= set(span)


def test_a_commit_landing_after_the_window_is_refused_rather_than_counted() -> None:
    """At N=8 and 1/s aggregate a writer waits 8s between commits; one entered near the end
    would otherwise land long after the readers stopped and inflate the rate they felt."""
    name = "no_commit_began_outside_the_window"

    # One in-flight transaction per writer is arithmetic, not a tolerance: a writer refuses
    # to START once the window closes, so at most one of its transactions can still land.
    assert name not in failed(evaluate(**observations(commits_outside_window=1, writers=1)))
    assert name not in failed(evaluate(**observations(commits_outside_window=2, writers=2)))

    late = evaluate(**observations(commits_outside_window=3, writers=1))
    assert name in failed(late)
    observed = next(item["observed"] for item in late if item["name"] == name)
    assert observed["landed_outside"] == 3
    assert observed["writers"] == 1


def test_official_is_a_conjunction_and_names_every_reason_it_failed(tmp_path: Path) -> None:
    """bool(board_template) was too permissive: a copied board still gives an unattributable
    number if nobody vouched the machine was idle or the digest was never authenticated."""
    from tools.measure_multiclient_matrix import official_shortfalls

    board = tmp_path / "board"
    board.mkdir()

    synthetic = build_parser().parse_args(["--src", str(SOURCE_ROOT)])
    reasons = official_shortfalls(synthetic)
    assert any("synthetic board" in reason for reason in reasons)
    assert any("machine-idle-asserted" in reason for reason in reasons)

    undigested = build_parser().parse_args(
        ["--src", str(SOURCE_ROOT), "--board-template", str(board),
         "--machine-idle-asserted"])
    reasons = official_shortfalls(undigested)
    assert any("board-digest" in reason for reason in reasons)
    assert not any("synthetic board" in reason for reason in reasons)

    # A source root outside any checkout has no pin, so its number cannot be attributed.
    unpinned = build_parser().parse_args(
        ["--src", str(tmp_path), "--board-template", str(board), "--board-digest", "abc",
         "--machine-idle-asserted"])
    assert any("no pin" in reason for reason in official_shortfalls(unpinned))

    # Section 6.5 freezes the long read transaction at 60 seconds. A one-second run is a
    # smoke, and letting it call itself official is how a smoke becomes a result.
    shortened = build_parser().parse_args(
        ["--src", str(SOURCE_ROOT), "--case", "matrix", "--board-template", str(board),
         "--board-digest", "abc", "--machine-idle-asserted",
         "--long-reader-seconds", "1"])
    assert any("60 seconds" in reason for reason in official_shortfalls(shortened))

    frozen = build_parser().parse_args(
        ["--src", str(SOURCE_ROOT), "--case", "matrix", "--board-template", str(board),
         "--board-digest", "abc", "--machine-idle-asserted",
         "--long-reader-seconds", "60"])
    assert not any("60 seconds" in reason for reason in official_shortfalls(frozen))

    # A subcase, an overridden axis, or the reopen workaround each disqualify by name.
    for extra, marker in (
        (["--case", "f1-curve-2proc"], "subcase"),
        (["--case", "matrix", "--writers", "2"], "overridden"),
        (["--case", "matrix", "--reopen-on-stale-index"], "works around"),
    ):
        opts = build_parser().parse_args(
            ["--src", str(SOURCE_ROOT), "--board-template", str(board),
             "--board-digest", "abc", "--machine-idle-asserted",
             "--long-reader-seconds", "60", *extra])
        assert any(marker in reason for reason in official_shortfalls(opts)), extra


def test_the_board_digest_uses_the_recorded_historical_algorithm(tmp_path: Path) -> None:
    """sha256 over relpath\\0size\\0sha256(bytes) per file, sorted.

    Pinned against an independently computed value so the digest can be compared with ones
    recorded before this tool existed, rather than only with itself.
    """
    import hashlib

    from tools.measure_multiclient_matrix import hash_board

    board = tmp_path / "board"
    (board / "inner").mkdir(parents=True)
    (board / "a.dat").write_bytes(b"alpha")
    (board / "inner" / "b.dat").write_bytes(b"beta!!")

    expected = hashlib.sha256()
    for relative, payload in (("a.dat", b"alpha"), ("inner/b.dat", b"beta!!")):
        expected.update(
            f"{relative}\0{len(payload)}\0{hashlib.sha256(payload).hexdigest()}\n".encode()
        )

    observed = hash_board(board)
    assert observed["digest"] == expected.hexdigest()
    assert observed["files"] == 2
    assert observed["bytes"] == len(b"alpha") + len(b"beta!!")


def test_the_report_declares_what_it_does_not_cover() -> None:
    """The two-process case informs CE-3; it is not the section 5b gate, and F4 is partial."""
    text = help_text()

    assert "f1-curve-2proc" in text
    assert "NOT the literal section 5b CE-3 gate" in text
    assert "deprecated" in text

    # The deprecated alias still resolves to the same finite subcase.
    alias = build_parser().parse_args(["--src", str(SOURCE_ROOT), "--case", "ce3-2proc"])
    assert len(select_cells(alias)) == len(FROZEN_READER_TARGETS) * len(
        FROZEN_FOREIGN_COMMIT_RATES)


def test_the_machine_evidence_reports_what_it_cannot_observe(tmp_path: Path) -> None:
    """H5 wants the machine's state, and an invented load figure is worse than an absent one."""
    from tools.measure_multiclient_matrix import machine_evidence

    evidence = machine_evidence()

    assert isinstance(evidence["cpu_count"], int)
    # H5 asks for CPU utilisation, which is not a load average and is not even the same
    # quantity on Windows. Either a real percentage with its source, or a named reason.
    cpu = evidence["cpu_percent"]
    assert "percent" in cpu or "unavailable" in cpu
    if "percent" in cpu:
        assert 0.0 <= cpu["percent"] <= 100.0 * (evidence["cpu_count"] or 1)
        assert cpu["source"] in ("psutil.cpu_percent", "typeperf")
    value = evidence["python_processes"]
    assert isinstance(value, int) or "unavailable" in value


def test_a_source_root_that_is_not_a_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as refused:
        main(["--src", str(tmp_path / "absent")])

    assert refused.value.code == 2


# --- the real thing ----------------------------------------------------------------------


@pytest.mark.multiprocess
@pytest.mark.timeout(600)
def test_one_real_two_process_cell_runs_and_certifies_the_database(tmp_path: Path) -> None:
    """A minimal real smoke: a live writer, a live reader, a real serial oracle.

    Short on purpose. The frozen matrix and the 60s long reader are NOT run here.
    """
    report_path = tmp_path / "smoke.json"
    code = main([
        "--src", str(SOURCE_ROOT),
        "--case", "ce3-2proc",
        "--reader-target", "same-table",
        "--foreign-commit-rate", "1",
        "--seconds", "6",
        "--txns-per-writer", "3",
        "--rows-per-txn", "1",
        "--workspace", str(tmp_path / "workspace"),
        "--json", str(report_path),
        "--reopen-on-stale-index",
    ])

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert code == 0, [item for cell in report["cells"] for item in cell["criteria"]
                       if not item["pass"]]
    assert report["pass"] is True
    (cell,) = report["cells"]

    # The run really ran: rows were committed and read, and the oracle really walked.
    assert cell["throughput"]["commits_per_second"] > 0
    assert cell["reader_latency_ms_per_process"]["1"]["n"] > 0
    assert cell["writer_latency_ms_by_family"]["create_node"]["n"] > 0
    live = next(item for item in cell["criteria"] if item["name"] == "verify_clean_live")
    assert live["observed"]["clean"] is True
    assert live["observed"]["records_checked"] > 0

    # The live pass really was live, and it is a different process from the cold one.
    held = next(item for item in cell["criteria"]
                if item["name"] == "live_pass_used_a_handle_held_through_the_run")
    assert held["pass"] is True
    assert held["observed"]["opened_before_writers"] is True
    assert held["observed"]["saw_done_flag"] is True
    # The raw timestamps travel too, so the claim is checkable rather than merely asserted,
    # and the two anchors are demonstrably different moments.
    observed = held["observed"]
    assert observed["opened_at"] < observed["writers_started_at"]
    assert observed["writers_started_at"] < observed["done_flag_written_at"]

    # The instrument's own files stayed out of the measured directory.
    layout = cell["layout"]
    assert layout["reports_outside_database"] is True
    assert not str(layout["reports_root"]).startswith(str(layout["database_root"]))
    assert Path(layout["reports_root"]).parent == Path(layout["database_root"]).parent
    for foreign in ("reports", "auditor-ready.json", "auditor-done.flag", "_reports"):
        assert foreign not in (layout["database_entries_at_end"] or [])

    # All four families were certified by effect, and the two vantage points agree.
    names = {item["name"] for item in cell["criteria"]}
    assert {"update_node_effect_stored", "mark_superseded_effect_stored",
            "create_edge_effect_stored", "live_and_cold_ledgers_agree"} <= names
    edges = next(item for item in cell["criteria"]
                 if item["name"] == "create_edge_effect_stored")
    assert edges["observed"]["expected"] > 0, "the smoke never exercised create_edge"

    # Per-process metrics, not one process standing in for the rest -- and NOT EMPTY. With
    # the default noop sink every snapshot came back with entries=(), so the report counted
    # snapshots that held nothing at all.
    captured = cell["metrics"]["captured"]
    assert captured["per_process_snapshots"] >= 2
    assert {entry["role"] for entry in captured["by_process"]} == {"writer", "reader"}
    assert cell["metrics"]["capture_errors"] is None
    rendered = json.dumps(captured["by_process"])
    assert "oktografx_" in rendered, "no product metric was actually captured"
    for entry in captured["by_process"]:
        # The product's OWN machine-readable document, per process. A repr string would not
        # be a counter anyone could compare across processes, and None would mean the sink
        # was still the noop one.
        document = entry["document"]
        assert document is not None, f"{entry['role']}-{entry['slot']} published nothing"
        assert "unreadable" not in document, document
        assert document["publications"] >= 1
        assert document["final"]["metrics"], "the published document carried no metrics"

    # This run was a smoke on a synthetic board, and says so.
    assert cell["official"] is False
    assert cell["board"] == {"synthetic": True}
    assert report["official"] is False

    # H5: the machine was observed at both ends, not merely asserted about.
    assert "machine_before" in report["provenance"]
    assert "machine_after" in report
    assert report["provenance"]["machine_idle_asserted"] is False

    # Provenance a later reader can check, and the declared gaps beside the frozen defaults.
    provenance = report["provenance"]
    assert len(provenance["script_sha256"]) == 64
    assert provenance["source_root"] == str(SOURCE_ROOT)
    assert provenance["grafx_commit"]
    assert report["parameterised_gaps"]["seconds"] == 6.0
    assert report["frozen"]["foreign_commit_rates"] == list(FROZEN_FOREIGN_COMMIT_RATES)
    assert set(report["cells"][0]["metrics"]["unavailable"]) == set(UNAVAILABLE_METRICS)


def run_pin_child(source_root: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    """Run the oracle child against one pinned source root and return the completed process."""
    from tools.measure_multiclient_matrix import ORACLE_CHILD

    return subprocess.run(
        [sys.executable, "-B", "-c", ORACLE_CHILD, str(source_root), str(tmp_path / "db"),
         json.dumps(["Item"]), "cold", json.dumps(["Links"])],
        capture_output=True, text=True, timeout=120,
    )


@pytest.mark.timeout(300)
def test_the_child_refuses_a_source_root_with_no_okto_grafx_package(tmp_path: Path) -> None:
    """A94, first half: the pin must be checked, not assumed."""
    empty = tmp_path / "not-a-source-root"
    empty.mkdir()

    done = run_pin_child(empty, tmp_path)

    # Named failure, not merely a non-zero exit: a missing database would also exit non-zero,
    # and that would make this test pass for the wrong reason.
    assert "A94-PIN-FAILED" in done.stderr
    assert "no okto_grafx package" in done.stderr


def run_guard(source_root: Path, resolved_at: Path) -> subprocess.CompletedProcess:
    """Execute the real PIN_GUARD text against an okto_grafx that resolved at a given path.

    The guard is exercised as the source it is, not as a paraphrase: PIN_GUARD is the exact
    string every child embeds. Pre-seeding sys.modules is what lets the guard see a package
    that resolved somewhere the pin does not cover -- a state a fresh child, where
    sys.path.insert(0, SRC) always wins, cannot otherwise be pushed into.
    """
    from tools.measure_multiclient_matrix import PIN_GUARD

    program = (
        "import sys, types\n"
        "fake = types.ModuleType('okto_grafx')\n"
        f"fake.__file__ = {str(resolved_at)!r}\n"
        "sys.modules['okto_grafx'] = fake\n"
        f"SRC = {str(source_root)!r}\n"
        + PIN_GUARD
        + "print('GUARD-PASSED')\n"
    )
    return subprocess.run([sys.executable, "-B", "-c", program],
                          capture_output=True, text=True, timeout=60)


def test_a_source_root_that_is_only_a_name_prefix_is_not_accepted(tmp_path: Path) -> None:
    """'src-evil' starts with 'src'. A startswith pin would sail straight past this."""
    pinned = tmp_path / "src"
    (pinned / "okto_grafx").mkdir(parents=True)
    (pinned / "okto_grafx" / "__init__.py").write_text("", encoding="utf-8")
    evil = tmp_path / "src-evil"
    (evil / "okto_grafx").mkdir(parents=True)
    (evil / "okto_grafx" / "__init__.py").write_text("", encoding="utf-8")

    # The exact string comparison a prefix pin would have accepted.
    assert str(evil).startswith(str(pinned))

    refused = run_guard(pinned, evil / "okto_grafx" / "__init__.py")
    assert refused.returncode != 0
    assert "A94-PIN-FAILED" in refused.stderr, refused.stderr[-400:]
    assert "outside the pinned source root" in refused.stderr
    assert "GUARD-PASSED" not in refused.stdout

    # The control: the same guard accepts the package that really is under the pin.
    accepted = run_guard(pinned, pinned / "okto_grafx" / "__init__.py")
    assert accepted.returncode == 0, accepted.stderr[-400:]
    assert "GUARD-PASSED" in accepted.stdout


def test_every_child_embeds_the_one_source_pin() -> None:
    """One definition, five children. A child that skipped it would measure any tree at all."""
    from tools.measure_multiclient_matrix import (
        AUDITOR_CHILD,
        BOOTSTRAP_CHILD,
        ORACLE_CHILD,
        PIN_GUARD,
        READER_CHILD,
        WRITER_CHILD,
    )

    for child in (WRITER_CHILD, READER_CHILD, BOOTSTRAP_CHILD, ORACLE_CHILD, AUDITOR_CHILD):
        assert PIN_GUARD in child
        assert "__PIN_GUARD__" not in child, "the marker was never substituted"
