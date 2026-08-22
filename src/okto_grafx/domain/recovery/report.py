"""What one recovery pass did (CONTRACT.md section 8.6, SPEC-M1 FR-8, AC-4).

``RecoveryReport`` is the frozen shape section 8.6 declares, and the four outcome words are a
closed set the metric catalogue also enumerates. They say what recovery DID, not how bad the
database was:

* ``clean`` -- nothing was discarded and nothing was retired. The ordinary open.
* ``truncated`` -- the log had a damaged tail, its bytes were quarantined, and the tail was cut.
* ``quarantined`` -- a whole control-plane record was retired under quarantine and a ledger
  entry, which is the deliberate act carried finding CF-1 hands to this component. Every
  truncation quarantines first, so this word is reserved for the retirement, otherwise it would
  cover ``truncated`` entirely and one of the two would be a word no run could ever produce.
* ``refused`` -- ``recovery_policy="refuse"`` met damage and stopped with everything on disk
  untouched.

``findings`` carries everything a report should say and an outcome word cannot: which segment was
cut, which range was quarantined, which control record was retired, and every disagreement the
replay met that was not itself a discard. A finding is a statement about the database, never an
error: recovery's product is this report.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.ids import NO_LSN, Lsn

__all__ = [
    "RECOVERY_OUTCOMES",
    "RECOVERY_POLICIES",
    "POLICY_REFUSE",
    "POLICY_REPLAY",
    "OUTCOME_CLEAN",
    "OUTCOME_QUARANTINED",
    "OUTCOME_REFUSED",
    "OUTCOME_TRUNCATED",
    "FindingKind",
    "RecoveryFinding",
    "RecoveryReport",
    "stronger_outcome",
]

OUTCOME_CLEAN: str = "clean"
"""Nothing was discarded and nothing was retired."""

OUTCOME_TRUNCATED: str = "truncated"
"""A damaged log tail was quarantined and then cut."""

OUTCOME_QUARANTINED: str = "quarantined"
"""A control-plane record was retired under quarantine and a forensic ledger entry (CF-1)."""

OUTCOME_REFUSED: str = "refused"
"""The refuse policy met damage and left everything on disk untouched."""

RECOVERY_OUTCOMES: tuple[str, ...] = (
    OUTCOME_CLEAN,
    OUTCOME_TRUNCATED,
    OUTCOME_QUARANTINED,
    OUTCOME_REFUSED,
)
"""The closed set of outcome words, in increasing order of what recovery had to do."""

POLICY_REPLAY: str = "replay"
"""The DEFAULT policy of FR-8: replay to the last intact record."""

POLICY_REFUSE: str = "refuse"
"""The explicit opt-in of FR-8: refuse to open rather than discard anything."""

RECOVERY_POLICIES: tuple[str, ...] = (POLICY_REPLAY, POLICY_REFUSE)
"""The only two policies CONTRACT.md section 5 and FR-8 define."""

_OUTCOME_RANK: dict[str, int] = {word: rank for rank, word in enumerate(RECOVERY_OUTCOMES)}


class FindingKind:
    """The vocabulary of recovery findings. A closed set, so a caller can switch on it."""

    DAMAGED_RANGE: str = "damaged_range"
    DISCARDED_RECORD: str = "discarded_record"
    LSN_DISCONTINUITY: str = "lsn_discontinuity"
    LOG_TRUNCATED: str = "log_truncated"
    TRUNCATION_DEFERRED: str = "truncation_deferred"
    QUARANTINED_RANGE: str = "quarantined_range"
    CONTROL_RECORD_RETIRED: str = "control_record_retired"
    READER_HORIZON_REDERIVED: str = "reader_horizon_rederived"
    CATALOG_UNREADABLE: str = "catalog_unreadable"
    CATALOG_ADOPTED: str = "catalog_adopted"
    REDO_REFUSED: str = "redo_refused"
    UNWRITTEN_PAGE: str = "unwritten_page"


@dataclass(frozen=True, slots=True)
class RecoveryFinding:
    """One thing recovery observed, with enough location to act on it."""

    kind: str
    detail: str
    file: str = ""
    offset: int = 0
    length: int = 0
    lsn: Lsn = NO_LSN
    page: int = -1
    entry_id: int = 0
    quarantine: str = ""

    def __post_init__(self) -> None:
        """Refuse a finding that names nothing, since a finding is read by a human."""
        if not isinstance(self.kind, str) or not self.kind:
            raise GrafxConfigurationError(
                "A recovery finding must carry a kind.", field="kind", value=repr(self.kind)
            )
        if not isinstance(self.detail, str) or not self.detail:
            raise GrafxConfigurationError(
                f"The {self.kind!r} finding must carry a detail a reader can act on.",
                field="detail",
                value=repr(self.detail),
            )


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """The frozen result of one recovery pass (CONTRACT.md section 8.6)."""

    outcome: str
    records_replayed: int = 0
    records_discarded: int = 0
    ledger_entries_created: int = 0
    last_good_lsn: Lsn = NO_LSN
    findings: tuple[RecoveryFinding, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        """Refuse a report whose outcome is not one of the four words section 8.6 freezes."""
        if self.outcome not in _OUTCOME_RANK:
            raise GrafxConfigurationError(
                f"A recovery outcome is one of {RECOVERY_OUTCOMES}; got {self.outcome!r}.",
                field="outcome",
                value=repr(self.outcome),
            )
        for name, value in (
            ("records_replayed", self.records_replayed),
            ("records_discarded", self.records_discarded),
            ("ledger_entries_created", self.ledger_entries_created),
            ("last_good_lsn", self.last_good_lsn),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise GrafxConfigurationError(
                    f"The {name} of a recovery report must be a non-negative integer; "
                    f"got {value!r}.",
                    field=name,
                    value=repr(value),
                )
        if not isinstance(self.findings, tuple):
            raise GrafxConfigurationError(
                f"The findings of a recovery report are a tuple; got "
                f"{type(self.findings).__name__}.",
                field="findings",
                value=type(self.findings).__name__,
            )

    def findings_of(self, kind: str) -> tuple[RecoveryFinding, ...]:
        """Return every finding of one kind, so a caller need not filter by hand."""
        return tuple(finding for finding in self.findings if finding.kind == kind)


def stronger_outcome(left: str, right: str) -> str:
    """Return whichever of two outcome words describes the larger act.

    The order is the one ``RECOVERY_OUTCOMES`` declares, so a pass that both cut a tail and
    retired a control record reports the retirement -- the act an operator must be told about.
    """
    for word in (left, right):
        if word not in _OUTCOME_RANK:
            raise GrafxConfigurationError(
                f"A recovery outcome is one of {RECOVERY_OUTCOMES}; got {word!r}.",
                field="outcome",
                value=repr(word),
            )
    return left if _OUTCOME_RANK[left] >= _OUTCOME_RANK[right] else right
