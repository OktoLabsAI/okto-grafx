"""GX-CAP-1 value contracts; no claim of durable lookup or public API admission.

An identity is a qualified reference, never evidence that a commit exists. The
publication coordinator, not these pure values, will assign durable identities.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import total_ordering

from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.ids import PROVISIONAL_CSN
from okto_grafx.domain.model import Timestamp


def _invalid(field: str) -> GrafxConfigurationError:
    return GrafxConfigurationError("Invalid commit identity or logical timestamp.", field=field)


@total_ordering
@dataclass(frozen=True, slots=True)
class CommitId:
    """Store-qualified, non-sentinel logical commit reference with store-local ordering."""

    database_uuid: bytes
    sequence: int

    def __post_init__(self) -> None:
        if type(self.database_uuid) is not bytes or len(self.database_uuid) != 16:
            raise _invalid("database_uuid")
        if type(self.sequence) is not int or not 0 < self.sequence < PROVISIONAL_CSN:
            raise _invalid("sequence")

    def __lt__(self, other: object) -> bool:
        if type(other) is not CommitId:
            return NotImplemented
        if self.database_uuid != other.database_uuid:
            raise _invalid("database_uuid")
        return self.sequence < other.sequence

    def to_token(self) -> str:
        """Canonical bounded transport spelling, independent of Python repr/pickle."""
        return self.database_uuid.hex() + ":" + format(self.sequence, "016x")

    @classmethod
    def parse(cls, token: str) -> CommitId:
        """Refuse alternate spellings instead of ambiguously normalizing an identity."""
        if type(token) is not str or len(token) != 49 or token[32] != ":":
            raise _invalid("commit_id")
        digits = token[:32] + token[33:]
        if any(character not in "0123456789abcdef" for character in digits):
            raise _invalid("commit_id")
        return cls(bytes.fromhex(token[:32]), int(token[33:], 16))


def _instant(value: Timestamp, field: str) -> Timestamp:
    if (type(value) is not Timestamp or type(value.micros) is not int
            or not -(1 << 63) <= value.micros < (1 << 63)):
        raise _invalid(field)
    return Timestamp(value.micros)


@dataclass(frozen=True, slots=True)
class CommitTime:
    """Immutable observed/ordered instants; never a clock or lease authority."""

    observed_at: Timestamp
    ordered_at: Timestamp
    clock_adjusted: bool

    def __post_init__(self) -> None:
        observed = _instant(self.observed_at, "observed_at")
        ordered = _instant(self.ordered_at, "ordered_at")
        if ordered.micros < observed.micros:
            raise _invalid("ordered_at")
        if (type(self.clock_adjusted) is not bool
                or self.clock_adjusted != (ordered.micros != observed.micros)):
            raise _invalid("clock_adjusted")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "ordered_at", ordered)


def assign_commit_time(observed_at: Timestamp, previous_ordered_at: Timestamp | None) -> CommitTime:
    """Compute one publication time from supplied values; no I/O or wall sampling.

    The future caller must provide the previous *durable* timestamp while holding
    the existing publication fence. This function alone cannot certify that fact.
    """
    observed = _instant(observed_at, "observed_at")
    ordered = observed.micros
    if previous_ordered_at is not None:
        previous = _instant(previous_ordered_at, "previous_ordered_at")
        if previous.micros == (1 << 63) - 1:
            raise _invalid("ordered_at")
        ordered = max(ordered, previous.micros + 1)
    return CommitTime(observed, Timestamp(ordered), ordered != observed.micros)

__all__ = ["CommitId","CommitTime","assign_commit_time"]
