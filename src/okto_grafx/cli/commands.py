"""What each ``oktografx`` command does (C12, operator surface).

Every command in this module is a function of one :class:`~okto_grafx.cli.parser.Invocation` to
one :class:`Report`, and a report carries three things at once: the exit code a script reads, the
machine-readable document ``--json`` prints, and the lines a person reads. They are built
together, from the same decision, precisely so they cannot disagree -- a report that printed
"clean" while returning the exit code for damage would be the worst defect this component could
ship, and the only durable defence against it is to leave no way to write the two separately.

Three rules run through all of them.

**Nothing is created by accident.** A path that does not already hold a database is refused
rather than opened, unless ``--create`` says otherwise. An operator reaching for this tool
because something is wrong must not be answered by a brand-new, perfectly clean, empty database
at the path they mistyped.

**A refusal is an answer, printed as the engine wrote it.** Whatever this build cannot do yet
refuses with a typed error naming its reason, and a command line whose job is to make the engine
legible prints that refusal as what it is -- class, code, retryability, details -- instead of
hiding it behind a generic failure or rewording it. Where the refusal is a caller who reached for
the wrong door, a hint is added BESIDE it, never in place of it: CONTRACT.md section 10 gives
``db.execute`` the autocommit READ and ``db.begin("write")`` the write, and only this layer knows
which one the operator meant.

**A file this tool writes is confirmed independently of the write.** LESSONS L5: an operation can
report success, write the right bytes and use the wrong name, and a check that reads back through
the handle the operation returned cannot see it. So an export lists the directory it wrote into
and re-opens the file by a freshly built path before it reports success.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from okto_grafx import Database, connect
from okto_grafx.cli.exits import (
    FINDINGS,
    INCONCLUSIVE,
    INTERNAL,
    OK,
    exit_code_for,
    is_inconclusive,
    is_retryable,
)
from okto_grafx.cli.output import describe, render_table
from okto_grafx.cli.parser import CONNECTION_OPTIONS, Invocation
from okto_grafx.domain import errors as grafx_errors
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxError,
    GrafxTransactionStateError,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.recovery.report import OUTCOME_CLEAN
from okto_grafx.engine.database import META_FILE
from okto_grafx.runtime.bootstrap import (
    QuarantineInventoryReader,
    open_quarantine_inventory,
)
from okto_grafx.runtime.config import MEMORY_PATH, DatabaseConfig
from okto_grafx.runtime.control_migration import downgrade_control_format

__all__ = [
    "MAX_PREVIEW_BYTES",
    "VERDICT_CLEAN",
    "VERDICT_CODES",
    "VERDICT_DAMAGED",
    "VERDICT_INCONCLUSIVE",
    "Report",
    "recovery_is_clean",
    "run",
    "status_code",
    "status_reasons",
    "verification_verdict",
]

VERDICT_CLEAN: str = "clean"
"""The walk ran and found nothing."""

VERDICT_DAMAGED: str = "damaged"
"""The walk found something wrong, and every finding is located in the report."""

VERDICT_INCONCLUSIVE: str = "inconclusive"
"""The walk ran and examined nothing, so it certifies nothing. Never read this as clean."""

VERDICT_CODES: Mapping[str, int] = {
    VERDICT_CLEAN: OK,
    VERDICT_DAMAGED: FINDINGS,
    VERDICT_INCONCLUSIVE: INCONCLUSIVE,
}
"""The exit code each verdict reports.

One mapping, so the word printed for a verdict and the number returned for it are the same
decision read twice rather than two decisions that have to be kept in step.
"""

MAX_PREVIEW_BYTES: int = 64
"""How many preserved bytes a text report shows inline before it stops.

Evidence is read with ``--output``, which writes the whole range to a file. The inline preview is
there to let an operator recognise what they are looking at, not to carry it.
"""

_QUARANTINE_INVENTORY_STATES: tuple[str, ...] = (
    "complete",
    "incomplete",
    "corrupt_manifest",
    "unreadable_manifest",
    "missing_payload",
    "manifest_mismatch",
    "unexpected_layout",
)
"""Every state the conclusive public quarantine inventory can contain."""

_DECLARED_GRAFX_ERROR_TYPES: tuple[type[GrafxError], ...] = tuple(
    error_type
    for name in grafx_errors.__all__
    if isinstance((error_type := getattr(grafx_errors, name)), type)
    and issubclass(error_type, GrafxError)
)
"""Engine-owned failures whose plain message field is safe to copy during cleanup."""

_DetachedGrafxOutcome = tuple[str, str, str, bool, dict[str, object]]


def _initialize_detached_grafx_failure(
    failure: GrafxError, outcome: _DetachedGrafxOutcome
) -> None:
    """Install only canonical builtins without calling a possibly patched Grafx constructor."""
    error_type, code, message, retryable, details = outcome
    Exception.__init__(failure, message)
    failure.message = message
    failure.code = code
    failure.retryable = retryable
    failure.details = dict(details)
    failure.error_type = error_type  # type: ignore[attr-defined]
    failure.inconclusive = details.get("inconclusive") is True  # type: ignore[attr-defined]


class _DetachedCliGrafxFailure(GrafxError):
    """A trusted copy of one exact declared failure, with no source capability attached."""

    def __init__(self, outcome: _DetachedGrafxOutcome) -> None:
        _initialize_detached_grafx_failure(self, outcome)


class _DetachedCliCorruptionFailure(GrafxCorruptionDetected):
    """Detached damage that retains the taxonomy's isinstance-based precedence."""

    def __init__(self, outcome: _DetachedGrafxOutcome) -> None:
        _initialize_detached_grafx_failure(self, outcome)


class _DetachedCliTransactionStateFailure(GrafxTransactionStateError):
    """Detached transaction refusal that retains the CLI's write-door hint routing."""

    def __init__(self, outcome: _DetachedGrafxOutcome) -> None:
        _initialize_detached_grafx_failure(self, outcome)

_CONNECT_KEYS: tuple[str, ...] = tuple(
    option.key
    for option in CONNECTION_OPTIONS
    if option.key not in {"create", "read_only"}
)
"""Connection options that pass straight through to ``connect`` under the same name.

Derived from the option table rather than listed a second time, so a new connection option is
wired by adding it in one place (A24).
"""


@dataclass(frozen=True, slots=True)
class Report:
    """One command's answer: the exit code, the machine-readable document and the text form."""

    exit_code: int
    payload: Mapping[str, object] = field(default_factory=dict)
    lines: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()


def verification_verdict(finding_count: int, examined: int) -> str:
    """Return ``"damaged"``, ``"clean"`` or ``"inconclusive"`` for one verification walk.

    Three states, not two, and the third is the one this function exists for. A walk that
    examined nothing produces an empty finding list, exactly as a clean walk does, so a rule that
    reads only the findings certifies a database it never looked at. ``examined`` is the total of
    everything the walk counted; ``clean`` requires both halves.
    """
    if finding_count:
        return VERDICT_DAMAGED
    if examined:
        return VERDICT_CLEAN
    return VERDICT_INCONCLUSIVE


def status_reasons(
    *,
    recovery: object,
    ledger_total: int,
    quarantined: int,
    damaged_ledger_tail: bool,
) -> list[str]:
    """Return every reason this database is not clean, in the order an operator should read them.

    An empty list is the only thing that makes a database clean, so each condition here is a
    separate statement rather than a term folded into one boolean: an operator who is told a
    database is not clean is owed the whole list, not the first item of it.
    """
    reasons: list[str] = []
    if recovery is None:
        reasons.append("no recovery ran, because this open was read-only")
    elif getattr(recovery, "outcome", "") != OUTCOME_CLEAN:
        reasons.append(f"the recovery at open reported {getattr(recovery, 'outcome', '')!r}")
    discarded = int(getattr(recovery, "records_discarded", 0) or 0)
    if discarded:
        reasons.append(f"{discarded} record(s) were discarded")
    if ledger_total:
        reasons.append(f"the ledger holds {ledger_total} entry(ies) of unapplied work")
    if quarantined:
        reasons.append(
            f"quarantine holds {quarantined} physical inventory item(s) of preserved evidence"
        )
    if damaged_ledger_tail:
        reasons.append("the ledger itself has an unreadable tail")
    return reasons


def status_code(reasons: Sequence[str], *, recovered: bool, evidence: bool) -> int:
    """Return the exit code for a status report.

    A read-only open replays nothing, so it knows nothing about the log. When that is the ONLY
    thing wrong, the honest answer is that this run certified nothing -- not that the database is
    damaged. Preserved evidence is different: it is a fact about the database that a read-only
    open can see perfectly well, so it still reports as not clean.
    """
    if not reasons:
        return OK
    if not recovered and not evidence:
        return INCONCLUSIVE
    return FINDINGS


def recovery_is_clean(report: object) -> bool:
    """Return whether one recovery pass discarded nothing and had nothing to repair.

    Both terms are asserted. The outcome word alone would be enough while the component that
    produces it keeps its own invariant that a discard forces a non-clean word -- and a command
    line that certifies a database on another component's invariant is certifying something it
    cannot see (LESSONS L12).
    """
    if getattr(report, "outcome", "") != OUTCOME_CLEAN:
        return False
    return int(getattr(report, "records_discarded", 0) or 0) == 0


def run(invocation: Invocation) -> Report:
    """Run one parsed command and return its report. Never raises a Grafx failure at a caller."""
    handler = _HANDLERS.get(invocation.spec.label)
    if handler is None:
        # Unreachable through the parser, which only ever produces a label from the same table.
        # Answered rather than asserted, because a command that cannot run must still report.
        return _refusal(
            invocation,
            GrafxUnsupportedOperation(
                f"The command {invocation.spec.label!r} has no implementation in this build.",
                command=invocation.spec.label,
            ),
        )
    try:
        return handler(invocation)
    except GrafxError as failure:
        detached = _detach_declared_grafx_failure(failure)
        if detached is None:
            return _foreign_grafx_report(invocation)
        return _refusal(invocation, detached)


# --- opening ----------------------------------------------------------------------------------


def _connect_options(invocation: Invocation) -> dict[str, object]:
    """Return the keyword arguments this invocation asks ``connect`` for."""
    options: dict[str, object] = {}
    for key in _CONNECT_KEYS:
        value = invocation.options.get(key)
        if value is not None:
            options[key] = value
    if invocation.flag("read_only") or invocation.spec.force_read_only:
        options["read_only"] = True
    return options


def _require_database(invocation: Invocation) -> None:
    """Refuse a path that does not already hold a database, unless creating one was asked for."""
    path = invocation.path
    if path == MEMORY_PATH or invocation.flag("create"):
        return
    try:
        present = os.path.isfile(os.path.join(path, META_FILE))
    except (OSError, ValueError) as failure:
        # A path the operating system cannot even examine -- a null character, a name longer than
        # the platform allows -- is a command-line mistake, not a database state. Naming it here
        # keeps a raw ValueError from travelling any further (CONTRACT.md section 11 item 5).
        raise GrafxConfigurationError(
            f"The path {_quoted(path)} could not be examined: {type(failure).__name__}.",
            field="path",
            value=path,
        ) from failure
    if not present:
        raise GrafxConfigurationError(
            f"There is no Okto Grafx database at {_quoted(path)}: no {META_FILE!r} "
            "was found there. Nothing was created. Pass --create to make one.",
            field="path",
            value=path,
        )


def _open(invocation: Invocation) -> Database:
    """Open the database this invocation names, refusing a path that does not hold one."""
    _require_database(invocation)
    return connect(invocation.path, **_connect_options(invocation))


def _on_database(invocation: Invocation, body: Callable[[Database], Report]) -> Report:
    """Open the database, run one command over it, and close it whatever happened.

    A failure inside the command wins over a failure while closing: replacing the reason the
    operator is being told about with a failure of the closing path hides the answer they asked
    for. A close that fails after a CLEAN run is reported and decides the exit code, because a
    database that could not release what it held is a state worth knowing about.

    A close that fails after a NOT-clean run does not decide it. Letting a release failure
    overwrite the code for damage would hide the damage behind the tidying-up, and a script
    branching on the damage code would stop seeing it.
    """
    database = _open(invocation)
    try:
        report = body(database)
    except GrafxError as failure:
        note = _close_quietly(database)
        detached = _detach_declared_grafx_failure(failure)
        if detached is None:
            return _foreign_grafx_report(invocation, close_problem=note)
        refusal = _refusal(invocation, detached)
        return (
            refusal
            if note is None
            else Report(
                exit_code=refusal.exit_code,
                payload={**refusal.payload, "close_problem": note},
                lines=refusal.lines,
                problems=(*refusal.problems, note),
            )
        )
    except BaseException:
        _close_quietly(database)
        raise
    try:
        database.close()
    except BaseException as failure:
        description = _close_failure_description(failure)
        note = f"The database could not be closed: {description}"
        if report.exit_code != OK:
            # A completed non-clean observation is the primary answer.  Cleanup remains visible,
            # but must not inject a contradictory refusal envelope or erase evidence -- even when
            # cleanup raised process-control rather than an ordinary Exception.
            return Report(
                exit_code=report.exit_code,
                payload={**report.payload, "close_problem": description},
                lines=report.lines,
                problems=(*report.problems, note),
            )
        if isinstance(failure, GrafxError):
            detached = _detach_declared_grafx_failure(failure)
            if detached is None:
                return _foreign_grafx_report(invocation)
            closing = _refusal(invocation, detached)
            return Report(
                exit_code=closing.exit_code,
                payload={
                    **report.payload,
                    **closing.payload,
                    "close_problem": description,
                },
                lines=report.lines,
                problems=(*report.problems, *closing.problems),
            )
        raise
    return report


def _close_quietly(database: Database) -> str | None:
    """Close a database while another failure is being reported, and describe any second failure.

    Nothing is raised from here. The failure already in flight is the one the operator needs, and
    a close that fails on the way out must not replace it.
    """
    try:
        database.close()
    except BaseException as failure:
        return f"The database could not be closed: {_close_failure_description(failure)}"
    return None


def _close_failure_description(failure: BaseException) -> str:
    """Copy a trusted Grafx message without dispatching code on a foreign cleanup failure."""
    if isinstance(failure, GrafxError):
        detached = _detach_declared_grafx_failure(failure)
        if detached is not None:
            return detached.message
    return "a secondary close failure"


def _detach_declared_grafx_failure(failure: GrafxError) -> GrafxError | None:
    """Return a capability-free copy only for an exact engine-declared error class."""
    failure_type = type(failure)
    if not any(failure_type is declared for declared in _DECLARED_GRAFX_ERROR_TYPES):
        return None
    try:
        captured_message = object.__getattribute__(failure, "message")
    except BaseException:
        captured_message = None
    message = (
        captured_message
        if type(captured_message) is str
        else "A Grafx failure had no safe text message."
    )
    try:
        captured_retryable = object.__getattribute__(failure, "retryable")
    except BaseException:
        captured_retryable = False
    retryable = captured_retryable is True
    try:
        captured_details = object.__getattribute__(failure, "details")
    except BaseException:
        captured_details = None
    details = _detached_error_details(captured_details)
    try:
        captured_inconclusive = object.__getattribute__(failure, "inconclusive")
    except BaseException:
        captured_inconclusive = False
    if captured_inconclusive is True:
        details["inconclusive"] = True
    error_type = failure_type.__name__
    code = failure_type.code
    outcome: _DetachedGrafxOutcome = (
        error_type if type(error_type) is str else "GrafxError",
        code if type(code) is str else "grafx_error",
        message,
        retryable,
        details,
    )
    if failure_type is GrafxCorruptionDetected:
        return _DetachedCliCorruptionFailure(outcome)
    if failure_type is GrafxTransactionStateError:
        return _DetachedCliTransactionStateFailure(outcome)
    return _DetachedCliGrafxFailure(outcome)


def _detached_error_details(value: object, *, depth: int = 0) -> dict[str, object]:
    """Copy a bounded exact-dict error envelope without invoking stored collaborators."""
    if type(value) is not dict or depth >= 8:
        return {}
    copied: dict[str, object] = {}
    for position, (key, item) in enumerate(dict.items(value)):
        if position >= 64:
            break
        if type(key) is str and key != "retryable":
            copied[key] = _detached_error_detail(item, depth=depth + 1)
    return copied


def _detached_error_detail(value: object, *, depth: int) -> object:
    """Return a JSON-safe builtin projection of one error detail without foreign dispatch."""
    if value is None or type(value) in {bool, int, float, str}:
        return value
    if depth >= 8:
        return "<detail depth exceeded>"
    if type(value) is list:
        return [
            _detached_error_detail(item, depth=depth + 1) for item in value[:64]
        ]
    if type(value) is tuple:
        return tuple(
            _detached_error_detail(item, depth=depth + 1) for item in value[:64]
        )
    if type(value) is dict:
        return _detached_error_details(value, depth=depth)
    return "<unsafe detail omitted>"


def _foreign_grafx_report(
    invocation: Invocation, *, close_problem: str | None = None
) -> Report:
    """Contain an undeclared Grafx subclass without reading or rendering the foreign object."""
    message = (
        "An undeclared Grafx error subclass crossed the CLI boundary and was contained without "
        "dispatching its attributes."
    )
    payload: dict[str, object] = {
        **_head(invocation),
        "error": {
            "type": "UndeclaredGrafxError",
            "code": "internal",
            "message": message,
            "retryable": False,
            "inconclusive": False,
            "details": {},
        },
    }
    problems = (message,)
    if close_problem is not None:
        payload["close_problem"] = close_problem
        problems = (*problems, close_problem)
    return Report(exit_code=INTERNAL, payload=payload, problems=problems)


# --- shared rendering ---------------------------------------------------------------------------


def _head(invocation: Invocation) -> dict[str, object]:
    """Return the payload keys every command reports."""
    return {"command": invocation.spec.label, "path": invocation.path}


WRITE_DOOR_HINT: str = (
    "  hint      a statement that writes needs a write transaction. Run this command with "
    "--write, which opens one with db.begin('write') and commits it; without it the statement "
    "runs in the autocommit read of CONTRACT.md section 10, which stages nothing."
)
"""What to add when a refusal looks like a caller who reached for the wrong door.

The refusal itself is never reworded, shortened or replaced: the engine's class, code, message
and details are printed exactly as they arrived, and this is an extra line underneath. A tool
whose job is to make this engine legible must not edit what the engine said -- but it can say
which door the caller wanted, because that is the one thing the engine cannot know.
"""


def _wanted_the_write_door(invocation: Invocation, failure: GrafxError) -> bool:
    """Return True when this refusal is a statement that writes, run without ``--write``.

    Keyed on the class and on the command line, never on the text of the message: a hint that
    reads the engine's prose would go quiet the first time that prose is improved. The autocommit
    path opens a fresh read transaction per statement, so a transaction-state refusal from it is
    always the read transaction declining to stage something.
    """
    if invocation.spec.label != "query" or invocation.flag("write"):
        return False
    return isinstance(failure, GrafxTransactionStateError)


def _refusal(invocation: Invocation, failure: GrafxError) -> Report:
    """Return the report for a typed refusal, with the taxonomy on show rather than flattened."""
    retryable = is_retryable(failure)
    inconclusive = is_inconclusive(failure)
    exit_code = exit_code_for(failure)
    error_type = _public_error_type(failure)
    payload = {
        **_head(invocation),
        "error": {
            "type": error_type,
            "code": getattr(failure, "code", "grafx_error"),
            "message": getattr(failure, "message", describe(failure)),
            "retryable": retryable,
            "inconclusive": inconclusive,
            "details": dict(getattr(failure, "details", {}) or {}),
        },
    }
    if retryable:
        advice = "may succeed if tried again"
    elif inconclusive:
        advice = "certified nothing; never read this as clean"
    else:
        advice = "will not succeed if tried again"
    problems = [
        f"{invocation.spec.label}: refused",
        f"  code      {getattr(failure, 'code', 'grafx_error')} ({advice})",
        f"  type      {error_type}",
        f"  message   {getattr(failure, 'message', describe(failure))}",
    ]
    details = getattr(failure, "details", None)
    if isinstance(details, Mapping) and details:
        for name in sorted(details, key=describe):
            problems.append(f"  detail    {describe(name)} = {describe(details[name])}")
    if _wanted_the_write_door(invocation, failure):
        problems.append(WRITE_DOOR_HINT)
        payload["hint"] = WRITE_DOOR_HINT.split("hint      ", 1)[-1]
    return Report(exit_code=exit_code, payload=payload, problems=tuple(problems))


def _public_error_type(failure: GrafxError) -> str:
    """Return a detached failure's original public type without trusting foreign attributes."""
    try:
        declared = getattr(failure, "error_type", None)
    except BaseException:
        declared = None
    if type(declared) is str:
        return declared
    try:
        name = type(failure).__name__
    except BaseException:
        return "GrafxError"
    return name if type(name) is str else "GrafxError"


def _field_lines(pairs: Sequence[tuple[str, object]]) -> tuple[str, ...]:
    """Return aligned ``name  value`` lines for a block of report fields."""
    if not pairs:
        return ()
    width = max(len(name) for name, _ in pairs)
    return tuple(f"{name.ljust(width)}  {describe(value)}" for name, value in pairs)


def _wall_time(stamp: object) -> str:
    """Return an ISO-8601 UTC rendering of a wall stamp, or a plain description when it is not one.

    A stamp read off a damaged page can be any number at all, and
    ``datetime.fromtimestamp`` raises for values outside the platform's range. An operator
    inspecting a damaged database is exactly who must not meet that.
    """
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        return describe(stamp)
    try:
        moment = datetime.datetime.fromtimestamp(float(stamp), datetime.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return f"{stamp!r} (not a readable time)"
    return moment.isoformat().replace("+00:00", "Z")


def _uuid_text(raw: object) -> str:
    """Return the canonical text of a database identity, or a description when it is not one."""
    if not isinstance(raw, (bytes, bytearray)):
        return describe(raw)
    try:
        return str(uuid.UUID(bytes=bytes(raw)))
    except (ValueError, TypeError):
        return "0x" + bytes(raw).hex()


def _preview(body: bytes) -> str:
    """Return a short hexadecimal preview of preserved bytes, marked when it was shortened."""
    head = bytes(body[:MAX_PREVIEW_BYTES])
    text = "0x" + head.hex() if head else "(empty)"
    return text if len(body) <= MAX_PREVIEW_BYTES else f"{text}... ({len(body)} bytes)"


def _quoted(text: str) -> str:
    """Return a path in quotes without escaping it.

    ``repr`` doubles every separator on Windows, so a refusal about
    ``C:\\Users\\me\\db`` reaches the operator spelled differently from the path they typed. The
    quotes still show a stray space at either end, which is the reason ``repr`` was reached for.
    """
    return f"'{text}'"


def _optional_component(database: Database, name: str) -> object | None:
    """Return one optional engine of a database, or None when the composition has none.

    Only ``unsupported_operation`` is treated as absence: it is the typed word the engine uses for
    a component that was never composed. Anything else is a real failure and travels on.
    """
    try:
        return getattr(database, name)
    except GrafxUnsupportedOperation:
        return None


# --- status ---------------------------------------------------------------------------------


def _schema(invocation: Invocation) -> Report:
    """Inspect a single detached catalog, without scanning graph records."""
    return _on_database(invocation, lambda database: _schema_body(invocation, database))


def _schema_body(invocation: Invocation, database: Database) -> Report:
    from okto_grafx.cli.discovery import dto

    catalog = database.catalog.catalog
    tables = catalog.tables()
    spaces = catalog.spaces()
    selected_spaces = spaces[:invocation.number("limit", 100)]
    selected = tables[:invocation.number("limit", 100)]
    definitions = [
        {
            "table_id": table.table_id, "name": table.name, "kind": table.kind,
            "schema_version": table.schema_version, "primary_key": table.primary_key,
            "from_table": table.from_table, "to_table": table.to_table,
            "columns": [
                {"name": column.name, "type": column.type.name,
                 "nullable": column.nullable, "vector_space": column.vector_space}
                for column in table.columns
            ],
        }
        for table in selected
    ]
    truncated = len(selected) < len(tables) or len(selected_spaces) < len(spaces)
    return Report(
        exit_code=OK,
        payload={**_head(invocation), "schema_version": 1, "read_only": True,
                 "tables": definitions, "total_tables": len(tables),
                 "returned_tables": len(selected), "truncated": truncated,
                 "spaces": dto(selected_spaces), "total_spaces": len(spaces),
                 "returned_spaces": len(selected_spaces)},
        lines=(f"tables {len(selected)}/{len(tables)}; truncated={str(truncated).lower()}",
               *(f"{table.table_id} {table.kind} {table.name}" for table in selected)),
    )


def _status(invocation: Invocation) -> Report:
    """Open the database and report its identity, its recovery and the evidence it holds."""
    return _on_database(invocation, lambda database: _status_body(invocation, database))


def _status_body(invocation: Invocation, database: Database) -> Report:
    """Build the status report of one open database."""
    identity = database.identity
    recovery = database.recovery_report
    ledger = _optional_component(database, "ledger")
    quarantine = _require_quarantine(database)

    depth = dict(ledger.depth()) if ledger is not None else {}
    ledger_total = sum(depth.values())
    inventory = quarantine.inventory()  # type: ignore[attr-defined]
    quarantine_states = {state: 0 for state in _QUARANTINE_INVENTORY_STATES}
    for item in inventory:
        quarantine_states[item.state] += 1
    quarantined = len(inventory)
    complete_entries = quarantine_states["complete"]
    damage = getattr(ledger, "damage", None) if ledger is not None else None

    reasons = status_reasons(
        recovery=recovery,
        ledger_total=ledger_total,
        quarantined=quarantined,
        damaged_ledger_tail=damage is not None,
    )
    code = status_code(
        reasons,
        recovered=recovery is not None,
        evidence=bool(ledger_total or quarantined or damage is not None),
    )

    payload = {
        **_head(invocation),
        "identity": {
            "database_uuid": _uuid_text(identity.database_uuid),
            "page_size": identity.page_size,
            "partitions_per_table": identity.partitions_per_table,
            "created_at_wall": identity.created_at_wall,
            "created_at": _wall_time(identity.created_at_wall),
            "granularity_descriptor": identity.granularity_descriptor,
            "format_version": identity.format_version,
        },
        "read_only": database.read_only,
        "recovery": _recovery_payload(recovery),
        "ledger": {"depth": depth, "total": ledger_total, "damaged_tail": damage is not None},
        "quarantine": {
            "entries": complete_entries,
            "inventory": {
                "conclusive": True,
                "count": quarantined,
                "states": quarantine_states,
            },
        },
        "metrics_endpoint": database.metrics_endpoint,
        "reasons": reasons,
    }
    lines = [
        f"database   {database.path}",
        *_field_lines(
            [
                ("uuid", _uuid_text(identity.database_uuid)),
                ("page size", identity.page_size),
                ("partitions", identity.partitions_per_table),
                ("created", _wall_time(identity.created_at_wall)),
                ("granularity", identity.granularity_descriptor),
                ("format", identity.format_version),
                ("read-only", "yes" if database.read_only else "no"),
                ("recovery", _recovery_summary(recovery)),
                ("ledger", _ledger_summary(depth, ledger_total, damage)),
                (
                    "quarantine",
                    f"{quarantined} physical item(s), {complete_entries} complete entry(ies)",
                ),
                (
                    "quarantine states",
                    ", ".join(
                        f"{state}={quarantine_states[state]}"
                        for state in _QUARANTINE_INVENTORY_STATES
                    ),
                ),
                ("metrics", database.metrics_endpoint or "no endpoint is exposed"),
            ]
        ),
    ]
    lines.append("")
    if reasons:
        lines.append("this database is NOT clean:")
        lines.extend(f"  - {reason}" for reason in reasons)
    else:
        lines.append("this database is clean.")
    return Report(exit_code=code, payload=payload, lines=tuple(lines))


def _recovery_summary(recovery: object) -> str:
    """Return the one-line form of a recovery report, or a statement that none ran."""
    if recovery is None:
        return "did not run (this open was read-only)"
    return (
        f"{getattr(recovery, 'outcome', '?')} "
        f"(replayed {getattr(recovery, 'records_replayed', 0)}, "
        f"discarded {getattr(recovery, 'records_discarded', 0)}, "
        f"ledger entries {getattr(recovery, 'ledger_entries_created', 0)}, "
        f"last good LSN {getattr(recovery, 'last_good_lsn', 0)})"
    )


def _ledger_summary(depth: Mapping[str, int], total: int, damage: object) -> str:
    """Return the one-line form of the ledger state."""
    parts = ", ".join(f"{name}={count}" for name, count in sorted(depth.items()))
    text = f"{total} entry(ies)" + (f" ({parts})" if parts else "")
    return text + (" -- the ledger file has an unreadable tail" if damage is not None else "")


def _recovery_payload(recovery: object) -> Mapping[str, object] | None:
    """Return the machine-readable form of a recovery report, or None when none ran."""
    if recovery is None:
        return None
    return {
        "outcome": getattr(recovery, "outcome", ""),
        "records_replayed": getattr(recovery, "records_replayed", 0),
        "records_discarded": getattr(recovery, "records_discarded", 0),
        "ledger_entries_created": getattr(recovery, "ledger_entries_created", 0),
        "last_good_lsn": getattr(recovery, "last_good_lsn", 0),
        "findings": [
            {
                "kind": finding.kind,
                "detail": finding.detail,
                "file": finding.file,
                "offset": finding.offset,
                "length": finding.length,
                "lsn": finding.lsn,
                "page": finding.page,
                "entry_id": finding.entry_id,
                "quarantine": finding.quarantine,
            }
            for finding in getattr(recovery, "findings", ())
        ],
    }


# --- verify ------------------------------------------------------------------------------------


def _verify(invocation: Invocation) -> Report:
    """Walk the database and report every finding with its location."""
    return _on_database(invocation, lambda database: _verify_body(invocation, database))


def _verify_body(invocation: Invocation, database: Database) -> Report:
    """Build the verification report of one open database."""
    scope = invocation.text("scope", "all")
    report = database.verify(scope)
    findings = tuple(getattr(report, "findings", ()))
    examined = (
        int(getattr(report, "pages_checked", 0))
        + int(getattr(report, "records_checked", 0))
        + int(getattr(report, "index_entries_checked", 0))
    )
    # One decision, read three ways below. There is deliberately no second place that can call a
    # database clean: the exit code, the machine-readable verdict and the printed word all come
    # from this word and from the one mapping that gives it a number.
    verdict = verification_verdict(len(findings), examined)
    code = VERDICT_CODES[verdict]

    located = [
        {
            "kind": finding.kind,
            "file": finding.location.file,
            "page": finding.location.page,
            "slot": finding.location.slot,
            "lsn": finding.location.lsn,
            "index": finding.location.index,
            "detail": finding.detail,
        }
        for finding in findings
    ]
    payload = {
        **_head(invocation),
        "scope": getattr(report, "scope", scope),
        "verdict": verdict,
        "clean": verdict == VERDICT_CLEAN,
        "pages_checked": getattr(report, "pages_checked", 0),
        "records_checked": getattr(report, "records_checked", 0),
        "index_entries_checked": getattr(report, "index_entries_checked", 0),
        "files_checked": list(getattr(report, "files_checked", ())),
        "findings": located,
        "recovery_ran": database.recovery_report is not None,
    }
    lines = [
        f"verification of {database.path}",
        *_field_lines(
            [
                ("scope", getattr(report, "scope", scope)),
                ("files", ", ".join(getattr(report, "files_checked", ())) or "none"),
                ("pages", f"{getattr(report, 'pages_checked', 0)} checked"),
                ("records", f"{getattr(report, 'records_checked', 0)} checked"),
                ("index entries", f"{getattr(report, 'index_entries_checked', 0)} checked"),
                ("verdict", _verdict_line(verdict, located)),
            ]
        ),
    ]
    if database.recovery_report is None:
        lines.append("")
        lines.append(
            "NOTE: this open was read-only, so recovery did not run and nothing was replayed."
        )
    if located:
        lines.append("")
        lines.append("findings:")
        lines.extend(
            render_table(
                ("#", "kind", "file", "page", "slot", "lsn", "index", "detail"),
                [
                    (
                        str(number),
                        finding["kind"],
                        describe(finding["file"]),
                        describe(finding["page"]),
                        describe(finding["slot"]),
                        describe(finding["lsn"]),
                        describe(finding["index"]),
                        describe(finding["detail"]),
                    )
                    for number, finding in enumerate(located, start=1)
                ],
            )
        )
    return Report(exit_code=code, payload=payload, lines=tuple(lines))


def _verdict_line(verdict: str, located: Sequence[Mapping[str, object]]) -> str:
    """Return the human sentence for a verification verdict."""
    if verdict == VERDICT_CLEAN:
        return "CLEAN -- the walk ran and found nothing"
    if verdict == VERDICT_INCONCLUSIVE:
        return "INCONCLUSIVE -- this walk examined nothing, so it certifies nothing"
    places = {
        (describe(item["file"]), describe(item["page"]), describe(item["slot"]))
        for item in located
    }
    return f"DAMAGED -- {len(located)} finding(s) at {len(places)} location(s)"


# --- query -------------------------------------------------------------------------------------


def _query(invocation: Invocation) -> Report:
    """Run one statement and print its result."""
    return _on_database(invocation, lambda database: _query_body(invocation, database))


def _query_body(invocation: Invocation, database: Database) -> Report:
    """Run every statement of one invocation, sharing one write transaction when asked.

    The two doors of CONTRACT.md section 10, spelled out:

    * ``--write`` opens ONE ``db.begin("write")`` and runs every statement inside it through
      ``txn.execute``. The block commits on a clean exit and rolls back on any refusal, so a
      sequence of statements is applied together or not at all. That is the whole reason several
      statements are accepted: a schema change and the rows that depend on it belong in one
      transaction, and a command line that could only run them separately could not say so.
    * without it, each statement is its own autocommit READ through ``db.execute``.

    Autocommit running a read transaction is not a promise that a statement changes nothing: what
    a refused statement leaves behind belongs to the engine that refused it, and this layer
    neither adds to nor conceals that.
    """
    statements = tuple(invocation.positionals[1:])
    parameters = _parameters(invocation)
    results: list[object] = []
    if invocation.flag("write"):
        with database.transaction("write") as txn:
            for statement in statements:
                results.append(txn.execute(statement, parameters))
    else:
        for statement in statements:
            results.append(database.execute(statement, parameters))
    return _query_report(invocation, database, statements, tuple(results))


def _parameters(invocation: Invocation) -> dict[str, object]:
    """Return the named parameters of an invocation, refusing a spelling that names nothing."""
    parameters: dict[str, object] = {}
    for raw in invocation.repeated("parameter"):
        name, separator, value = raw.partition("=")
        if not separator or not name:
            raise GrafxConfigurationError(
                f"A parameter is spelled NAME=VALUE; got {raw!r}.",
                field="parameter",
                value=raw,
            )
        if name in parameters:
            raise GrafxConfigurationError(
                f"The parameter {name!r} was given more than once.",
                field="parameter",
                value=name,
            )
        parameters[name] = _parameter_value(value)
    return parameters


def _parameter_value(raw: str) -> object:
    """Return a parameter value: the JSON it spells, or the text itself when it spells none.

    ``NaN`` and the two infinities are refused rather than parsed. They are legal to Python's JSON
    reader and are not values a caller means to type, and a non-finite number entering a query
    from a command line is the kind of value that becomes persistable further down (LESSONS L14).
    """

    def _refuse(literal: str) -> object:
        raise GrafxConfigurationError(
            f"A parameter value may not be {literal}.", field="parameter", value=literal
        )

    try:
        return json.loads(raw, parse_constant=_refuse)
    except GrafxConfigurationError:
        raise
    except (ValueError, RecursionError):
        return raw


def _one_result(result: object) -> dict[str, object]:
    """Return the machine-readable form of one statement result."""
    columns = tuple(getattr(result, "columns", ()) or ())
    rows = tuple(getattr(result, "rows", ()) or ())
    return {
        "columns": list(columns),
        "row_count": len(rows),
        "rows": [list(row) for row in rows],
        "statistics": dict(getattr(result, "statistics", {}) or {}),
    }


def _query_report(
    invocation: Invocation,
    database: Database,
    statements: Sequence[str],
    results: Sequence[object],
) -> Report:
    """Build the report of every statement that ran.

    The top-level ``columns``, ``rows``, ``row_count`` and ``statistics`` describe the LAST
    statement, because that is the answer the command produced -- a schema change followed by a
    read answers with the read. They are taken from ``results`` rather than computed a second
    time, so the summary and the detail cannot disagree (A24).
    """
    reported = [_one_result(result) for result in results]
    last = reported[-1] if reported else _one_result(None)
    limit = invocation.number("limit", 0)

    payload = {
        **_head(invocation),
        "statements": list(statements),
        "statement": statements[-1] if statements else "",
        "write": invocation.flag("write"),
        "results": reported,
        "columns": last["columns"],
        "row_count": last["row_count"],
        "rows": last["rows"],
        "statistics": last["statistics"],
    }

    lines: list[str] = []
    if invocation.flag("write") and len(statements) > 1:
        lines.append(
            f"{len(statements)} statements committed together in one write transaction"
        )
        lines.append("")
    for position, statement in enumerate(statements):
        body = reported[position]
        if position:
            lines.append("")
        lines.append(f"statement  {statement}")
        lines.append(f"columns    {', '.join(body['columns']) or 'none'}")
        lines.append(f"rows       {body['row_count']}")
        statistics = body["statistics"]
        if statistics:
            rendered = " ".join(f"{name}={value}" for name, value in sorted(statistics.items()))
            lines.append(f"statistics {rendered}")
        rows = tuple(results[position].rows if hasattr(results[position], "rows") else ())
        shown = rows[:limit] if limit > 0 else rows
        if body["columns"] and shown:
            lines.append("")
            lines.extend(
                render_table(
                    body["columns"], [[describe(value) for value in row] for row in shown]
                )
            )
            if len(shown) < len(rows):
                lines.append(
                    f"({len(rows) - len(shown)} more row(s) not shown; --limit is {limit})"
                )
        elif not rows:
            lines.append("")
            lines.append("(no rows)")
    if database.read_only:
        lines.append("")
        lines.append("NOTE: this open was read-only, so recovery did not run.")
    return Report(exit_code=OK, payload=payload, lines=tuple(lines))


# --- recovery ---------------------------------------------------------------------------------


def _recovery(invocation: Invocation) -> Report:
    """Report what recovery did, either at this open or on a fresh pass."""
    return _on_database(invocation, lambda database: _recovery_body(invocation, database))


def _recovery_body(invocation: Invocation, database: Database) -> Report:
    """Build the recovery report of one open database."""
    rerun = invocation.flag("rerun")
    report = database.recover() if rerun else database.recovery_report
    source = "a pass run by this command" if rerun else "the open of this command"
    payload = {
        **_head(invocation),
        "source": source,
        "recovery": _recovery_payload(report),
    }
    if report is None:
        lines = (
            f"recovery of {database.path}",
            "",
            "No recovery has run on this database in this process, because the open was",
            "read-only. Nothing was replayed, and this command certifies nothing about the log.",
        )
        return Report(exit_code=INCONCLUSIVE, payload=payload, lines=lines)

    outcome = getattr(report, "outcome", "")
    discarded = int(getattr(report, "records_discarded", 0) or 0)
    clean = recovery_is_clean(report)
    lines = [
        f"recovery of {database.path}",
        *_field_lines(
            [
                ("source", source),
                ("outcome", outcome),
                ("replayed", getattr(report, "records_replayed", 0)),
                ("discarded", discarded),
                ("ledger entries", getattr(report, "ledger_entries_created", 0)),
                ("last good LSN", getattr(report, "last_good_lsn", 0)),
            ]
        ),
    ]
    findings = tuple(getattr(report, "findings", ()))
    if findings:
        lines.append("")
        lines.append("findings:")
        lines.extend(
            render_table(
                ("#", "kind", "file", "offset", "length", "lsn", "page", "quarantine", "detail"),
                [
                    (
                        str(number),
                        finding.kind,
                        describe(finding.file),
                        describe(finding.offset),
                        describe(finding.length),
                        describe(finding.lsn),
                        describe(finding.page),
                        describe(finding.quarantine),
                        describe(finding.detail),
                    )
                    for number, finding in enumerate(findings, start=1)
                ],
            )
        )
    lines.append("")
    lines.append(
        "this recovery was clean."
        if clean
        else f"this recovery was NOT clean: outcome {outcome!r}, {discarded} record(s) discarded."
    )
    return Report(exit_code=OK if clean else FINDINGS, payload=payload, lines=tuple(lines))


# --- ledger ------------------------------------------------------------------------------------


def _require_ledger(database: Database) -> object:
    """Return the ledger of a database, or refuse naming the component that is absent."""
    ledger = _optional_component(database, "ledger")
    if ledger is None:
        raise GrafxUnsupportedOperation(
            "This database was composed without a ledger, so there is no unapplied work to read.",
            component="ledger",
            path=database.path,
        )
    return ledger


def _ledger_list(invocation: Invocation) -> Report:
    """List the entries of the unapplied-work ledger."""
    return _on_database(invocation, lambda database: _ledger_list_body(invocation, database))


def _ledger_list_body(invocation: Invocation, database: Database) -> Report:
    """Build the ledger listing of one open database."""
    ledger = _require_ledger(database)
    selection: dict[str, object] = {}
    origin_class = invocation.text("origin_class")
    reason = invocation.text("reason")
    if origin_class:
        selection["origin_class"] = origin_class
    if reason:
        selection["reason"] = reason
    limit = invocation.number("limit", 0)
    if limit > 0:
        selection["limit"] = limit
    offset = invocation.number("offset", 0)
    if offset > 0:
        selection["offset"] = offset
    entries = tuple(ledger.list(**selection))
    depth = dict(ledger.depth())
    damage = ledger.damage

    payload = {
        **_head(invocation),
        "depth": depth,
        "total": sum(depth.values()),
        "shown": len(entries),
        "damaged_tail": _damage_payload(damage),
        "entries": [_entry_payload(entry) for entry in entries],
    }
    lines = [
        f"ledger of {database.path}",
        *_field_lines(
            [
                ("file", getattr(ledger, "file", "")),
                ("depth", ", ".join(f"{k}={v}" for k, v in sorted(depth.items())) or "none"),
                ("shown", len(entries)),
            ]
        ),
    ]
    if damage is not None:
        lines.append("")
        lines.append(
            f"WARNING: the ledger file has an unreadable tail at offset "
            f"{getattr(damage, 'offset', 0)} covering {getattr(damage, 'length', 0)} byte(s): "
            f"{getattr(damage, 'detail', '')}"
        )
    if entries:
        lines.append("")
        lines.extend(
            render_table(
                ("id", "class", "reason", "type", "lsn range", "epoch", "captured", "bytes"),
                [
                    (
                        describe(entry.entry_id),
                        entry.origin_class.name.lower(),
                        entry.reason.name.lower(),
                        entry.entry_type.name.lower(),
                        f"{entry.lsn_start}-{entry.lsn_end}",
                        describe(entry.epoch),
                        _wall_time(entry.captured_at_wall),
                        describe(len(entry.payload)),
                    )
                    for entry in entries
                ],
            )
        )
    else:
        lines.append("")
        lines.append("(no entries match)")
    holds_evidence = bool(entries) or damage is not None
    return Report(
        exit_code=FINDINGS if holds_evidence else OK, payload=payload, lines=tuple(lines)
    )


def _damage_payload(damage: object) -> Mapping[str, object] | None:
    """Return the machine-readable form of a damaged ledger tail, or None when it reads clean."""
    if damage is None:
        return None
    return {
        "offset": getattr(damage, "offset", 0),
        "length": getattr(damage, "length", 0),
        "detail": getattr(damage, "detail", ""),
    }


def _entry_payload(entry: object) -> Mapping[str, object]:
    """Return the machine-readable form of one ledger entry."""
    return {
        "entry_id": entry.entry_id,
        "origin_class": entry.origin_class.name.lower(),
        "reason": entry.reason.name.lower(),
        "entry_type": entry.entry_type.name.lower(),
        "lsn_start": entry.lsn_start,
        "lsn_end": entry.lsn_end,
        "epoch": entry.epoch,
        "captured_at_wall": entry.captured_at_wall,
        "captured_at": _wall_time(entry.captured_at_wall),
        "payload_bytes": len(entry.payload),
        "digest": entry.digest.hex(),
        "reapplicable": entry.reapplicable,
    }


def _ledger_inspect(invocation: Invocation) -> Report:
    """Show one ledger entry in full."""
    return _on_database(invocation, lambda database: _ledger_inspect_body(invocation, database))


def _ledger_inspect_body(invocation: Invocation, database: Database) -> Report:
    """Build the report for one named ledger entry."""
    ledger = _require_ledger(database)
    entry_id = int(invocation.positionals[1])
    entry = ledger.inspect(entry_id)
    provenance = ledger.provenance(entry_id)
    payload = {
        **_head(invocation),
        "entry": _entry_payload(entry),
        "provenance": {
            "origin": provenance.origin,
            "offset": provenance.offset,
            "length": provenance.length,
            "expected_lsn": provenance.expected_lsn,
            "record_type": provenance.record_type,
            "failure": provenance.failure,
            "detail": provenance.detail,
            "quarantine": provenance.quarantine,
            "preserved_bytes": len(provenance.body),
        },
    }
    lines = [
        f"ledger entry {entry.entry_id} of {database.path}",
        *_field_lines(
            [
                ("class", entry.origin_class.name.lower()),
                ("reason", entry.reason.name.lower()),
                ("type", entry.entry_type.name.lower()),
                ("lsn range", f"{entry.lsn_start}-{entry.lsn_end}"),
                ("epoch", entry.epoch),
                ("captured", _wall_time(entry.captured_at_wall)),
                ("digest", entry.digest.hex()),
                ("reapplicable", "yes" if entry.reapplicable else "no"),
                ("origin", provenance.origin),
                ("origin offset", provenance.offset),
                ("origin length", provenance.length),
                ("expected LSN", provenance.expected_lsn),
                ("record type", provenance.record_type),
                ("failure", provenance.failure or "none recorded"),
                ("detail", provenance.detail or "none recorded"),
                ("quarantine", provenance.quarantine or "none"),
                ("preserved", _preview(provenance.body)),
            ]
        ),
    ]
    if not entry.reapplicable:
        lines.append("")
        lines.append(
            "This entry is forensic: it preserves bytes that never decoded. Read them with "
            "'ledger export'; it cannot be reprocessed."
        )
    return Report(exit_code=OK, payload=payload, lines=tuple(lines))


def _ledger_export(invocation: Invocation) -> Report:
    """Write the preserved bytes of one forensic ledger entry to a file."""
    return _on_database(invocation, lambda database: _ledger_export_body(invocation, database))


def _ledger_export_body(invocation: Invocation, database: Database) -> Report:
    """Export one forensic ledger entry and confirm the file independently of the write."""
    ledger = _require_ledger(database)
    output = _require_output(invocation)
    entry_id = int(invocation.positionals[1])
    body = ledger.export(entry_id)
    written = _write_evidence(output, body)
    payload = {
        **_head(invocation),
        "entry_id": entry_id,
        "output": written["path"],
        "bytes": len(body),
        "digest": written["digest"],
        "confirmed_in_directory": written["confirmed"],
    }
    lines = [
        f"exported ledger entry {entry_id} of {database.path}",
        *_field_lines(
            [
                ("output", written["path"]),
                ("bytes", len(body)),
                ("sha256", written["digest"]),
                ("confirmed", written["confirmation"]),
                ("preview", _preview(body)),
            ]
        ),
    ]
    return Report(exit_code=OK, payload=payload, lines=tuple(lines))


# --- quarantine --------------------------------------------------------------------------------


def _require_quarantine(database: Database) -> object:
    """Return the quarantine of a database, or refuse naming the component that is absent."""
    store = _optional_component(database, "quarantine")
    if store is None:
        raise GrafxUnsupportedOperation(
            "This database was composed without quarantine, so there is no evidence to read.",
            component="quarantine",
            path=database.path,
        )
    return store


def _quarantine_list(invocation: Invocation) -> Report:
    """List the preserved evidence quarantine holds."""
    return _on_database(invocation, lambda database: _quarantine_list_body(invocation, database))


def _quarantine_list_body(invocation: Invocation, database: Database) -> Report:
    """Build the quarantine listing of one open database."""
    store = _require_quarantine(database)
    entries = tuple(store.list())
    payload = {
        **_head(invocation),
        "directory": getattr(store, "directory", ""),
        "entries": [_quarantine_payload(entry) for entry in entries],
        "count": len(entries),
    }
    lines = [
        f"quarantine of {database.path}",
        *_field_lines(
            [("directory", getattr(store, "directory", "")), ("entries", len(entries))]
        ),
    ]
    if entries:
        lines.append("")
        lines.extend(
            render_table(
                ("name", "origin", "offset", "length", "reason", "captured", "digest"),
                [
                    (
                        entry.name,
                        entry.manifest.origin,
                        describe(entry.manifest.offset),
                        describe(entry.manifest.length),
                        entry.manifest.reason,
                        _wall_time(entry.manifest.captured_at_wall),
                        entry.manifest.digest,
                    )
                    for entry in entries
                ],
            )
        )
    else:
        lines.append("")
        lines.append("(nothing is quarantined)")
    return Report(
        exit_code=FINDINGS if entries else OK, payload=payload, lines=tuple(lines)
    )


def _quarantine_inventory(invocation: Invocation) -> Report:
    """Inventory quarantine without composing the coordination or recovery machinery."""
    reader = _open_quarantine_inventory(invocation)
    try:
        report = _quarantine_inventory_body(invocation, reader)
    except GrafxError as failure:
        note = _close_quarantine_inventory_quietly(reader)
        refusal = _refusal(invocation, failure)
        return (
            refusal
            if note is None
            else Report(
                exit_code=refusal.exit_code,
                payload={**refusal.payload, "close_problem": note},
                lines=refusal.lines,
                problems=(*refusal.problems, note),
            )
        )
    except BaseException:
        _close_quarantine_inventory_quietly(reader)
        raise
    try:
        reader.close()
    except GrafxError as failure:
        closing = _refusal(invocation, failure)
        if report.exit_code != OK:
            note = (
                "The quarantine inventory reader could not close: "
                f"{failure.message}"
            )
            return Report(
                exit_code=report.exit_code,
                payload={**report.payload, "close_problem": failure.message},
                lines=report.lines,
                problems=(*report.problems, note),
            )
        return Report(
            exit_code=closing.exit_code,
            payload={
                **report.payload,
                **closing.payload,
                "close_problem": failure.message,
            },
            lines=report.lines,
            problems=(*report.problems, *closing.problems),
        )
    except Exception as failure:
        # A non-clean inventory is the operator's primary answer.  A foreign close defect is
        # still reported beside it, but cannot erase evidence by changing the exit code.  With a
        # clean result there is no stronger answer to preserve, so the CLI's outer containment
        # reports the unforeseen close failure as INTERNAL.
        if report.exit_code == OK:
            raise
        note = (
            "The quarantine inventory reader could not close: "
            f"{_describe_quarantine_cleanup_failure(failure)}"
        )
        return Report(
            exit_code=report.exit_code,
            payload={**report.payload, "close_problem": note},
            lines=report.lines,
            problems=(*report.problems, note),
        )
    return report


def _close_quarantine_inventory_quietly(
    reader: QuarantineInventoryReader,
) -> str | None:
    """Close during unwind without letting cleanup replace a primary failure."""
    try:
        reader.close()
    except BaseException as failure:
        return (
            "The quarantine inventory reader could not close: "
            f"{_describe_quarantine_cleanup_failure(failure)}"
        )
    return None


def _describe_quarantine_cleanup_failure(failure: BaseException) -> str:
    """Describe secondary cleanup without allowing hostile rendering to change the outcome."""
    try:
        return describe(failure)
    except BaseException:
        return "<an unprintable cleanup failure>"


def _open_quarantine_inventory(invocation: Invocation) -> QuarantineInventoryReader:
    """Build the dedicated zero-write reader for one existing filesystem database."""
    _require_database(invocation)
    options = _connect_options(invocation)
    options["read_only"] = True
    config = DatabaseConfig(path=invocation.path, **options)  # type: ignore[arg-type]
    return open_quarantine_inventory(config)


def _quarantine_inventory_body(invocation: Invocation, database: object) -> Report:
    """Build one conclusive report from the quarantine's full captured inventory."""
    store = _require_quarantine(database)
    items = tuple(store.inventory())
    payload = {
        **_head(invocation),
        "directory": getattr(store, "directory", ""),
        "conclusive": True,
        "items": [_quarantine_inventory_payload(item) for item in items],
        "count": len(items),
    }
    lines = [
        f"quarantine inventory of {database.path}",
        *_field_lines(
            [
                ("directory", getattr(store, "directory", "")),
                ("items", len(items)),
                ("conclusive", True),
            ]
        ),
    ]
    if items:
        lines.append("")
        lines.extend(
            render_table(
                ("state", "name", "files", "manifest", "payload", "detail"),
                [
                    (
                        item.state,
                        item.name,
                        str(len(item.files)),
                        item.manifest_file or "none",
                        item.payload_file or "none",
                        item.detail or "none",
                    )
                    for item in items
                ],
            )
        )
    else:
        lines.extend(("", "(the quarantine inventory is conclusively empty)"))
    return Report(
        exit_code=FINDINGS if items else OK, payload=payload, lines=tuple(lines)
    )


def _quarantine_inventory_payload(item: object) -> Mapping[str, object]:
    """Return every field of one conclusive inventory item in JSON-native form."""
    manifest = item.manifest
    manifest_payload = None
    if manifest is not None:
        manifest_payload = {
            "origin": manifest.origin,
            "offset": manifest.offset,
            "length": manifest.length,
            "reason": manifest.reason,
            "detail": manifest.detail,
            "captured_at_wall": manifest.captured_at_wall,
            "captured_at": _wall_time(manifest.captured_at_wall),
            "digest": manifest.digest,
            "payload_file": manifest.payload_file,
            "entry_name": manifest.entry_name,
            "expected_lsn": manifest.expected_lsn,
            "schema": manifest.schema,
        }
    return {
        "name": item.name,
        "state": item.state,
        "files": list(item.files),
        "manifest_file": item.manifest_file,
        "payload_file": item.payload_file,
        "manifest": manifest_payload,
        "detail": item.detail,
    }


def _quarantine_payload(entry: object) -> Mapping[str, object]:
    """Return the machine-readable form of one quarantine entry."""
    manifest = entry.manifest
    return {
        "name": entry.name,
        "directory": entry.directory,
        "payload_file": entry.payload_file,
        "manifest_file": entry.manifest_file,
        "origin": manifest.origin,
        "offset": manifest.offset,
        "length": manifest.length,
        "reason": manifest.reason,
        "detail": manifest.detail,
        "expected_lsn": manifest.expected_lsn,
        "captured_at_wall": manifest.captured_at_wall,
        "captured_at": _wall_time(manifest.captured_at_wall),
        "digest": manifest.digest,
    }


def _quarantine_inspect(invocation: Invocation) -> Report:
    """Show one quarantine entry and the manifest that describes it."""
    return _on_database(
        invocation, lambda database: _quarantine_inspect_body(invocation, database)
    )


def _quarantine_inspect_body(invocation: Invocation, database: Database) -> Report:
    """Build the report for one named quarantine entry."""
    store = _require_quarantine(database)
    entry = store.inspect(invocation.positionals[1])
    manifest = entry.manifest
    receipts = database.quarantine_receipts(entry.name)
    payload = {
        **_head(invocation),
        "entry": _quarantine_payload(entry),
        "restore_receipts": list(receipts),
    }
    lines = [
        f"quarantine entry {entry.name} of {database.path}",
        *_field_lines(
            [
                ("origin", manifest.origin),
                ("offset", manifest.offset),
                ("length", manifest.length),
                ("reason", manifest.reason),
                ("detail", manifest.detail or "none recorded"),
                ("expected LSN", manifest.expected_lsn),
                ("captured", _wall_time(manifest.captured_at_wall)),
                ("digest", manifest.digest),
                ("payload file", entry.payload_file),
                ("manifest file", entry.manifest_file),
                ("restores", ", ".join(receipts) or "none"),
            ]
        ),
    ]
    return Report(exit_code=OK, payload=payload, lines=tuple(lines))


def _quarantine_read(invocation: Invocation) -> Report:
    """Write the preserved bytes of one quarantine entry to a file."""
    return _on_database(invocation, lambda database: _quarantine_read_body(invocation, database))


def _quarantine_read_body(invocation: Invocation, database: Database) -> Report:
    """Read one quarantine entry and confirm the written file independently of the write."""
    output = _require_output(invocation)
    name = invocation.positionals[1]
    body = database.read_quarantine(name)
    written = _write_evidence(output, body)
    payload = {
        **_head(invocation),
        "entry": name,
        "output": written["path"],
        "bytes": len(body),
        "digest": written["digest"],
        "confirmed_in_directory": written["confirmed"],
    }
    lines = [
        f"read quarantine entry {name} of {database.path}",
        *_field_lines(
            [
                ("output", written["path"]),
                ("bytes", len(body)),
                ("sha256", written["digest"]),
                ("confirmed", written["confirmation"]),
                ("preview", _preview(body)),
            ]
        ),
    ]
    return Report(exit_code=OK, payload=payload, lines=tuple(lines))


# --- metrics -----------------------------------------------------------------------------------


def _metrics(invocation: Invocation) -> Report:
    """Report the metrics endpoint of a database and the current value of every metric."""
    return _on_database(invocation, lambda database: _metrics_body(invocation, database))


def _control_downgrade(invocation: Invocation) -> Report:
    """Run the explicitly offline v2-to-v1 control-envelope migration."""
    _require_database(invocation)
    if invocation.flag("read_only"):
        raise GrafxUnsupportedOperation(
            "The control downgrade cannot run with --read-only.", field="read_only"
        )
    result = downgrade_control_format(
        DatabaseConfig(path=invocation.path, **_connect_options(invocation))
    )
    payload = {
        **_head(invocation),
        "previous_format": result.previous_format,
        "current_format": result.current_format,
        "converted_files": list(result.converted_files),
        "already_current": result.already_current,
    }
    lines = (
        f"control downgrade of {invocation.path}",
        *tuple(
            _field_lines(
                [
                    ("format", f"{result.previous_format} -> {result.current_format}"),
                    ("converted files", len(result.converted_files)),
                    ("already format 1", result.already_current),
                ]
            )
        ),
    )
    return Report(exit_code=OK, payload=payload, lines=lines)


def _metrics_body(invocation: Invocation, database: Database) -> Report:
    """Build the metrics report of one open database."""
    snapshot = database.snapshot_metrics()
    series = sorted(snapshot.items())
    payload = {
        **_head(invocation),
        "endpoint": database.metrics_endpoint,
        "series_count": len(series),
        "series": {name: dict(body) for name, body in series},
    }
    lines = [
        f"metrics of {database.path}",
        *_field_lines(
            [
                ("endpoint", database.metrics_endpoint or "no endpoint is exposed"),
                ("series", len(series)),
            ]
        ),
    ]
    if not series:
        lines.append("")
        lines.append(
            "(this database emits through the no-op sink, which keeps no values; select "
            "--metrics openmetrics or --metrics json to see them)"
        )
    else:
        lines.append("")
        for name, body in series:
            lines.append(f"{name}  [{describe(body.get('kind', ''))}]")
            samples = body.get("samples", ())
            if not samples:
                lines.append("    (no samples)")
                continue
            for sample in samples:
                lines.append(f"    {_sample_text(sample)}")
    return Report(exit_code=OK, payload=payload, lines=tuple(lines))


def _sample_text(sample: object) -> str:
    """Return one metric sample as ``{label="value"} number``."""
    if isinstance(sample, Mapping):
        labels = sample.get("labels", {})
        value = sample.get("value", "")
        if isinstance(labels, Mapping) and labels:
            rendered = ",".join(
                f'{describe(name)}="{describe(labels[name])}"' for name in sorted(labels)
            )
            return f"{{{rendered}}} {describe(value)}"
        return describe(value)
    return describe(sample)


# --- writing evidence out ------------------------------------------------------------------------


def _require_output(invocation: Invocation) -> str:
    """Return the output path of an export, refusing an invocation that named none."""
    output = invocation.text("output")
    if not output:
        raise GrafxConfigurationError(
            "This command writes preserved bytes to a file, so --output FILE is required.",
            field="output",
            value=output,
        )
    return output


def _write_evidence(output: str, body: bytes) -> dict[str, object]:
    """Write preserved bytes to a file and confirm the file by a route the write did not choose.

    LESSONS L5: a rename or a create can report success, write the right bytes and use the wrong
    name, and a check that reads back through the handle the write returned follows the write's
    own answer and cannot see it. So the file is confirmed twice, both times from the namespace:
    the directory is listed and must contain the exact base name, and the file is re-opened by a
    path built again from its parts, whose bytes must digest to what was written.

    An existing file is never overwritten. Evidence an operator already exported is not this
    tool's to replace.
    """
    resolved = os.path.abspath(output)
    directory = os.path.dirname(resolved) or os.curdir
    base = os.path.basename(resolved)
    if not base:
        raise GrafxConfigurationError(
            f"The output path {_quoted(output)} names a directory rather than a file.",
            field="output",
            value=output,
        )
    expected = hashlib.sha256(body).hexdigest()
    try:
        handle = open(resolved, "xb")
    except FileExistsError as failure:
        raise GrafxConfigurationError(
            f"The file {_quoted(resolved)} already exists, and this command never overwrites "
            "preserved evidence. Choose another --output.",
            field="output",
            value=resolved,
        ) from failure
    except (OSError, ValueError) as failure:
        raise GrafxConfigurationError(
            f"The file {_quoted(resolved)} could not be created: {type(failure).__name__}.",
            field="output",
            value=resolved,
        ) from failure
    try:
        with handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
    except (OSError, ValueError) as failure:
        raise GrafxConfigurationError(
            f"The file {_quoted(resolved)} could not be written: {type(failure).__name__}.",
            field="output",
            value=resolved,
        ) from failure

    try:
        listed = base in os.listdir(directory)
    except (OSError, ValueError):
        listed = False
    try:
        with open(os.path.join(directory, base), "rb") as reader:
            observed = hashlib.sha256(reader.read()).hexdigest()
    except (OSError, ValueError) as failure:
        raise GrafxConfigurationError(
            f"The file {_quoted(resolved)} was written but could not be read back by name: "
            f"{type(failure).__name__}.",
            field="output",
            value=resolved,
        ) from failure
    if not listed or observed != expected:
        raise GrafxConfigurationError(
            f"The export of {len(body)} byte(s) reported success, but the name {_quoted(base)} is "
            f"{'present' if listed else 'ABSENT'} in {_quoted(directory)} and the bytes read "
            f"back by name digest to {observed}, not {expected}. Nothing here is trustworthy; "
            "do not treat this file as the evidence.",
            field="output",
            value=resolved,
        )
    return {
        "path": resolved,
        "digest": expected,
        "confirmed": True,
        "confirmation": f"listed in {directory} and re-read by name with a matching sha256",
    }


def _discovery(invocation: Invocation) -> Report:
    from okto_grafx.cli.discovery import capabilities, inspect_indexes, search, catalog_inventory, workspace_resolution

    if invocation.spec.name == "capabilities":
        return capabilities(invocation)
    if invocation.spec.name == "catalogs":
        return catalog_inventory(invocation)
    if invocation.spec.name == "workspace":
        return workspace_resolution(invocation)
    operation = inspect_indexes if invocation.spec.name == "indexes" else search
    return _on_database(invocation, lambda database: operation(invocation, database))


_HANDLERS: Mapping[str, Callable[[Invocation], Report]] = {
    "catalogs": _discovery,
    "workspace resolve": _discovery,
    "capabilities": _discovery,
    "indexes": _discovery,
    "search text": _discovery,
    "search vector": _discovery,
    "search hybrid": _discovery,
    "schema": _schema,
    "status": _status,
    "verify": _verify,
    "query": _query,
    "recovery": _recovery,
    "ledger list": _ledger_list,
    "ledger inspect": _ledger_inspect,
    "ledger export": _ledger_export,
    "quarantine list": _quarantine_list,
    "quarantine inventory": _quarantine_inventory,
    "quarantine inspect": _quarantine_inspect,
    "quarantine read": _quarantine_read,
    "metrics": _metrics,
    "control downgrade": _control_downgrade,
}
"""Every command label the parser can produce, mapped to the function that answers it."""
