"""Running one statement, and printing what came back -- including a refusal (C12).

CONTRACT.md section 10 gives this engine two statement doors: ``db.execute`` is the autocommit
READ, and a write goes through ``db.begin("write")`` plus ``txn.execute(...)``. A method called
``execute`` that refuses half of what a user will hand it needs the narrower contract to be
visible at the point of use, so this command line spells the two doors out and, when a caller
reaches for the wrong one, says which one they wanted.

The refusal itself is never edited. What this build cannot do yet -- SET, DELETE, traversal --
refuses with a typed error naming the missing door, and these tests assert that the class, the
code, the message and the details arrive exactly as the engine wrote them. Every assertion here
is about HOW a refusal is rendered rather than about WHICH statements refuse, so the suite keeps
meaning as more of the engine lands: row writing arrived while this component was being built,
and nothing here had to change its claim.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_grafx.cli.exits import OK, REFUSED

from tests.cli.conftest import TABLE_STATEMENT, CliRunner

INSERT_STATEMENT: str = "CREATE (:Person {id: 1, name: 'Ada'})"
"""A row write. It landed mid-build, so nothing here asserts that it refuses."""

UNKNOWN_LABEL_STATEMENT: str = "MATCH (p:NoSuchTable) RETURN p.name"
"""A statement the planner refuses whatever else this build can do, used to pin how a refusal
is rendered. Choosing a refusal that cannot become supported is the difference between a test
about rendering and a test that quietly starts measuring nothing (A48)."""


def test_a_read_query_prints_its_columns_and_its_row_count(
    database_path: str, cli: CliRunner
) -> None:
    run = cli("query", database_path, "MATCH (p:Person) RETURN p.name")
    assert run.code == OK
    assert "p.name" in run.out
    assert "rows       0" in run.out
    assert "(no rows)" in run.out


def test_a_read_query_reports_machine_readably(database_path: str, cli: CliRunner) -> None:
    document = cli("query", database_path, "MATCH (p:Person) RETURN p.name", "--json").document
    assert document["columns"] == ["p.name"]
    assert document["rows"] == []
    assert document["row_count"] == 0
    assert document["write"] is False
    assert document["exit_code"] == OK


def test_a_write_statement_commits_and_reports_what_it_changed(
    tmp_path: str, cli: CliRunner, empty_database_path: str
) -> None:
    run = cli("query", empty_database_path, TABLE_STATEMENT, "--write", "--json")
    assert run.code == OK
    assert run.document["write"] is True
    statistics = run.document["statistics"]
    assert int(statistics["tables_created"]) == 1
    # The commit is durable, so a fresh open of the same database can read the table back.
    after = cli("query", empty_database_path, "MATCH (p:Person) RETURN p.name", "--json")
    assert after.code == OK
    assert after.document["columns"] == ["p.name"]


def test_a_schema_statement_without_the_write_flag_is_refused(
    empty_database_path: str, cli: CliRunner
) -> None:
    # Autocommit runs a read transaction, so a schema statement is refused there and the refusal
    # reaches the operator as a refusal rather than as a stack trace.
    run = cli("query", empty_database_path, TABLE_STATEMENT)
    assert run.code == REFUSED
    assert "transaction_state" in run.err
    assert "Traceback" not in run.text


def test_a_typed_refusal_is_printed_as_a_refusal_with_its_code(
    database_path: str, cli: CliRunner
) -> None:
    run = cli("query", database_path, UNKNOWN_LABEL_STATEMENT)
    assert run.code == REFUSED
    assert "refused" in run.err
    assert "plan_error" in run.err
    assert "will not succeed if tried again" in run.err
    assert "NoSuchTable" in run.err
    assert "Traceback" not in run.text
    assert run.out == ""


def test_a_typed_refusal_carries_its_taxonomy_into_the_document(
    database_path: str, cli: CliRunner
) -> None:
    document = cli("query", database_path, UNKNOWN_LABEL_STATEMENT, "--json").document
    error = document["error"]
    assert error["code"] == "plan_error"
    assert error["type"] == "GrafxPlanError"
    assert error["retryable"] is False
    assert error["message"].strip()
    assert isinstance(error["details"], dict)
    assert document["exit_code"] == REFUSED
    assert document["result"] == "refused"


def test_a_row_write_either_lands_or_refuses_in_a_way_an_operator_can_read(
    database_path: str, cli: CliRunner
) -> None:
    # Row writing arrived while this component was being built. The property that has to hold in
    # both worlds is the one asserted here: the answer is legible, typed and never a traceback.
    run = cli("query", database_path, INSERT_STATEMENT, "--write", "--json")
    assert "Traceback" not in run.text
    if run.code == OK:
        assert int(run.document["statistics"].get("rows_created", 0)) >= 1
        read_back = cli("query", database_path, "MATCH (p:Person) RETURN p.id", "--json")
        assert read_back.code == OK
        assert read_back.document["row_count"] >= 1, "a committed row must be readable"
    else:
        assert run.code == REFUSED
        assert run.document["error"]["code"].strip()
        assert run.document["error"]["retryable"] is False


def test_the_verified_write_shape_works_as_one_transaction(
    tmp_path: Path, cli: CliRunner
) -> None:
    # The shape the coordinator drove by hand against the public API:
    #
    #     with db.begin("write") as txn:
    #         txn.execute("CREATE NODE TABLE Person(...)")
    #         txn.execute("CREATE (:Person {id: 1, name: 'Ada'})")
    #     db.execute("MATCH (p:Person) RETURN p.id, p.name")
    #
    # Two statements in ONE transaction, then an autocommit read. A command line that could only
    # run one statement per invocation could not express the transaction boundary at all.
    path = str(tmp_path / "shape")
    written = cli(
        "query",
        path,
        TABLE_STATEMENT,
        INSERT_STATEMENT,
        "--write",
        "--create",
        "--json",
    )
    assert written.code == OK, written.text
    document = written.document
    assert document["write"] is True
    assert len(document["statements"]) == 2
    assert len(document["results"]) == 2
    assert int(document["results"][0]["statistics"]["tables_created"]) == 1
    assert int(document["results"][1]["statistics"]["rows_created"]) == 1

    read = cli("query", path, "MATCH (p:Person) RETURN p.id, p.name", "--json")
    assert read.code == OK
    assert read.document["columns"] == ["p.id", "p.name"]
    assert read.document["row_count"] == 1
    assert read.document["rows"][0][1] == "Ada"


def test_several_statements_in_one_transaction_say_so(tmp_path: Path, cli: CliRunner) -> None:
    run = cli(
        "query", str(tmp_path / "block"), TABLE_STATEMENT, INSERT_STATEMENT, "--write", "--create"
    )
    assert run.code == OK
    assert "2 statements committed together in one write transaction" in run.out
    assert run.out.count("statement  ") == 2


def test_the_last_statement_is_the_answer_the_command_reports(
    tmp_path: Path, cli: CliRunner
) -> None:
    # A schema change followed by a read answers with the read, so the top-level summary has to
    # describe the last result. It is taken from `results` rather than recomputed, so the summary
    # and the detail cannot disagree (A24).
    path = str(tmp_path / "answer")
    cli("query", path, TABLE_STATEMENT, INSERT_STATEMENT, "--write", "--create")
    second = INSERT_STATEMENT.replace("id: 1", "id: 2").replace("Ada", "Bo")
    document = cli(
        "query", path, second, "MATCH (p:Person) RETURN p.id", "--write", "--json"
    ).document
    assert len(document["results"]) == 2
    assert document["columns"] == document["results"][-1]["columns"] == ["p.id"]
    assert document["row_count"] == document["results"][-1]["row_count"]
    assert document["rows"] == document["results"][-1]["rows"]
    assert document["statistics"] == document["results"][-1]["statistics"]


def test_a_read_inside_a_write_transaction_sees_its_staged_row(
    tmp_path: Path, cli: CliRunner
) -> None:
    # M-PULSE-1A added the owner-only PendingRowRef overlay: later statements in the same write
    # transaction see staged nodes without exposing provisional physical ids. A later reader sees
    # the same row after commit. This CLI contract must track that engine contract rather than the
    # pre-overlay behavior it originally measured.
    path = str(tmp_path / "isolation")
    assert cli("query", path, TABLE_STATEMENT, "--write", "--create").code == OK
    inside = cli(
        "query", path, INSERT_STATEMENT, "MATCH (p:Person) RETURN p.id", "--write", "--json"
    )
    assert inside.code == OK
    assert inside.document["results"][0]["statistics"]["rows_created"] == 1
    assert inside.document["results"][-1]["row_count"] == 1, (
        "the owner sees its staged row"
    )
    after = cli("query", path, "MATCH (p:Person) RETURN p.id", "--json")
    assert after.code == OK
    assert after.document["row_count"] == 1, "the committed row is visible to a later reader"


def test_rows_staged_in_a_transaction_that_refuses_are_discarded(
    tmp_path: Path, cli: CliRunner
) -> None:
    # The half of the rollback this build really delivers, measured rather than assumed. The
    # other half -- a schema statement surviving the rollback -- is an engine gap this component
    # reported and deliberately does NOT assert, because asserting a defect freezes it.
    path = str(tmp_path / "rollback")
    assert cli("query", path, TABLE_STATEMENT, "--write", "--create").code == OK
    refused = cli("query", path, INSERT_STATEMENT, UNKNOWN_LABEL_STATEMENT, "--write")
    assert refused.code == REFUSED
    assert "plan_error" in refused.err
    after = cli("query", path, "MATCH (p:Person) RETURN p.id", "--json")
    assert after.code == OK
    assert after.document["row_count"] == 0, "a row from a rolled-back transaction is visible"


def test_a_single_statement_still_reports_the_way_it_always_did(
    database_path: str, cli: CliRunner
) -> None:
    # A34: accepting several statements must not change the one-statement shape an operator and
    # a script already read.
    document = cli("query", database_path, "MATCH (p:Person) RETURN p.name", "--json").document
    assert document["statements"] == ["MATCH (p:Person) RETURN p.name"]
    assert document["statement"] == "MATCH (p:Person) RETURN p.name"
    assert document["columns"] == ["p.name"]
    assert len(document["results"]) == 1


def test_a_statement_that_writes_without_the_write_flag_is_pointed_at_the_right_door(
    empty_database_path: str, cli: CliRunner
) -> None:
    # CONTRACT.md section 10 gives db.execute the autocommit READ and db.begin("write") the
    # write, and a user reading a method called "execute" does not see that line. The engine's
    # refusal cannot know which door was meant; this layer can, so it says so BESIDE the refusal.
    run = cli("query", empty_database_path, TABLE_STATEMENT)
    assert run.code == REFUSED
    # The refusal itself arrives untouched: class, code, message and details as the engine wrote
    # them. Nothing here is reworded or replaced.
    assert "transaction_state" in run.err
    assert "GrafxTransactionStateError" in run.err
    # The message moved when the schema door gained its up-front MODE refusal (round 6): a DDL
    # through the read door used to be refused late, by staging, AFTER it had registered
    # indexes. The class and code are the stable surface the hint keys on; the message is the
    # engine's and says what was wrong with the mode.
    assert "opened 'read'" in run.err
    # And the hint is an extra line, naming the flag and both doors.
    assert "hint" in run.err
    assert "--write" in run.err
    assert "db.begin('write')" in run.err
    assert "autocommit read" in run.err


def test_the_hint_carries_into_the_machine_readable_document(
    empty_database_path: str, cli: CliRunner
) -> None:
    document = cli("query", empty_database_path, TABLE_STATEMENT, "--json").document
    assert document["error"]["code"] == "transaction_state"
    assert "--write" in str(document["hint"])


def test_the_door_the_hint_names_is_the_one_that_works(
    empty_database_path: str, tmp_path: Path, cli: CliRunner
) -> None:
    # A72 aimed at advice rather than at a fixture: a hint that names a remedy has to be checked
    # against the remedy actually working, or it is a sentence nobody proved.
    #
    # The two halves run on SEPARATE databases on purpose. Retrying on the same one measures what
    # the refused attempt left behind rather than what the hinted door does, and this build has a
    # cross-component defect there -- a refused schema statement still reaches the catalog -- so
    # the second half would fail for a reason that has nothing to do with the hint.
    refused = cli("query", empty_database_path, TABLE_STATEMENT)
    assert refused.code == REFUSED
    assert "--write" in refused.err
    accepted = cli("query", str(tmp_path / "hinted"), TABLE_STATEMENT, "--write", "--create",
                   "--json")
    assert accepted.code == OK
    assert int(accepted.document["statistics"]["tables_created"]) == 1


def test_a_refusal_the_write_flag_would_not_fix_carries_no_hint(
    database_path: str, cli: CliRunner
) -> None:
    # A hint that fires on every refusal is noise, and a hint that names a remedy which would
    # not have helped is worse than none. This one is keyed on the class and the command line.
    plan = cli("query", database_path, UNKNOWN_LABEL_STATEMENT)
    assert plan.code == REFUSED
    assert "hint" not in plan.err
    written = cli("query", database_path, INSERT_STATEMENT, "--write")
    if written.code == OK:
        # SET runs now; what the write flag cannot fix is a statement the SCHEMA refuses. The
        # refusal is a plan error, and the hint -- "add --write" -- would be a lie about it.
        unfixable = cli(
            "query", database_path, "MATCH (p:Person) SET p.nosuch = 'Grace'", "--write"
        )
        assert unfixable.code == REFUSED
        assert "plan_error" in unfixable.err
        assert "hint" not in unfixable.err


def test_no_other_command_ever_emits_the_write_door_hint(
    tmp_path: Path, cli: CliRunner
) -> None:
    for argv in (("verify",), ("status",), ("recovery",), ("metrics",)):
        run = cli(*argv, str(tmp_path / "absent"))
        assert run.code == REFUSED
        assert "hint" not in run.err, argv


def test_a_statement_that_cannot_be_parsed_is_refused_by_the_engine(
    database_path: str, cli: CliRunner
) -> None:
    run = cli("query", database_path, "THIS IS NOT A STATEMENT")
    assert run.code == REFUSED
    assert "refused" in run.err
    assert "Traceback" not in run.text


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [
        ("id=1", 1),
        ("id=-1", -1),
        ("id=1.5", 1.5),
        ("flag=true", True),
        ("flag=false", False),
        ("nothing=null", None),
        ("name=Ada", "Ada"),
        ('name="Ada"', "Ada"),
        ("name=", ""),
        ("name=with spaces", "with spaces"),
        ("name=01", "01"),
    ],
)
def test_a_parameter_value_is_json_when_it_is_json_and_text_otherwise(
    spelling: str, expected: object
) -> None:
    from okto_grafx.cli.commands import _parameters
    from okto_grafx.cli.parser import COMMANDS, Invocation

    spec = next(item for item in COMMANDS if item.label == "query")
    invocation = Invocation(
        spec=spec, positionals=("db", "MATCH (n) RETURN n"), options={"parameter": (spelling,)}
    )
    name = spelling.split("=", 1)[0]
    assert _parameters(invocation)[name] == expected


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_a_non_finite_parameter_is_refused_rather_than_parsed(
    literal: str, database_path: str, cli: CliRunner
) -> None:
    # LESSONS L14: a format a caller can construct must be validated where the caller reaches it.
    # Python's JSON reader accepts these three, and a non-finite number entering a query from a
    # command line is exactly the value that becomes persistable further down.
    run = cli(
        "query", database_path, "MATCH (p:Person) RETURN p.name", "--parameter", f"x={literal}"
    )
    assert run.code == REFUSED
    assert "may not be" in run.err


@pytest.mark.parametrize("spelling", ["novalue", "=1", "="])
def test_a_parameter_that_names_nothing_is_refused(
    spelling: str, database_path: str, cli: CliRunner
) -> None:
    run = cli(
        "query", database_path, "MATCH (p:Person) RETURN p.name", "--parameter", spelling
    )
    assert run.code == REFUSED
    assert "NAME=VALUE" in run.err or "more than once" in run.err


def test_the_same_parameter_given_twice_is_refused(database_path: str, cli: CliRunner) -> None:
    run = cli(
        "query",
        database_path,
        "MATCH (p:Person) RETURN p.name",
        "--parameter",
        "id=1",
        "--parameter",
        "id=2",
    )
    assert run.code == REFUSED
    assert "more than once" in run.err


def test_a_statement_beginning_with_a_dash_can_still_be_passed(
    database_path: str, cli: CliRunner
) -> None:
    run = cli("query", database_path, "--", "-- not a statement")
    assert run.code == REFUSED
    assert "refused" in run.err, "the engine refused it, not the parser"


def test_rows_are_printed_as_a_table_and_can_be_limited(
    database_path: str, cli: CliRunner
) -> None:
    written = cli("query", database_path, INSERT_STATEMENT, "--write")
    if written.code != OK:
        # Row writing is not available in this build. Never a skip: an unattributed skip is a
        # test that vanishes, and the property below still has to hold for a refusal.
        assert written.code == REFUSED
        assert "refused" in written.err
        return
    for identifier in (2, 3):
        cli(
            "query",
            database_path,
            f"CREATE (:Person {{id: {identifier}, name: 'Row{identifier}'}})",
            "--write",
        )
    full = cli("query", database_path, "MATCH (p:Person) RETURN p.id, p.name")
    assert full.code == OK
    assert "p.id" in full.out and "p.name" in full.out
    assert "Ada" in full.out
    limited = cli(
        "query", database_path, "MATCH (p:Person) RETURN p.id, p.name", "--limit", "1", "--json"
    )
    assert limited.code == OK
    assert int(limited.document["row_count"]) >= 2, "the count reports all rows, not the shown ones"
    limited_text = cli(
        "query", database_path, "MATCH (p:Person) RETURN p.id, p.name", "--limit", "1"
    )
    assert "more row(s) not shown" in limited_text.out


def test_a_query_against_a_read_only_open_says_recovery_did_not_run(
    database_path: str, cli: CliRunner
) -> None:
    run = cli("query", database_path, "MATCH (p:Person) RETURN p.name", "--read-only")
    assert run.code == OK
    assert "recovery did not run" in run.out
