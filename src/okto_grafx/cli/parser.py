"""Reading the command line of ``oktografx`` (C12, operator surface).

This tool parses its own arguments rather than delegating, for two reasons that are both about
what an operator sees at three in the morning.

**Every message here is a literal in this file.** G1 makes en-US a property of the product's
output, not only of its identifiers. A parser whose refusals come from somewhere else has an
output surface this project does not own, and cannot promise the language of.

**Nothing here ends the process.** :func:`parse` returns a :class:`ParseOutcome` describing what
to print and what to report; it never raises for a bad command line and never calls ``exit``.
The one boundary that turns an answer into a process status is in :mod:`okto_grafx.cli.entry`,
which is also the one place a failure can be contained.

The grammar is small on purpose::

    oktografx <command> [<subcommand>] <positional>... [--option [value]]...

An option may be spelled ``--name value`` or ``--name=value``. A bare ``--`` ends option parsing,
so a statement that begins with a dash can still be passed. An option this tool does not know is
refused by name with the accepted ones listed, because a mistyped option that is silently ignored
is a setting the operator believes they applied.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from okto_grafx import __version__
from okto_grafx.cli.exits import EXIT_CODE_MEANINGS, INCONCLUSIVE, OK, USAGE
from okto_grafx.domain.verify import VERIFICATION_SCOPES
from okto_grafx.runtime.config import (
    METRICS_SINKS,
    RECOVERY_POLICIES,
    VECTOR_MATH_SELECTORS,
)

__all__ = [
    "COMMANDS",
    "CONNECTION_OPTIONS",
    "JSON_OPTION",
    "MAX_ARGUMENT_LENGTH",
    "PROGRAM_NAME",
    "CommandSpec",
    "Invocation",
    "Option",
    "ParseOutcome",
    "command_help",
    "help_text",
    "parse",
    "wants_machine_output",
]

PROGRAM_NAME: str = "oktografx"
"""The name this tool reports itself under, in usage lines and in the version banner."""

MAX_ARGUMENT_LENGTH: int = 1 << 16
"""Longest single argument this tool accepts before refusing it as a command-line mistake.

A megabyte-long option value is never a thing an operator typed, and carrying it into an error
message makes the refusal itself unreadable.
"""

_ORIGIN_CLASSES: tuple[str, ...] = ("reapplicable", "forensic")
"""The two ledger origin classes SPEC-M1 FR-9 defines, as the operator surface spells them."""


@dataclass(frozen=True, slots=True)
class Option:
    """One option a command accepts, with everything needed to parse and to document it."""

    name: str
    summary: str
    takes_value: bool = True
    metavar: str = "VALUE"
    choices: tuple[str, ...] = ()
    integer: bool = False
    minimum: int = 0
    repeatable: bool = False

    @property
    def key(self) -> str:
        """Return the name this option is stored under: the long name without its dashes."""
        return self.name.lstrip("-").replace("-", "_")

    @property
    def usage(self) -> str:
        """Return how this option is spelled in the help text."""
        return f"{self.name} {self.metavar}" if self.takes_value else self.name


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """One command of the tool: the arguments it takes and the summary it documents."""

    name: str
    summary: str
    positionals: tuple[str, ...] = ()
    options: tuple[Option, ...] = ()
    subcommand: str = ""
    details: tuple[str, ...] = ()
    repeats_last: bool = False

    @property
    def label(self) -> str:
        """Return the command as an operator types it, subcommand included."""
        return f"{self.name} {self.subcommand}" if self.subcommand else self.name

    @property
    def shape(self) -> str:
        """Return the positional shape as the help prints it."""
        if not self.positionals:
            return ""
        names = list(self.positionals)
        if self.repeats_last:
            names[-1] = f"{names[-1]} [{names[-1]} ...]"
        return " ".join(names)

    def option(self, name: str) -> Option | None:
        """Return the option with this long name, or None when the command does not accept it."""
        for option in self.options:
            if option.name == name:
                return option
        return None


@dataclass(frozen=True, slots=True)
class Invocation:
    """One fully parsed command line: which command, its positionals and its options."""

    spec: CommandSpec
    positionals: tuple[str, ...] = ()
    options: Mapping[str, object] = field(default_factory=dict)

    @property
    def path(self) -> str:
        """Return the database path this invocation names, or an empty string when it has none."""
        return self.positionals[0] if self.positionals else ""

    def flag(self, name: str) -> bool:
        """Return whether a flag was given."""
        return bool(self.options.get(name, False))

    def text(self, name: str, default: str = "") -> str:
        """Return a string option, or the default when it was not given."""
        value = self.options.get(name)
        return value if isinstance(value, str) else default

    def number(self, name: str, default: int) -> int:
        """Return an integer option, or the default when it was not given."""
        value = self.options.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) else default

    def repeated(self, name: str) -> tuple[str, ...]:
        """Return every value given for a repeatable option, in the order they were given."""
        value = self.options.get(name)
        return tuple(value) if isinstance(value, tuple) else ()


@dataclass(frozen=True, slots=True)
class ParseOutcome:
    """What reading a command line produced: an invocation to run, or text to print.

    Exactly one of the two halves is filled. When :attr:`invocation` is None the command line
    asked for help, asked for the version, or could not be read, and :attr:`message` is what the
    operator should see.
    """

    invocation: Invocation | None = None
    message: str = ""
    exit_code: int = OK
    to_stderr: bool = False


JSON_OPTION: Option = Option(
    name="--json",
    summary="Report as one JSON document on standard output instead of text.",
    takes_value=False,
)
"""The machine-readable switch every command accepts."""

CONNECTION_OPTIONS: tuple[Option, ...] = (
    Option(
        name="--page-size",
        summary="Page size to open with. Must match the size the database was created with.",
        metavar="BYTES",
        integer=True,
        minimum=1,
    ),
    Option(
        name="--partitions-per-table",
        summary="Conflict granularity for a new database; a stored value is reported, not forced.",
        metavar="COUNT",
        integer=True,
        minimum=1,
    ),
    Option(
        name="--buffer-budget-bytes",
        summary="Memory budget for resident pages of this database.",
        metavar="BYTES",
        integer=True,
        minimum=1,
    ),
    Option(
        name="--recovery-policy",
        summary="Replay to the last intact record, or refuse to open a damaged database.",
        metavar="POLICY",
        choices=tuple(sorted(RECOVERY_POLICIES)),
    ),
    Option(
        name="--metrics",
        summary="Which metrics adapter to bind while this command runs.",
        metavar="SINK",
        choices=tuple(sorted(METRICS_SINKS)),
    ),
    Option(
        name="--metrics-destination",
        summary="Where the metrics adapter writes: a file for json, a host:port for openmetrics.",
        metavar="TARGET",
    ),
    Option(
        name="--vector-math",
        summary="Which vector math adapter to bind.",
        metavar="SELECTOR",
        choices=tuple(sorted(VECTOR_MATH_SELECTORS)),
    ),
    Option(
        name="--read-only",
        summary="Open without writing anything. Recovery does NOT run, so nothing is replayed.",
        takes_value=False,
    ),
    Option(
        name="--create",
        summary="Allow creating a database at a path that does not hold one yet.",
        takes_value=False,
    ),
)
"""Options every command that opens a database accepts.

``--create`` is here because the default is the opposite: a path that is not already a database
is refused rather than created. A tool an operator reaches for when something is wrong must not
answer a mistyped path by making a new, empty, perfectly clean database and reporting it as one.
"""


def _database_command(
    name: str,
    summary: str,
    *,
    positionals: tuple[str, ...] = ("PATH",),
    options: tuple[Option, ...] = (),
    subcommand: str = "",
    details: tuple[str, ...] = (),
    repeats_last: bool = False,
) -> CommandSpec:
    """Return a command specification with the shared connection options already attached."""
    return CommandSpec(
        name=name,
        summary=summary,
        positionals=positionals,
        options=(JSON_OPTION, *options, *CONNECTION_OPTIONS),
        subcommand=subcommand,
        details=details,
        repeats_last=repeats_last,
    )


COMMANDS: tuple[CommandSpec, ...] = (
    _database_command(
        "status",
        "Open a database and report what state it is in.",
        details=(
            "Exit 1 when this database carries evidence of lost work: an open whose recovery was",
            "not clean, a ledger that holds entries, or anything in quarantine.",
        ),
    ),
    _database_command(
        "verify",
        "Walk the database and report every finding, precisely located.",
        options=(
            Option(
                name="--scope",
                summary="Which walk to run.",
                metavar="SCOPE",
                choices=tuple(sorted(VERIFICATION_SCOPES)),
            ),
        ),
        details=(
            "Exit 0 only when the walk ran AND found nothing. A walk that examined nothing exits",
            f"{INCONCLUSIVE} instead: an empty finding list is not evidence of a clean database.",
        ),
    ),
    _database_command(
        "query",
        "Run one or more statements and print what came back.",
        positionals=("PATH", "STATEMENT"),
        repeats_last=True,
        options=(
            Option(
                name="--write",
                summary="Run inside a write transaction and commit it, rather than autocommit.",
                takes_value=False,
            ),
            Option(
                name="--parameter",
                summary="A named parameter as NAME=VALUE. Repeatable.",
                metavar="NAME=VALUE",
                repeatable=True,
            ),
            Option(
                name="--limit",
                summary="Print at most this many rows. Zero prints all of them.",
                metavar="ROWS",
                integer=True,
            ),
        ),
        details=(
            "There are two doors, and CONTRACT.md section 10 draws the line between them.",
            "",
            "  Without --write the statement runs in the AUTOCOMMIT READ, db.execute(...): it",
            "  opens a read transaction, answers, and stages nothing. A statement that writes is",
            "  refused there, and the refusal says so.",
            "",
            "  With --write it runs inside a write transaction, db.begin('write') plus",
            "  txn.execute(...), and the transaction is committed when the statement succeeds.",
            "",
            "Several statements may be given. With --write they share ONE transaction, committed",
            "once after the last one and rolled back if any of them refuses. Rows staged in a",
            "transaction that rolls back are discarded -- measured. A schema statement in this",
            "build reaches the catalog before the transaction ends and is NOT undone by the",
            "rollback; that is an engine gap, reported, and this tool will not claim otherwise.",
            "Without --write each statement is its own autocommit read.",
            "",
            "    oktografx query ./mydb \\",
            "      \"CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))\" \\",
            "      \"CREATE (:Person {id: 1, name: 'Ada'})\" --write",
            "    oktografx query ./mydb \"MATCH (p:Person) RETURN p.id, p.name\"",
            "",
            "A parameter value is read as JSON when it parses as JSON, and as text otherwise, so",
            "--parameter id=1 passes the number one and --parameter name=Ada passes the string.",
        ),
    ),
    _database_command(
        "recovery",
        "Report what the recovery pass at open did.",
        options=(
            Option(
                name="--rerun",
                summary="Run another recovery pass now and report that one instead.",
                takes_value=False,
            ),
        ),
    ),
    _database_command(
        "ledger",
        "List the entries of the unapplied-work ledger.",
        subcommand="list",
        options=(
            Option(
                name="--origin-class",
                summary="Show only entries of one class.",
                metavar="CLASS",
                choices=_ORIGIN_CLASSES,
            ),
            Option(
                name="--reason",
                summary="Show only entries discarded for one reason.",
                metavar="REASON",
            ),
            Option(
                name="--limit",
                summary="Show at most this many entries.",
                metavar="COUNT",
                integer=True,
            ),
            Option(
                name="--offset",
                summary="Skip this many entries before showing any.",
                metavar="COUNT",
                integer=True,
            ),
        ),
    ),
    _database_command(
        "ledger",
        "Show one ledger entry in full, with the provenance of the work it preserved.",
        subcommand="inspect",
        positionals=("PATH", "ENTRY_ID"),
    ),
    _database_command(
        "ledger",
        "Write the preserved bytes of one forensic ledger entry to a file.",
        subcommand="export",
        positionals=("PATH", "ENTRY_ID"),
        options=(
            Option(
                name="--output",
                summary="File to write the preserved bytes to. Required.",
                metavar="FILE",
            ),
        ),
        details=(
            "The written file is confirmed by listing its directory and reading it back by name,",
            "never through the handle that wrote it, so a file written under the wrong name is",
            "reported rather than reported as success.",
        ),
    ),
    _database_command(
        "quarantine",
        "List the preserved evidence quarantine holds.",
        subcommand="list",
    ),
    _database_command(
        "quarantine",
        "Show one quarantine entry and the manifest that describes it.",
        subcommand="inspect",
        positionals=("PATH", "NAME"),
    ),
    _database_command(
        "quarantine",
        "Write the preserved bytes of one quarantine entry to a file.",
        subcommand="read",
        positionals=("PATH", "NAME"),
        options=(
            Option(
                name="--output",
                summary="File to write the preserved bytes to. Required.",
                metavar="FILE",
            ),
        ),
    ),
    _database_command(
        "metrics",
        "Report the metrics endpoint of this database and the current value of every metric.",
    ),
)
"""Every command this tool offers. The help text is rendered from this table, so the parser and
the documentation are one description rather than two that drift."""

_HELP_TOKENS: frozenset[str] = frozenset({"-h", "--help", "help"})
"""Spellings that ask for help rather than for work."""

_VERSION_TOKENS: frozenset[str] = frozenset({"-V", "--version", "version"})
"""Spellings that ask which build this is."""

_SUBCOMMANDED: frozenset[str] = frozenset(
    spec.name for spec in COMMANDS if spec.subcommand
)
"""Commands that require a subcommand, derived from the table rather than listed again."""


def wants_machine_output(argv: Sequence[str]) -> bool:
    """Return whether this command line asked for machine-readable output.

    Read before parsing so that a command line this tool cannot read still reports its refusal in
    the form the caller asked for. Only the tokens before a bare ``--`` are considered, because
    everything after it is a value rather than an option.
    """
    for token in argv:
        if token == "--":
            return False
        if token == JSON_OPTION.name or token.startswith(f"{JSON_OPTION.name}="):
            return True
    return False


def parse(argv: Sequence[str]) -> ParseOutcome:
    """Read a command line and return either an invocation to run or text to print.

    Never raises for a bad command line: an unreadable one comes back as a refusal carrying the
    usage exit code, which is what a caller has to be able to rely on.
    """
    tokens = list(argv)
    oversized = _oversized(tokens)
    if oversized is not None:
        return _usage_failure(oversized)
    if not tokens:
        return _usage_failure("No command was given.")

    head = tokens[0]
    if head in _HELP_TOKENS:
        return ParseOutcome(message=help_text(), exit_code=OK)
    if head in _VERSION_TOKENS:
        return ParseOutcome(message=_version_text(), exit_code=OK)
    if head.startswith("-"):
        return _usage_failure(
            f"Unknown option {head!r} before any command. Options come after the command, as in "
            f"'{PROGRAM_NAME} verify PATH {JSON_OPTION.name}'."
        )

    matching = [spec for spec in COMMANDS if spec.name == head]
    if not matching:
        return _usage_failure(f"Unknown command {head!r}.")

    rest = tokens[1:]
    if head in _SUBCOMMANDED:
        return _parse_subcommanded(head, matching, rest)
    spec = matching[0]
    if rest and rest[0] in _HELP_TOKENS:
        return ParseOutcome(message=command_help(spec), exit_code=OK)
    return _parse_arguments(spec, rest)


def _parse_subcommanded(
    head: str, matching: Sequence[CommandSpec], rest: Sequence[str]
) -> ParseOutcome:
    """Resolve the subcommand of a command that has several, then parse the remainder."""
    names = ", ".join(sorted(spec.subcommand for spec in matching))
    if not rest:
        return _usage_failure(f"The {head!r} command needs a subcommand. One of: {names}.")
    wanted = rest[0]
    if wanted in _HELP_TOKENS:
        return ParseOutcome(
            message="\n".join(command_help(spec) for spec in matching), exit_code=OK
        )
    for spec in matching:
        if spec.subcommand == wanted:
            tail = rest[1:]
            if tail and tail[0] in _HELP_TOKENS:
                return ParseOutcome(message=command_help(spec), exit_code=OK)
            return _parse_arguments(spec, tail)
    return _usage_failure(f"Unknown {head!r} subcommand {wanted!r}. One of: {names}.")


def _parse_arguments(spec: CommandSpec, tokens: Sequence[str]) -> ParseOutcome:
    """Split options from positionals for one command and validate both."""
    positionals: list[str] = []
    options: dict[str, object] = {}
    remaining = list(tokens)
    index = 0
    while index < len(remaining):
        token = remaining[index]
        index += 1
        if token == "--":
            positionals.extend(remaining[index:])
            break
        if token in _HELP_TOKENS:
            return ParseOutcome(message=command_help(spec), exit_code=OK)
        if not token.startswith("-") or token == "-" or _looks_like_a_number(token):
            positionals.append(token)
            continue
        name, separator, inline = token.partition("=")
        option = spec.option(name)
        if option is None:
            return _usage_failure(_unknown_option(spec, name))
        if not option.takes_value:
            if separator:
                return _usage_failure(f"{option.name} takes no value.")
            if option.key in options:
                return _usage_failure(f"{option.name} was given more than once.")
            options[option.key] = True
            continue
        if separator:
            raw = inline
        else:
            if index >= len(remaining):
                return _usage_failure(f"{option.name} needs a value: {option.usage}.")
            raw = remaining[index]
            index += 1
        stored = _store_value(option, raw, options)
        if isinstance(stored, str):
            return _usage_failure(stored)

    missing = _positional_problem(spec, positionals)
    if missing is not None:
        return _usage_failure(missing)
    problem = _value_problem(spec, positionals)
    if problem is not None:
        return _usage_failure(problem)
    return ParseOutcome(
        invocation=Invocation(
            spec=spec, positionals=tuple(positionals), options=dict(options)
        ),
        exit_code=OK,
    )


def _store_value(option: Option, raw: str, options: dict[str, object]) -> str | None:
    """Validate one option value and store it, or return the refusal text."""
    if option.repeatable:
        previous = options.get(option.key)
        collected = tuple(previous) if isinstance(previous, tuple) else ()
        options[option.key] = (*collected, raw)
        return None
    if option.key in options:
        return f"{option.name} was given more than once."
    if option.choices and raw not in option.choices:
        allowed = ", ".join(option.choices)
        return f"{option.name} must be one of: {allowed}. Got {raw!r}."
    if option.integer:
        number = _as_integer(raw)
        if number is None:
            return f"{option.name} needs a whole number. Got {raw!r}."
        if number < option.minimum:
            return f"{option.name} must be at least {option.minimum}. Got {number}."
        options[option.key] = number
        return None
    options[option.key] = raw
    return None


def _looks_like_a_number(token: str) -> bool:
    """Return True for a token that is a negative number rather than an option.

    ``-3`` where an identifier is expected is a number an operator typed, not an option they
    misspelled, and refusing it as an unknown option answers a question they did not ask. The
    value check downstream still refuses it, and does so by naming what was wrong with it.
    """
    return len(token) > 1 and token[0] == "-" and token[1:].isascii() and token[1:].isdigit()


def _as_integer(raw: str) -> int | None:
    """Return the integer a token spells, or None when it spells something else.

    Deliberately stricter than ``int()``: underscores, leading plus signs and surrounding
    whitespace all parse in Python and none of them is a number an operator meant to type.
    """
    text = raw[1:] if raw.startswith("-") else raw
    if not text.isdigit() or not text.isascii():
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _positional_problem(spec: CommandSpec, positionals: Sequence[str]) -> str | None:
    """Return the refusal for a wrong number of positional arguments, or None when they fit."""
    expected = len(spec.positionals)
    if len(positionals) == expected or (spec.repeats_last and len(positionals) > expected):
        return None
    shape = spec.shape or "(none)"
    if len(positionals) < expected:
        return (
            f"The {spec.label!r} command needs {expected} argument(s): {shape}. "
            f"Got {len(positionals)}."
        )
    extra = ", ".join(repr(item) for item in positionals[expected:])
    return f"The {spec.label!r} command takes {expected} argument(s): {shape}. Extra: {extra}."


def _value_problem(spec: CommandSpec, positionals: Sequence[str]) -> str | None:
    """Return the refusal for a positional whose shape is wrong, or None when all of them fit."""
    names = list(spec.positionals)
    if spec.repeats_last and names and len(positionals) > len(names):
        names.extend([names[-1]] * (len(positionals) - len(names)))
    for name, value in zip(names, positionals):
        if name == "PATH":
            if not value:
                return "The database path must not be empty."
            if "\x00" in value:
                return "The database path must not contain a null character."
        if name == "ENTRY_ID":
            number = _as_integer(value)
            if number is None or number < 0:
                return f"ENTRY_ID must be a whole number of zero or more. Got {value!r}."
        if name == "STATEMENT" and not value.strip():
            return "The statement must not be empty."
        if name == "NAME" and not value.strip():
            return "The quarantine entry name must not be empty."
    return None


def _unknown_option(spec: CommandSpec, name: str) -> str:
    """Return the refusal for an option this command does not accept, listing the ones it does."""
    accepted = ", ".join(sorted(option.name for option in spec.options))
    return f"The {spec.label!r} command does not accept {name!r}. It accepts: {accepted}."


def _oversized(tokens: Sequence[str]) -> str | None:
    """Return the refusal for an argument longer than this tool will carry, or None."""
    for position, token in enumerate(tokens):
        if len(token) > MAX_ARGUMENT_LENGTH:
            return (
                f"Argument {position + 1} is {len(token)} characters long, and this tool accepts "
                f"at most {MAX_ARGUMENT_LENGTH}."
            )
    return None


def _usage_failure(reason: str) -> ParseOutcome:
    """Return the outcome for a command line that could not be read."""
    return ParseOutcome(
        message=f"{reason}\n\n{_usage_summary()}",
        exit_code=USAGE,
        to_stderr=True,
    )


def _usage_summary() -> str:
    """Return the short usage block shown with every refusal."""
    lines = [f"usage: {PROGRAM_NAME} <command> [arguments] [options]", "", "commands:"]
    for spec in COMMANDS:
        lines.append(f"  {spec.label} {spec.shape}".rstrip())
    lines.append("")
    lines.append(f"Run '{PROGRAM_NAME} --help' for the full description.")
    return "\n".join(lines)


def _version_text() -> str:
    """Return the version banner, reading the version the package itself declares."""
    return f"{PROGRAM_NAME} {__version__}"


def command_help(spec: CommandSpec) -> str:
    """Return the help text for one command, rendered from its specification."""
    lines = [f"usage: {PROGRAM_NAME} {spec.label} {spec.shape} [options]".rstrip(), "", spec.summary]
    if spec.details:
        lines.append("")
        lines.extend(spec.details)
    if spec.options:
        lines.append("")
        lines.append("options:")
        width = max(len(option.usage) for option in spec.options)
        for option in sorted(spec.options, key=lambda item: item.name):
            usage = option.usage.ljust(width)
            summary = option.summary
            if option.choices:
                summary = f"{summary} One of: {', '.join(option.choices)}."
            lines.append(f"  {usage}  {summary}")
    return "\n".join(lines)


def help_text() -> str:
    """Return the full help of the tool: every command, and what every exit code means."""
    lines = [
        f"usage: {PROGRAM_NAME} <command> [arguments] [options]",
        "",
        "Okto Grafx operator command line: open a database, see what state it is in, verify it,",
        "and read the evidence it preserved when something went wrong.",
        "",
        "commands:",
    ]
    width = max(len(f"{spec.label} {spec.shape}".rstrip()) for spec in COMMANDS)
    for spec in COMMANDS:
        shape = f"{spec.label} {spec.shape}".rstrip()
        lines.append(f"  {shape.ljust(width)}  {spec.summary}")
    lines.extend(
        [
            "",
            "exit codes:",
        ]
    )
    for code in sorted(EXIT_CODE_MEANINGS):
        lines.append(f"  {str(code).rjust(3)}  {EXIT_CODE_MEANINGS[code]}")
    lines.extend(
        [
            "",
            "Exit 1 means this command found evidence that the database is not clean. Commands",
            "that answer a question about one named object exit 0 once they have answered it.",
            "",
            f"Run '{PROGRAM_NAME} <command> --help' for the options of one command.",
        ]
    )
    return "\n".join(lines)
