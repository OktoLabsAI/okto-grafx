"""Reading a command line: what is accepted, and how a bad one is refused (C12).

The parser is the first thing a malformed argument meets, and it is the reason a malformed
argument cannot become a traceback. Every refusal here is checked for three things: it returns
the usage code, it names what was wrong, and it never opened anything on the way.
"""

from __future__ import annotations

import pytest

from okto_grafx import __version__
from okto_grafx.cli.exits import OK, USAGE
from okto_grafx.cli.parser import (
    COMMANDS,
    CONNECTION_OPTIONS,
    JSON_OPTION,
    MAX_ARGUMENT_LENGTH,
    PROGRAM_NAME,
    command_help,
    help_text,
    parse,
    wants_machine_output,
)
from tests.cli.conftest import CliRunner


def _assert_required_attribution(text: str) -> None:
    assert "Okto Grafx" in text
    assert "Okto Labs" in text
    assert "Copyright 2026 Okto Labs" in text
    assert "Elastic License 2.0 + SaaS/Branding Addendum" in text


def test_the_command_table_covers_the_operator_surface() -> None:
    labels = {spec.label for spec in COMMANDS}
    assert labels == {
        "catalogs",
        "workspace resolve",
        "schema",
        "indexes",
        "capabilities",
        "search text",
        "search vector",
        "search hybrid",
        "status",
        "verify",
        "query",
        "recovery",
        "ledger list",
        "ledger inspect",
        "ledger export",
        "quarantine list",
        "quarantine inventory",
        "quarantine inspect",
        "quarantine read",
        "metrics",
        "control downgrade",
    }


def test_no_two_commands_share_a_label() -> None:
    labels = [spec.label for spec in COMMANDS]
    assert len(set(labels)) == len(labels)


@pytest.mark.parametrize("spec", COMMANDS, ids=lambda item: item.label)
def test_every_command_documents_itself_and_accepts_the_shared_options(spec: object) -> None:
    assert spec.summary.strip().endswith(".")
    names = {option.name for option in spec.options}
    assert JSON_OPTION.name in names
    if spec.name in ("catalogs", "workspace"):
        assert "--allow-root" in names
        assert "--create" not in names and "--read-only" not in names
        assert not {option.name for option in CONNECTION_OPTIONS}.intersection(names)
    elif spec.positionals:
        for option in CONNECTION_OPTIONS:
            assert option.name in names
    else:
        assert names == {JSON_OPTION.name}, "build discovery must not pretend to open storage"
    assert len(names) == len(spec.options), "an option is declared twice"
    text = command_help(spec)
    assert PROGRAM_NAME in text
    assert spec.summary in text
    _assert_required_attribution(text)


def test_the_full_help_names_every_command_and_every_exit_code() -> None:
    text = help_text()
    _assert_required_attribution(text)
    for spec in COMMANDS:
        assert spec.label in text
    for code in (0, 1, 2, 3, 4, 5, 6, 70, 130):
        assert f" {code}  " in text or f"{code}  " in text


@pytest.mark.parametrize("token", ["--help", "-h", "help"])
def test_asking_for_help_is_a_success_on_standard_output(token: str, cli: CliRunner) -> None:
    run = cli(token)
    assert run.code == OK
    assert PROGRAM_NAME in run.out
    _assert_required_attribution(run.out)
    assert run.err == ""


@pytest.mark.parametrize("token", ["--version", "-V", "version"])
def test_asking_for_the_version_reports_the_package_version(token: str, cli: CliRunner) -> None:
    run = cli(token)
    assert run.code == OK
    assert run.out.splitlines()[0] == f"{PROGRAM_NAME} {__version__}"
    _assert_required_attribution(run.out)


def test_help_for_one_command_describes_only_that_command(cli: CliRunner) -> None:
    run = cli("verify", "--help")
    assert run.code == OK
    assert "usage: oktografx verify PATH" in run.out
    assert "--scope" in run.out


def test_help_for_a_subcommanded_command_lists_its_subcommands(cli: CliRunner) -> None:
    run = cli("ledger", "--help")
    assert run.code == OK
    for subcommand in ("ledger list", "ledger inspect", "ledger export"):
        assert subcommand in run.out

    quarantine = cli("quarantine", "--help")
    for subcommand in (
        "quarantine list",
        "quarantine inventory",
        "quarantine inspect",
        "quarantine read",
    ):
        assert subcommand in quarantine.out


def test_quarantine_inventory_is_declared_forced_read_only() -> None:
    spec = next(item for item in COMMANDS if item.label == "quarantine inventory")

    assert spec.force_read_only is True
    assert "always opens read-only" in command_help(spec)


def test_quarantine_inventory_refuses_create_during_parsing() -> None:
    outcome = parse(["quarantine", "inventory", "database", "--create"])

    assert outcome.invocation is None
    assert outcome.exit_code == USAGE
    assert "always read-only" in outcome.message
    assert "Nothing was opened or created" in outcome.message


@pytest.mark.parametrize(
    ("argv", "fragment"),
    [
        ((), "No command was given."),
        (("frobnicate",), "Unknown command"),
        (("-x",), "before any command"),
        (("verify",), "needs 1 argument"),
        (("verify", "a", "b"), "takes 1 argument"),
        (("verify", "a", "--nope"), "does not accept"),
        (("verify", "a", "--scope"), "needs a value"),
        (("verify", "a", "--scope", "banana"), "must be one of"),
        (("verify", "a", "--scope", "pages", "--scope", "all"), "more than once"),
        (("verify", "a", "--page-size", "big"), "needs a whole number"),
        (("verify", "a", "--page-size", "0"), "must be at least"),
        (("verify", "a", "--page-size", "1_0"), "needs a whole number"),
        (("verify", "a", "--page-size", " 8"), "needs a whole number"),
        (("verify", "a", "--page-size", "+8"), "needs a whole number"),
        (("verify", ""), "must not be empty"),
        (("verify", "with\x00null"), "null character"),
        (("query", "a"), "needs 2 argument"),
        (("query", "a", "   "), "must not be empty"),
        (("ledger",), "needs a subcommand"),
        (("ledger", "wibble"), "Unknown 'ledger' subcommand"),
        (("ledger", "inspect", "a", "not-a-number"), "ENTRY_ID must be a whole number"),
        (("ledger", "inspect", "a", "-3"), "ENTRY_ID must be a whole number"),
        (("quarantine", "inspect", "a", "  "), "must not be empty"),
    ],
)
def test_a_command_line_that_cannot_be_read_is_refused_by_name(
    argv: tuple[str, ...], fragment: str, cli: CliRunner
) -> None:
    run = cli(*argv)
    assert run.code == USAGE
    assert fragment in run.err, run.err
    _assert_required_attribution(run.err)
    assert run.out == "", "a text refusal must not be written to standard output"


@pytest.mark.parametrize(
    ("argv", "fragment"),
    [
        (("--json",), "before any command"),
        (("verify", "a", "--json=yes"), "takes no value"),
        (("verify", "a", "--json", "--json"), "more than once"),
        (("--json", "verify"), "before any command"),
    ],
)
def test_a_refusal_answers_in_the_form_the_caller_asked_for(
    argv: tuple[str, ...], fragment: str, cli: CliRunner
) -> None:
    # A caller that asked for a machine-readable answer gets one even when the command line was
    # the thing that was wrong; otherwise its parser meets prose exactly when it least expects it.
    run = cli(*argv)
    assert run.code == USAGE
    document = run.document
    assert document["exit_code"] == USAGE
    assert document["result"] == "usage"
    assert fragment in str(document["message"])
    assert run.err == ""


def test_a_refusal_shows_the_usage_summary_so_the_next_attempt_can_succeed(
    cli: CliRunner,
) -> None:
    run = cli("frobnicate")
    assert "usage: oktografx" in run.err
    for spec in COMMANDS:
        assert spec.label in run.err


@pytest.mark.parametrize(
    "spelling",
    [
        chr(0x0663) + chr(0x0662),
        chr(0x0669),
        chr(0x1D7E4),
        chr(0xFF11) + chr(0xFF16),
    ],
    ids=["arabic-indic", "arabic-indic-single", "mathematical", "fullwidth"],
)
def test_a_digit_outside_the_ascii_plane_is_not_a_number_this_tool_accepts(
    spelling: str, cli: CliRunner
) -> None:
    # ``str.isdigit`` is True for all of these and ``int`` parses every one, so a numeric option
    # would silently accept a page size an operator could not have typed. LESSONS L5: a corpus
    # that shares an alphabet with its assertions cannot find this.
    run = cli("verify", "somewhere", "--page-size", spelling)
    assert run.code == USAGE
    assert "needs a whole number" in run.err


def test_the_digits_this_tool_does_accept_are_still_accepted(cli: CliRunner) -> None:
    # A34: the rule above is only meaningful if the ordinary spelling still parses.
    outcome = parse(["verify", "somewhere", "--page-size", "8192"])
    assert outcome.invocation is not None
    assert outcome.invocation.number("page_size", 0) == 8192


@pytest.mark.parametrize(
    "argv",
    [
        ("query", "db", "MATCH (n) RETURN n", "   "),
        ("query", "db", "   ", "MATCH (n) RETURN n"),
        ("query", "db", "MATCH (n) RETURN n", "MATCH (n) RETURN n", ""),
    ],
    ids=["empty-second", "empty-first", "empty-third"],
)
def test_every_repeated_statement_is_checked_not_only_the_first(
    argv: tuple[str, ...], cli: CliRunner
) -> None:
    # ``query`` takes one or more statements, so the shape rule has to run on all of them. A
    # check that stops after the first would let an empty statement through in any later slot.
    run = cli(*argv)
    assert run.code == USAGE
    assert "must not be empty" in run.err


def test_a_command_that_does_not_repeat_still_refuses_an_extra_argument(cli: CliRunner) -> None:
    # A34: making one command variadic must not make every command variadic.
    run = cli("verify", "db", "extra")
    assert run.code == USAGE
    assert "takes 1 argument" in run.err


def test_the_usage_shape_shows_which_argument_repeats() -> None:
    query = next(spec for spec in COMMANDS if spec.name == "query")
    assert query.shape == "PATH STATEMENT [STATEMENT ...]"
    verify = next(spec for spec in COMMANDS if spec.name == "verify")
    assert verify.shape == "PATH"


def test_an_oversized_argument_is_refused_rather_than_carried(cli: CliRunner) -> None:
    run = cli("verify", "x" * (MAX_ARGUMENT_LENGTH + 1))
    assert run.code == USAGE
    assert "characters long" in run.err
    assert len(run.err) < MAX_ARGUMENT_LENGTH, "the refusal must not quote the whole argument"


def test_an_option_may_be_spelled_with_an_equals_sign(database_path: str, cli: CliRunner) -> None:
    joined = cli("verify", database_path, "--scope=pages")
    separate = cli("verify", database_path, "--scope", "pages")
    assert joined.code == separate.code == OK
    assert joined.out == separate.out


def test_a_double_dash_ends_option_parsing(database_path: str, cli: CliRunner) -> None:
    # A statement can legitimately begin with a dash, and there has to be a way to pass one.
    run = cli("query", database_path, "--", "-- a comment is not a statement")
    assert run.code != USAGE
    assert "--" not in run.err.split("\n")[0]


def test_a_lone_dash_is_a_positional_not_an_option(cli: CliRunner) -> None:
    run = cli("verify", "-")
    # It is read as a path, so the refusal is about the database rather than about the grammar.
    assert run.code != USAGE
    assert "no Okto Grafx database" in run.err


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ((), False),
        (("verify", "db"), False),
        (("verify", "db", "--json"), True),
        (("--json", "verify", "db"), True),
        (("query", "db", "--", "--json"), False),
    ],
)
def test_the_machine_readable_switch_is_seen_before_parsing(
    argv: tuple[str, ...], expected: bool
) -> None:
    # A command line this tool cannot read still has to report its refusal in the form the
    # caller asked for, so the switch is found before the grammar is applied.
    assert wants_machine_output(argv) is expected


def test_parsing_never_raises_for_any_shape_of_argument() -> None:
    shapes = [
        [],
        [""],
        ["--"],
        ["--", "--"],
        ["-"],
        ["=" * 10],
        ["--=1"],
        ["verify", "--=1", "db"],
        ["verify", "db", "--scope="],
        ["ledger", "export", "db", "1", "--output="],
        ["\t"],
        ["verify", "db", "--limit", "-1"],
    ]
    for argv in shapes:
        outcome = parse(argv)
        assert outcome.exit_code in {OK, USAGE}, argv
        if outcome.invocation is None:
            assert outcome.message, argv


@pytest.mark.parametrize(
    ("argv", "fragment"),
    [
        ([1, 2], "not a string"),
        (5, "cannot be read as one"),
        (object(), "cannot be read as one"),
        ("one whole string", "not one string"),
    ],
    ids=["list-of-ints", "an-int", "an-object", "a-bare-string"],
)
def test_an_argument_list_that_is_not_a_list_of_strings_is_refused(
    argv: object, fragment: str
) -> None:
    import io

    from okto_grafx.cli.entry import main

    out, err = io.StringIO(), io.StringIO()
    code = main(argv, stdout=out, stderr=err)  # type: ignore[arg-type]
    assert code == USAGE
    assert fragment in err.getvalue()
    assert out.getvalue() == ""
