"""The exit codes the Okto Grafx command line reports (C12, operator surface).

An operator reads prose; a script reads the exit code, and only the exit code is a contract.
The whole set is here, in one place, with one rule for deriving a code from a typed failure, so
a command can never invent a number and two commands can never disagree about what one means.

The set answers three questions a script has to be able to ask without parsing English:

* **Is this database clean?** :data:`OK` says yes, :data:`FINDINGS` says no, and
  :data:`INCONCLUSIVE` says the command ran but certified nothing -- which is deliberately NOT
  the same answer as clean. A walk that examined nothing has an empty finding list too, and
  reporting that as clean is how a verifier comes to certify a database it never looked at.
* **Could the command run at all?** :data:`USAGE` for a command line this tool could not read,
  :data:`REFUSED` for a typed refusal, :data:`DAMAGED` when damaged bytes stopped the work, and
  :data:`RETRY` when the refusal says so itself.
* **Did something unforeseen happen?** :data:`INTERNAL` and :data:`INTERRUPTED` exist so that
  neither a defect nor a Ctrl-C can ever reach the terminal as a Python traceback.

The mapping from a ``Grafx*`` failure to a code is a rule over the taxonomy of CONTRACT.md
section 2 rather than a table of class names: ``corruption_detected`` is reserved for damaged
bytes (A11-revised), so it earns its own code, and everything else is told apart by the
``retryable`` flag the taxonomy already makes every caller read. A table of names would need
editing every time the taxonomy grows; this rule does not.
"""

from __future__ import annotations

from collections.abc import Mapping

from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxError

__all__ = [
    "DAMAGED",
    "EXIT_CODE_MEANINGS",
    "FINDINGS",
    "INCONCLUSIVE",
    "INTERNAL",
    "INTERRUPTED",
    "OK",
    "REFUSED",
    "RESULT_WORDS",
    "RETRY",
    "USAGE",
    "exit_code_for",
    "is_inconclusive",
    "is_retryable",
    "result_word",
]

OK: int = 0
"""The command ran and what it examined is clean."""

FINDINGS: int = 1
"""The command ran and found evidence that this database is not clean."""

USAGE: int = 2
"""The command line could not be read. Nothing was opened and nothing was changed."""

REFUSED: int = 3
"""A typed refusal that is neither damaged bytes nor a condition worth retrying."""

DAMAGED: int = 4
"""Damaged bytes stopped the command: the failure carried ``corruption_detected``."""

RETRY: int = 5
"""A typed refusal whose own ``retryable`` flag says the same call may succeed later."""

INCONCLUSIVE: int = 6
"""The command ran but certified nothing. Never read this as clean."""

INTERNAL: int = 70
"""An unforeseen failure was contained here rather than shown as a traceback."""

INTERRUPTED: int = 130
"""The operator interrupted the command. 128 plus the interrupt signal, as a shell reports it."""

EXIT_CODE_MEANINGS: Mapping[int, str] = {
    OK: "the command ran and what it examined is clean",
    FINDINGS: "the command ran and this database is not clean",
    USAGE: "the command line could not be read; nothing was opened",
    REFUSED: "a typed refusal that is neither damage nor retryable",
    DAMAGED: "damaged bytes stopped the command",
    RETRY: "a typed refusal that says it may succeed on a retry",
    INCONCLUSIVE: "the command ran but certified nothing; not the same as clean",
    INTERNAL: "an unforeseen failure was contained; this is a defect worth reporting",
    INTERRUPTED: "the operator interrupted the command",
}
"""Every code this tool can report, with the one en-US sentence that says what it means.

The help text is rendered from this mapping, so a code cannot be documented as one thing and
returned as another (A24: one definition of the meaning of a number).
"""

RESULT_WORDS: Mapping[int, str] = {
    OK: "ok",
    FINDINGS: "findings",
    USAGE: "usage",
    REFUSED: "refused",
    DAMAGED: "damaged",
    RETRY: "retry",
    INCONCLUSIVE: "inconclusive",
    INTERNAL: "internal",
    INTERRUPTED: "interrupted",
}
"""The single word the machine-readable output reports for each code.

Derived from the code rather than chosen per command, so the word in the JSON document and the
number the shell sees can never say two different things about one run.
"""


def is_retryable(failure: GrafxError) -> bool:
    """Return whether this failure says the same call may succeed if it is tried again.

    The taxonomy of CONTRACT.md section 2 puts the flag on the class, and components additionally
    pass it through ``details`` on the paths where a caller inspects the dictionary rather than
    the object. Both spellings are read here, because a command line that told an operator to
    stop when the engine said "retry" would forbid the one action that would have worked
    (A11-revised).
    """
    if bool(getattr(failure, "retryable", False)):
        return True
    details = getattr(failure, "details", None)
    if isinstance(details, Mapping):
        return details.get("retryable") is True
    return False


def is_inconclusive(failure: GrafxError) -> bool:
    """Return whether a typed failure explicitly says that it certified nothing."""
    if getattr(failure, "inconclusive", None) is True:
        return True
    details = getattr(failure, "details", None)
    if isinstance(details, Mapping):
        return details.get("inconclusive") is True
    return False


def exit_code_for(failure: GrafxError) -> int:
    """Return the exit code that reports one typed failure to a script.

    Damaged bytes come first and have their own code, because FR-8 and FR-10 turn that class into
    truncation, quarantine and a forensic ledger entry: an operator's automation must be able to
    branch on it without reading the message. Everything else is told apart by whether the
    failure says it is worth retrying. An explicit inconclusive declaration comes next: it is
    neither a permanent refusal nor a clean answer, and retry remains the more useful action when
    a failure declares both.
    """
    if isinstance(failure, GrafxCorruptionDetected):
        return DAMAGED
    if is_retryable(failure):
        return RETRY
    if is_inconclusive(failure):
        return INCONCLUSIVE
    return REFUSED


def result_word(code: int) -> str:
    """Return the one-word machine-readable name of an exit code.

    An unknown number answers ``"internal"`` rather than raising: this function is called while a
    report is being written, and a report that fails to render is a report an operator never sees.
    """
    return RESULT_WORDS.get(code, RESULT_WORDS[INTERNAL])
