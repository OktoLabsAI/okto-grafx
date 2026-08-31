"""The F1 instrument must fail loudly on a broken run, and never quietly report a good one.

Most of these drive ``evaluate`` directly. That is the point: the observations that matter most
-- a dead child, a torn read, a durable index refusal -- appear only intermittently in a short
real run, so a criterion exercised solely by running the tool is a criterion nobody has ever
seen fail. Feeding synthetic observations is how each judgement gets proven.
"""

from __future__ import annotations

import json
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
LEDGER = {"stored": [1, 2, 3], "owner": {"1": 5, "2": 5, "3": 9},
          "superseded": [1], "edges": [[1, 2]]}
LIVE_MARKS = {"held_open_through_run": True, "opened_before_writers": True,
              "saw_done_flag": True, "pid": 4242, "waited_seconds": 1.0}


def observations(**overrides: object) -> dict:
    """A cell that passed, so each test can spoil exactly one thing and watch it fail."""
    baseline = {
        "readers": 1, "rate": 1.0, "reopen_workaround": False,
        "child_failures": [], "missing": [],
        "live": {**CLEAN_WALK, **LEDGER, **LIVE_MARKS},
        "cold": {**CLEAN_WALK, **LEDGER},
        "acknowledged": [1, 2, 3], "stored_list": [1, 2, 3],
        "torn": [], "escapes": [], "durable": [], "reopens": 0,
        "commits": 7, "statements": 40,
        "expected_owner": {"1": 5, "2": 5, "3": 9},
        "expected_superseded": [1],
        "expected_edges": [[1, 2]],
    }
    baseline.update(overrides)
    return baseline


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

    truncated = evaluate(**observations(rate=10.0, commits=100,
                                        writer_span=10.0, reader_window=20.0))
    assert name in failed(truncated)
    observed = next(item["observed"] for item in truncated if item["name"] == name)
    assert observed["coverage"] == 0.5
    assert observed["labelled_rate"] == 10.0
    # The number a reader of the report should trust is the one over the reader's window.
    assert observed["effective_rate_over_reader_window"] == 5.0

    covered = evaluate(**observations(rate=10.0, commits=200,
                                      writer_span=19.8, reader_window=20.0))
    assert failed(covered) == []

    # With no foreign traffic commanded there is no coverage question to ask.
    idle = evaluate(**observations(rate=0.0, commits=0,
                                   writer_span=0.0, reader_window=20.0))
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
    text = build_parser().format_help()
    rate_help = text[text.index("--foreign-commit-rate"):]

    assert "AGGREGATE" in rate_help
    assert "sweeps the frozen curve" in rate_help
    # The old text promised omission meant unpaced. select_cells never did that.
    assert "omitting the option entirely means unpaced" not in text


def test_help_separates_the_frozen_dimensions_from_the_parameterised_gaps() -> None:
    text = build_parser().format_help()

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
        "--foreign-commit-rate", "10",
        "--seconds", "3",
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

    # Per-process metrics, not one process standing in for the rest.
    captured = cell["metrics"]["captured"]
    assert captured["per_process_snapshots"] >= 2
    assert {entry["role"] for entry in captured["by_process"]} == {"writer", "reader"}

    # Provenance a later reader can check, and the declared gaps beside the frozen defaults.
    provenance = report["provenance"]
    assert len(provenance["script_sha256"]) == 64
    assert provenance["source_root"] == str(SOURCE_ROOT)
    assert provenance["grafx_commit"]
    assert report["parameterised_gaps"]["seconds"] == 3.0
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
