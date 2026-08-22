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
from okto_grafx.domain.ids import NO_CSN, Csn, Lsn

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

    def visible(self, xmin: Csn, xmax: Csn) -> bool:
        """Return True when a version created at xmin and ended at xmax belongs to this view.

        This is the predicate of CONTRACT.md section 8.5, character for character:

        * ``xmin != 0`` -- a version whose creating transaction has no commit number was never
          committed, so no snapshot may see it;
        * ``xmin <= read_lsn`` -- it was committed at or before this snapshot opened;
        * ``xmax == 0 or xmax > read_lsn`` -- it had not been superseded or deleted yet.

        There is deliberately no fourth term. Adding one would hide a row a snapshot is entitled
        to see, and removing one would show a row it must not.
        """
        return xmin != 0 and xmin <= self.read_lsn and (xmax == 0 or xmax > self.read_lsn)
