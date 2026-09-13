"""Rule-based temporal conversion; separate from liveness and transaction clocks."""

from __future__ import annotations

from typing import Protocol

from okto_grafx.domain.model.temporal_values import DateTimeValue, LocalDateTimeValue


class TemporalZoneResolver(Protocol):
    """Named-zone rules pinned to a diagnosable provider for one runtime instance."""

    @property
    def data_version(self) -> str:
        """Identify the rules source/version, never the ambient machine timezone."""
        ...

    def at_local(self, local: LocalDateTimeValue, zone: str, *,
                 offset_seconds: int | None = None) -> DateTimeValue:
        """Resolve a wall time; explicit offsets must agree with the zone rules.

        Without an explicit offset, overlap chooses the earlier instant and gap
        shifts forward by its transition length. No unknown zone defaults to UTC.
        """
        ...

    def at_instant(self, seconds: int, nanosecond: int, zone: str) -> DateTimeValue:
        """Keep an exact instant while applying the provider's named-zone rules."""
        ...


__all__ = [
    'TemporalZoneResolver',
]
