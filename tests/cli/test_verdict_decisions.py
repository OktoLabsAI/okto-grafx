"""The three decisions that say whether a database is clean, tested on their own (C12).

Each of these is reached through a real database elsewhere in this suite. They are also tested
directly here, over the full matrix of inputs including the ones a real database in this build
cannot currently produce -- because a term that is only defensible while another component keeps
its own invariant is exactly the term a test driven through that component cannot see (A62,
LESSONS L12). A guard nothing can distinguish is a guard the mutation battery reports as a
survivor and nobody can defend.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from okto_grafx.cli.commands import (
    VERDICT_CLEAN,
    VERDICT_CODES,
    VERDICT_DAMAGED,
    VERDICT_INCONCLUSIVE,
    recovery_is_clean,
    status_code,
    status_reasons,
    verification_verdict,
)
from okto_grafx.cli.exits import FINDINGS, INCONCLUSIVE, OK
from okto_grafx.domain.recovery.report import RecoveryReport


@dataclass(frozen=True, slots=True)
class _Pass:
    """A recovery pass shaped like the frozen report, for combinations a real one cannot produce.

    Looser than :class:`~okto_grafx.domain.recovery.report.RecoveryReport` in exactly one way,
    and it is stated here because a double that is looser without saying so proves things about
    itself (LESSONS L12): the real class refuses an outcome outside its closed set, and this one
    does not, so that the redundancy of the discard term can be examined at all. Every test that
    uses a combination the real class can express uses the real class instead.
    """

    outcome: str
    records_discarded: int = 0


@pytest.mark.parametrize(
    ("findings", "examined", "expected"),
    [
        (0, 0, VERDICT_INCONCLUSIVE),
        (0, 1, VERDICT_CLEAN),
        (0, 10_000, VERDICT_CLEAN),
        (1, 0, VERDICT_DAMAGED),
        (1, 1, VERDICT_DAMAGED),
        (5, 10_000, VERDICT_DAMAGED),
    ],
)
def test_the_verification_verdict_covers_the_whole_matrix(
    findings: int, examined: int, expected: str
) -> None:
    assert verification_verdict(findings, examined) == expected


def test_a_walk_with_no_findings_and_nothing_examined_is_never_clean() -> None:
    # The one cell that matters most: it is indistinguishable from a clean walk by findings alone.
    assert verification_verdict(0, 0) != VERDICT_CLEAN


def test_a_walk_with_findings_is_damaged_however_much_it_examined() -> None:
    for examined in (0, 1, 3, 1_000_000):
        assert verification_verdict(1, examined) == VERDICT_DAMAGED


def test_every_verdict_has_exactly_one_exit_code() -> None:
    assert VERDICT_CODES == {
        VERDICT_CLEAN: OK,
        VERDICT_DAMAGED: FINDINGS,
        VERDICT_INCONCLUSIVE: INCONCLUSIVE,
    }
    assert len(set(VERDICT_CODES.values())) == 3


def test_a_clean_recovery_needs_both_an_outcome_and_no_discards() -> None:
    assert recovery_is_clean(RecoveryReport(outcome="clean")) is True
    assert recovery_is_clean(RecoveryReport(outcome="truncated")) is False
    assert recovery_is_clean(RecoveryReport(outcome="quarantined")) is False
    assert recovery_is_clean(RecoveryReport(outcome="refused")) is False


def test_a_recovery_that_says_clean_and_discarded_work_is_not_clean() -> None:
    # The combination C6's own invariant currently prevents. Asserting it here is the difference
    # between a guard that is load-bearing and a guard nothing can tell apart from its absence.
    assert recovery_is_clean(_Pass(outcome="clean", records_discarded=1)) is False
    assert recovery_is_clean(_Pass(outcome="clean", records_discarded=0)) is True


def test_a_recovery_report_carrying_a_real_discard_is_not_clean() -> None:
    real = RecoveryReport(outcome="truncated", records_discarded=2, ledger_entries_created=2)
    assert recovery_is_clean(real) is False


def test_a_clean_database_produces_no_reasons() -> None:
    reasons = status_reasons(
        recovery=RecoveryReport(outcome="clean"),
        ledger_total=0,
        quarantined=0,
        damaged_ledger_tail=False,
    )
    assert reasons == []
    assert status_code(reasons, recovered=True, evidence=False) == OK


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"recovery": None}, "no recovery ran"),
        ({"recovery": RecoveryReport(outcome="truncated")}, "reported 'truncated'"),
        (
            {"recovery": RecoveryReport(outcome="truncated", records_discarded=3)},
            "3 record(s) were discarded",
        ),
        ({"ledger_total": 2}, "the ledger holds 2"),
        ({"quarantined": 4}, "quarantine holds 4"),
        ({"damaged_ledger_tail": True}, "unreadable tail"),
    ],
)
def test_every_reason_a_database_is_not_clean_is_stated_on_its_own(
    kwargs: dict[str, object], fragment: str
) -> None:
    # Each condition alone must produce its own sentence: an operator told a database is not
    # clean is owed the whole list, and a term folded into a single boolean names nothing.
    arguments: dict[str, object] = {
        "recovery": RecoveryReport(outcome="clean"),
        "ledger_total": 0,
        "quarantined": 0,
        "damaged_ledger_tail": False,
    }
    arguments.update(kwargs)
    reasons = status_reasons(**arguments)  # type: ignore[arg-type]
    assert any(fragment in reason for reason in reasons), reasons
    assert len(reasons) >= 1


def test_all_the_reasons_appear_together_rather_than_the_first_one() -> None:
    reasons = status_reasons(
        recovery=RecoveryReport(outcome="truncated", records_discarded=1),
        ledger_total=1,
        quarantined=1,
        damaged_ledger_tail=True,
    )
    assert len(reasons) == 5


@pytest.mark.parametrize(
    ("recovered", "evidence", "expected"),
    [
        (True, True, FINDINGS),
        (True, False, FINDINGS),
        (False, True, FINDINGS),
        (False, False, INCONCLUSIVE),
    ],
)
def test_a_read_only_open_that_knows_nothing_reports_inconclusive_not_damage(
    recovered: bool, evidence: bool, expected: int
) -> None:
    assert status_code(["a reason"], recovered=recovered, evidence=evidence) == expected


def test_no_reasons_always_reports_a_clean_code() -> None:
    for recovered in (True, False):
        for evidence in (True, False):
            assert status_code([], recovered=recovered, evidence=evidence) == OK
