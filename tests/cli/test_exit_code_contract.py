"""The exit codes are the contract with a script (C12).

An operator's automation branches on a number, so the number is the part that must not drift.
These tests pin three things: the values themselves, the rule that turns a typed failure into
one, and the invariant that the word printed in the machine-readable document is derived from
the same code the shell sees rather than written next to it.
"""

from __future__ import annotations

import pytest

from okto_grafx.cli import exits
from okto_grafx.cli.exits import (
    DAMAGED,
    EXIT_CODE_MEANINGS,
    FINDINGS,
    INCONCLUSIVE,
    INTERNAL,
    INTERRUPTED,
    OK,
    REFUSED,
    RESULT_WORDS,
    RETRY,
    USAGE,
    exit_code_for,
    is_inconclusive,
    is_retryable,
    result_word,
)
from okto_grafx.domain import errors as taxonomy
from okto_grafx.domain.errors import (
    GrafxCorruptionDetected,
    GrafxError,
    GrafxLeaseTimeout,
    GrafxQuarantineError,
    GrafxUnsupportedOperation,
    GrafxWriteConflict,
)

FROZEN_CODES: dict[str, int] = {
    "OK": 0,
    "FINDINGS": 1,
    "USAGE": 2,
    "REFUSED": 3,
    "DAMAGED": 4,
    "RETRY": 5,
    "INCONCLUSIVE": 6,
    "INTERNAL": 70,
    "INTERRUPTED": 130,
}
"""The published values, written out here so a change to one has to be a change to two files."""


@pytest.mark.parametrize("name", sorted(FROZEN_CODES))
def test_every_exit_code_keeps_the_value_it_publishes(name: str) -> None:
    assert getattr(exits, name) == FROZEN_CODES[name]


def test_no_two_exit_codes_share_a_value() -> None:
    values = list(FROZEN_CODES.values())
    assert len(set(values)) == len(values)


def test_every_exit_code_is_documented_and_named() -> None:
    codes = set(FROZEN_CODES.values())
    assert set(EXIT_CODE_MEANINGS) == codes
    assert set(RESULT_WORDS) == codes
    for code in codes:
        assert EXIT_CODE_MEANINGS[code].strip()
        assert RESULT_WORDS[code].strip()


def test_every_result_word_is_distinct() -> None:
    words = list(RESULT_WORDS.values())
    assert len(set(words)) == len(words)


def test_damage_gets_its_own_code_because_it_drives_quarantine() -> None:
    failure = GrafxCorruptionDetected("A page image failed its checksum.", page=7)
    assert exit_code_for(failure) == DAMAGED
    assert DAMAGED not in {OK, FINDINGS, REFUSED, RETRY, INCONCLUSIVE}


def test_a_retryable_refusal_is_told_apart_from_a_permanent_one() -> None:
    assert exit_code_for(GrafxWriteConflict("Partitions intersect.")) == RETRY
    assert exit_code_for(GrafxLeaseTimeout("The lease did not arrive.")) == RETRY
    assert exit_code_for(GrafxUnsupportedOperation("Rows cannot be written.")) == REFUSED


def test_a_retryable_flag_carried_in_details_is_still_read() -> None:
    # C11 measured a durability failure arriving with details["retryable"] intact, and a caller
    # that reads only the class would forbid the retry the engine is inviting.
    failure = GrafxError("A transient device condition.", retryable=None)
    object.__setattr__(failure, "details", {"retryable": True})
    assert is_retryable(failure) is True
    assert exit_code_for(failure) == RETRY


def test_a_failure_that_says_nothing_about_retrying_is_not_retryable() -> None:
    failure = GrafxError("Something permanent.")
    assert is_retryable(failure) is False
    assert exit_code_for(failure) == REFUSED


def test_an_explicitly_inconclusive_failure_is_not_flattened_into_refused() -> None:
    failure = GrafxQuarantineError(
        "The namespace snapshot could not be certified.",
        conclusive=False,
        inconclusive=True,
    )

    assert is_inconclusive(failure) is True
    assert exit_code_for(failure) == INCONCLUSIVE


def test_damage_and_retry_take_precedence_over_inconclusive() -> None:
    damaged = GrafxCorruptionDetected(
        "Stored bytes are damaged.", retryable=True, inconclusive=True
    )
    retry = GrafxQuarantineError(
        "The namespace is temporarily unavailable.", retryable=True, inconclusive=True
    )

    assert exit_code_for(damaged) == DAMAGED
    assert exit_code_for(retry) == RETRY


def _concrete_grafx_errors() -> list[type[GrafxError]]:
    """Return every concrete error class the taxonomy of section 2 publishes."""
    found: list[type[GrafxError]] = []
    for name in dir(taxonomy):
        candidate = getattr(taxonomy, name)
        if isinstance(candidate, type) and issubclass(candidate, GrafxError):
            found.append(candidate)
    return sorted(found, key=lambda item: item.__name__)


@pytest.mark.parametrize(
    "failure_class", _concrete_grafx_errors(), ids=lambda item: item.__name__
)
def test_the_whole_taxonomy_maps_to_a_documented_code(failure_class: type[GrafxError]) -> None:
    # Every class in section 2, not a hand-picked few: a taxonomy that grows must not be able to
    # produce a failure this tool has no number for.
    code = exit_code_for(failure_class("A message in en-US."))
    assert code in EXIT_CODE_MEANINGS
    assert code in {REFUSED, DAMAGED, RETRY}


def test_the_taxonomy_sweep_examines_the_whole_taxonomy() -> None:
    # A72: the parametrisation above proves nothing if it collected two classes.
    assert len(_concrete_grafx_errors()) >= 20


@pytest.mark.parametrize("code", sorted(FROZEN_CODES.values()))
def test_the_result_word_of_a_code_is_the_one_it_publishes(code: int) -> None:
    assert result_word(code) == RESULT_WORDS[code]


def test_an_unknown_code_still_answers_rather_than_raising() -> None:
    # This runs while a report is being written. A renderer that raises here is a report an
    # operator never sees.
    assert result_word(-1) == RESULT_WORDS[INTERNAL]
    assert result_word(9999) == RESULT_WORDS[INTERNAL]


def test_clean_and_not_clean_are_different_numbers() -> None:
    # The whole point of the set: a script must be able to tell these apart without prose.
    assert OK != FINDINGS
    assert OK != INCONCLUSIVE
    assert FINDINGS != INCONCLUSIVE
    assert USAGE not in {OK, FINDINGS, INCONCLUSIVE}
    assert INTERRUPTED not in {OK, FINDINGS, INCONCLUSIVE, INTERNAL}
