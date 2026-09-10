"""The one thing this component may never do is lie about a database (C12).

A verification that prints "clean" for a damaged database, or "damaged" for a clean one, is worse
than no verification at all -- and this build has already produced two consistency checks that
agreed with the corruption they were meant to catch, because the check and the thing checked
shared an assumption. So these tests do not read the tool's summary and believe it. They seed a
specific damage, then require that the exit code, the machine-readable verdict and the printed
word all move together, in the direction the damage went.

The damage shapes are real bytes on a real device, not a stubbed report:

* a page of zeros appended to the heap, which no checksum can claim, on a database that still
  opens -- so verification is the thing that has to notice;
* bytes overwritten inside a live page, which stops the database opening at all -- so the tool
  has to answer with damage rather than with a traceback;
* a run of zeros past the end of the log, the interior-zeros signature of SPEC-M1 TS-5, which
  recovery truncates and quarantines.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_grafx import connect
from okto_grafx.cli.exits import DAMAGED, FINDINGS, INCONCLUSIVE, OK, REFUSED
from okto_grafx.cli.parser import COMMANDS

from tests.cli.conftest import (
    TABLE_STATEMENT,
    CliRunner,
    append_blank_page,
    damage_log_tail,
    damage_page,
)


def test_a_clean_database_verifies_clean(database_path: str, cli: CliRunner) -> None:
    run = cli("verify", database_path)
    assert run.code == OK
    assert "CLEAN" in run.out
    assert "DAMAGED" not in run.out
    assert run.err == ""


def test_a_clean_database_says_so_in_the_machine_readable_document(
    database_path: str, cli: CliRunner
) -> None:
    document = cli("verify", database_path, "--json").document
    assert document["verdict"] == "clean"
    assert document["clean"] is True
    assert document["findings"] == []
    assert document["exit_code"] == OK
    assert int(document["pages_checked"]) > 0, "a clean verdict needs a walk that looked"


def test_a_damaged_page_is_reported_as_damage_and_located(
    database_path: str, cli: CliRunner
) -> None:
    append_blank_page(database_path, "heap.dat")
    run = cli("verify", database_path)
    assert run.code == FINDINGS
    assert "DAMAGED" in run.out
    assert "CLEAN" not in run.out
    assert "heap.dat" in run.out
    # The vocabulary of finding kinds belongs to the verifier, not to this layer, so what is
    # asserted is that a kind and a page reach the report -- never which kind C6 chose.
    document = cli("verify", database_path, "--json").document
    kinds = {str(finding["kind"]) for finding in document["findings"]}
    assert kinds, "a damaged page must produce at least one kind"
    for kind in kinds:
        assert kind in run.out, f"the printed report must name the finding kind {kind!r}"


def test_a_damaged_page_is_located_precisely_in_the_document(
    database_path: str, cli: CliRunner
) -> None:
    append_blank_page(database_path, "heap.dat")
    document = cli("verify", database_path, "--json").document
    assert document["verdict"] == "damaged"
    assert document["clean"] is False
    assert document["exit_code"] == FINDINGS
    findings = document["findings"]
    assert isinstance(findings, list) and findings
    for finding in findings:
        # FR-11 asks for precise location, not a count: a page number an operator can act on.
        assert finding["file"] == "heap.dat"
        assert finding["page"] == 1
        assert finding["detail"].strip()
        assert finding["kind"].strip()


def test_a_database_whose_bytes_stop_it_opening_answers_damage_not_a_traceback(
    database_path: str, cli: CliRunner
) -> None:
    damage_page(database_path, "heap.dat", offset=4000)
    run = cli("verify", database_path)
    assert run.code == DAMAGED
    assert "corruption_detected" in run.err
    assert "Traceback" not in run.text
    assert run.out == ""


def test_a_database_that_cannot_be_opened_is_never_reported_clean(
    database_path: str, cli: CliRunner
) -> None:
    damage_page(database_path, "catalog.dat", offset=8392)
    for command in (("verify", database_path), ("status", database_path)):
        run = cli(*command)
        assert run.code == DAMAGED, command
        assert "clean" not in run.out.lower(), command


@pytest.mark.parametrize("scope", ["all", "pages"])
def test_the_verdict_the_word_and_the_code_move_together(
    scope: str, database_path: str, cli: CliRunner
) -> None:
    before = cli("verify", database_path, "--scope", scope, "--json")
    assert before.code == OK and before.document["clean"] is True
    append_blank_page(database_path, "heap.dat")
    after = cli("verify", database_path, "--scope", scope, "--json")
    assert after.code == FINDINGS
    assert after.document["clean"] is False
    assert after.document["verdict"] == "damaged"
    text = cli("verify", database_path, "--scope", scope)
    assert text.code == after.code, "the two output forms must reach the same conclusion"
    assert "DAMAGED" in text.out


def test_a_walk_that_examined_nothing_is_not_reported_as_clean(
    empty_database_path: str, cli: CliRunner
) -> None:
    # The records walk over a database with no rows examines nothing. An empty finding list is
    # exactly what a clean walk produces, so calling this clean is how a verifier certifies a
    # database it never looked at.
    run = cli("verify", empty_database_path, "--scope", "records", "--json")
    assert run.code == INCONCLUSIVE
    assert run.document["verdict"] == "inconclusive"
    assert run.document["clean"] is False
    assert int(run.document["records_checked"]) == 0
    text = cli("verify", empty_database_path, "--scope", "records")
    assert "INCONCLUSIVE" in text.out
    assert "CLEAN" not in text.out


def test_status_reports_a_clean_database_as_clean(database_path: str, cli: CliRunner) -> None:
    run = cli("status", database_path)
    assert run.code == OK
    assert "this database is clean." in run.out
    assert "uuid" in run.out
    assert "recovery" in run.out


def test_status_names_every_reason_a_database_is_not_clean(
    database_path: str, cli: CliRunner
) -> None:
    damage_log_tail(database_path)
    run = cli("status", database_path, "--json")
    assert run.code == FINDINGS
    document = run.document
    reasons = document["reasons"]
    assert isinstance(reasons, list) and reasons
    assert document["recovery"]["outcome"] != "clean"
    assert int(document["recovery"]["records_discarded"]) >= 1
    assert int(document["ledger"]["total"]) >= 1
    assert int(document["quarantine"]["entries"]) >= 1
    text = cli("status", database_path)
    # The second open finds the log already repaired, so the recovery outcome is clean again --
    # but the evidence the first open preserved is still there and still makes it not clean.
    assert text.code == FINDINGS
    assert "this database is NOT clean" in text.out


def test_status_reports_the_identity_a_database_was_created_with(
    database_path: str, cli: CliRunner
) -> None:
    document = cli("status", database_path, "--json").document
    identity = document["identity"]
    with connect(database_path) as database:
        expected = database.identity
    assert identity["page_size"] == expected.page_size
    assert identity["partitions_per_table"] == expected.partitions_per_table
    assert identity["granularity_descriptor"] == expected.granularity_descriptor
    assert identity["database_uuid"].count("-") == 4, "a uuid is reported as a uuid"


def test_a_read_only_open_says_that_recovery_did_not_run(
    database_path: str, cli: CliRunner
) -> None:
    # A read-only open replays nothing, so a clean verdict from it is a statement about the
    # pages as they lie, not about the database the log would have produced. Saying so is the
    # difference between an honest answer and a misleading one.
    run = cli("verify", database_path, "--read-only")
    assert "recovery did not run" in run.out
    document = cli("verify", database_path, "--read-only", "--json").document
    assert document["recovery_ran"] is False


def test_a_read_only_status_that_knows_nothing_says_so(
    database_path: str, cli: CliRunner
) -> None:
    run = cli("status", database_path, "--read-only")
    assert run.code == INCONCLUSIVE
    assert "did not run" in run.out


def test_a_read_only_recovery_certifies_nothing(database_path: str, cli: CliRunner) -> None:
    run = cli("recovery", database_path, "--read-only")
    assert run.code == INCONCLUSIVE
    assert "certifies nothing" in run.out


def test_recovery_reports_what_the_open_replayed(database_path: str, cli: CliRunner) -> None:
    # The shared CLI fixture is checkpoint-complete so read-only commands have an honest stable
    # baseline. Put one later commit above that checkpoint for this test, whose subject is the
    # writable opener's replay report.
    with connect(database_path) as database:
        with database.begin("write") as txn:
            txn.execute("CREATE (:Person {id: 99, name: 'Recovery probe'})")
    run = cli("recovery", database_path, "--json")
    assert run.code == OK
    recovery = run.document["recovery"]
    assert recovery["outcome"] == "clean"
    assert int(recovery["records_replayed"]) >= 1
    assert run.document["source"] == "the open of this command"


def test_recovery_reports_a_discard_as_not_clean(database_path: str, cli: CliRunner) -> None:
    damage_log_tail(database_path)
    run = cli("recovery", database_path, "--json")
    assert run.code == FINDINGS
    recovery = run.document["recovery"]
    assert recovery["outcome"] in {"truncated", "quarantined"}
    assert int(recovery["records_discarded"]) >= 1
    # BR-3: every discard leaves a trace, and the report is where an operator reads it.
    assert int(recovery["ledger_entries_created"]) >= 1
    assert recovery["findings"], "a discard with no finding would be a discard without a trace"


def test_rerunning_recovery_reports_the_pass_it_ran(database_path: str, cli: CliRunner) -> None:
    run = cli("recovery", database_path, "--rerun", "--json")
    assert run.code == OK
    assert run.document["source"] == "a pass run by this command"


def _close_always_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every database refuse to release what it held, from the public close door."""
    from okto_grafx.domain.errors import GrafxStorageError
    from okto_grafx.engine.database import Database as EngineDatabase

    def refuse(_self: object) -> None:
        raise GrafxStorageError("The device would not release its descriptors.", attempts=9)

    monkeypatch.setattr(EngineDatabase, "close", refuse)


def test_a_close_that_fails_after_a_clean_run_is_reported(
    database_path: str, cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _close_always_fails(monkeypatch)
    run = cli("verify", database_path, "--json")
    assert run.code != OK, "a database that could not be released is not a clean run"
    document = run.document
    assert document["verdict"] == "clean", "the answer the operator asked for is still reported"
    assert document["close_problem"]
    assert document["exit_code"] == run.code


def test_a_close_that_fails_never_hides_damage(
    database_path: str, cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A script branching on the damage code must keep seeing it. A release failure is a second
    # problem, not a reason to stop reporting the first.
    append_blank_page(database_path, "heap.dat")
    _close_always_fails(monkeypatch)
    run = cli("verify", database_path, "--json")
    assert run.code == FINDINGS
    document = run.document
    assert document["verdict"] == "damaged"
    assert document["clean"] is False
    assert document["close_problem"]
    assert document["exit_code"] == FINDINGS


def test_the_refuse_policy_reaches_the_operator_as_a_typed_refusal(
    database_path: str, cli: CliRunner
) -> None:
    # FR-8 makes fail-closed an explicit choice rather than a default. An operator who made that
    # choice needs the refusal to arrive as one, naming the class, not as a stack trace.
    damage_log_tail(database_path)
    run = cli("verify", database_path, "--recovery-policy", "refuse")
    assert run.code == REFUSED
    assert "recovery_refused" in run.err
    assert "GrafxRecoveryRefused" in run.err
    assert "Traceback" not in run.text
    document = cli(
        "status", database_path, "--recovery-policy", "refuse", "--json"
    ).document
    assert document["error"]["code"] == "recovery_refused"
    assert document["error"]["retryable"] is False
    assert document["exit_code"] == REFUSED


def test_a_retryable_failure_reaches_the_operator_as_one(
    database_path: str, cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A lease timeout under contention is the ordinary way this arrives, and it is not
    # reproducible on demand. What has to hold is the mapping: a failure that says it is worth
    # retrying comes back under its own exit code, and the printed line says so in words.
    from okto_grafx.cli import commands
    from okto_grafx.cli.exits import RETRY
    from okto_grafx.domain.errors import GrafxLeaseTimeout

    def refuse(_invocation: object) -> object:
        raise GrafxLeaseTimeout(
            "The writer lease did not arrive before the timeout elapsed.", waited_seconds=10.0
        )

    monkeypatch.setattr(commands, "_HANDLERS", {**commands._HANDLERS, "verify": refuse})
    run = cli("verify", database_path)
    assert run.code == RETRY
    assert "lease_timeout" in run.err
    assert "may succeed if tried again" in run.err
    document = cli("verify", database_path, "--json").document
    assert document["error"]["retryable"] is True
    assert document["result"] == "retry"


def test_a_path_that_is_not_a_database_is_refused_and_nothing_is_created(
    tmp_path: Path, cli: CliRunner
) -> None:
    # An operator reaching for this tool because something is wrong must not be answered by a
    # brand-new empty database at the path they mistyped, reported as perfectly clean.
    target = tmp_path / "typo"
    run = cli("verify", str(target))
    assert run.code == REFUSED
    assert "no Okto Grafx database" in run.err
    assert not target.exists(), "a refusal must not have created anything"


def test_a_file_where_a_database_was_expected_is_refused(tmp_path: Path, cli: CliRunner) -> None:
    target = tmp_path / "notadir"
    target.write_bytes(b"this is not a database")
    run = cli("status", str(target))
    assert run.code == REFUSED
    assert "no Okto Grafx database" in run.err
    assert target.read_bytes() == b"this is not a database"


def test_creating_a_database_needs_the_flag_that_says_so(tmp_path: Path, cli: CliRunner) -> None:
    target = tmp_path / "made"
    made = cli("query", str(target), TABLE_STATEMENT, "--write", "--create")
    assert made.code == OK
    assert (target / "grafx.meta").is_file()
    again = cli("verify", str(target))
    assert again.code == OK


def test_an_in_memory_database_is_reachable_without_the_creation_flag(cli: CliRunner) -> None:
    run = cli("status", ":memory:")
    assert run.code in {OK, INCONCLUSIVE, FINDINGS}
    assert "Traceback" not in run.text


@pytest.mark.parametrize(
    "spec", [spec for spec in COMMANDS if spec.positionals == ("PATH",)], ids=lambda s: s.label
)
def test_every_path_command_refuses_a_path_that_is_not_a_database(
    spec: object, tmp_path: Path, cli: CliRunner
) -> None:
    argv = [*spec.label.split(" "), str(tmp_path / "absent")]
    if spec.name == "catalogs":
        argv.extend(("--allow-root", str(tmp_path)))
    run = cli(*argv)
    assert run.code == REFUSED, run.text
    assert ("Expected existing directory" if spec.name == "catalogs" else "no Okto Grafx database") in run.err
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize(
    "argv",
    [
        ("status",),
        ("verify",),
        ("recovery",),
        ("metrics",),
        ("ledger", "list"),
        ("quarantine", "list"),
    ],
)
def test_the_document_of_every_command_agrees_with_its_exit_code(
    argv: tuple[str, ...], database_path: str, cli: CliRunner
) -> None:
    run = cli(*argv, database_path, "--json")
    document = run.document
    assert document["exit_code"] == run.code
    assert document["command"] == " ".join(argv)
    assert document["path"] == database_path
    assert isinstance(document["result"], str) and document["result"]
