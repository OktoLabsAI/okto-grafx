"""The snapshot a transaction reads under (CONTRACT.md section 8.5, SPEC-M1 FR-2 and BR-9).

A snapshot is one number and one predicate. The number is the LSN that was published when the
transaction opened; the predicate says whether a stored version was already committed at that
moment and had not yet been superseded. Nothing else is needed, and nothing else is offered:
the storage layer takes this predicate structurally (amendment A19) precisely so the rule lives
in exactly one place.
"""

from __future__ import annotations

from dataclasses import dataclass

from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.ids import (
    NO_CSN,
    PROVISIONAL_CSN,
    Csn,
    Lsn,
    is_committed_csn,
    is_open_end_csn,
)

__all__ = ["Snapshot"]


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A fixed view of the database: the LSN a transaction opened at, and what it may see.

    The value is frozen because BR-9 says the view a reader gets is fixed at the moment it
    opens. A snapshot that could be advanced would be a snapshot that could show a commit the
    transaction had already been told did not exist.
    """

    read_lsn: Lsn

    def __post_init__(self) -> None:
        """Refuse a read LSN that is not a usable log sequence number.

        A negative or non-integer value is a caller mistake, not damaged bytes, so it is a
        configuration error and never ``corruption_detected`` (amendment A11-revised).
        """
        value = self.read_lsn
        if isinstance(value, bool) or not isinstance(value, int):
            raise GrafxConfigurationError(
                f"A snapshot read LSN must be an integer; got {type(value).__name__}.",
                field="read_lsn",
                value=repr(value),
            )
        if value < NO_CSN:
            raise GrafxConfigurationError(
                f"A snapshot read LSN must not be negative; got {value}.",
                field="read_lsn",
                value=value,
            )
        if value >= PROVISIONAL_CSN:
            raise GrafxConfigurationError(
                "A snapshot read LSN must be below the value reserved for provisional heap "
                f"versions ({PROVISIONAL_CSN}); got {value}.",
                field="read_lsn",
                value=value,
            )

    def visible(self, xmin: Csn, xmax: Csn) -> bool:
        """Return True when a version created at xmin and ended at xmax belongs to this view.

        This is the committed predicate of CONTRACT.md section 8.5 plus the fail-closed
        interpretation of the reserved pre-WAL stamp:

        * ``0 < xmin < PROVISIONAL_CSN`` -- a version whose creator has no real commit number
          was never committed, so no snapshot may see it;
        * ``xmin <= read_lsn`` -- it was committed at or before this snapshot opened;
        * ``xmax`` is zero/provisional or above ``read_lsn`` -- it has no committed end visible
          to this snapshot. A provisional end is an abandoned attempt to end an older row.

        The same sentinel therefore fails closed at both edges: it cannot create visibility and
        cannot take visibility away.
        """
        return (
            is_committed_csn(xmin)
            and xmin <= self.read_lsn
            and (is_open_end_csn(xmax) or xmax > self.read_lsn)
        )
