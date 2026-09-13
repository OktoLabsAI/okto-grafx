"""Explicit transaction/statement query clocks, separate from liveness clocks."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import GrafxCorruptionDetected, GrafxUnsupportedOperation
from .model.errors import SchemaMismatchError
from .model.temporal_values import DateTimeValue, TemporalInstant, TemporalValue, TimeValue, _component, _zone_key
from .ports.temporal_clock import TemporalClock
from .ports.temporal_zone import TemporalZoneResolver
from .temporal_text import parse_offset

_INSTANT_TYPES = frozenset({"date","localtime","time","localdatetime","datetime"})
_CLOCK_MODES = frozenset({"transaction","statement","realtime"})


def _choice(value: str, choices: frozenset[str], field: str) -> str:
    if type(value) is not str or len(value) > 32 or value.lower() not in choices:
        raise SchemaMismatchError("Unknown temporal context selector.", field=field, reason="temporal_context_selector")
    return value.lower()


def _zone(zone: str, resolver: TemporalZoneResolver | None) -> int | None:
    if type(zone) is not str:
        raise SchemaMismatchError("Temporal timezone must be an exact string.", field="timezone", reason="temporal_component_type")
    if zone == "Z" or zone.startswith(("+","-")):
        return parse_offset(zone)
    _zone_key(zone)
    if resolver is None:
        raise GrafxUnsupportedOperation("A named temporal timezone requires an explicit provider.", field="temporal_timezone_provider")
    return None


def _read(clock: TemporalClock) -> TemporalInstant:
    instant = clock.now()
    _component(instant, TemporalInstant, "temporal_clock_result")
    return instant


@dataclass(frozen=True, slots=True)
class TemporalTransactionContext:
    """Immutable captured transaction instant with its clock, timezone and resolver."""
    clock: TemporalClock
    transaction_instant: TemporalInstant
    resolver: TemporalZoneResolver | None = None
    timezone: str = "Z"

    def __post_init__(self) -> None:
        _component(self.transaction_instant, TemporalInstant, "transaction_instant")
        _zone(self.timezone, self.resolver)

    @classmethod
    def begin(cls, clock: TemporalClock, *, resolver: TemporalZoneResolver | None = None,
              timezone: str = "Z") -> "TemporalTransactionContext":
        """Capture one validated transaction instant from the supplied clock."""
        _zone(timezone, resolver)
        return cls(clock, _read(clock), resolver, timezone)

    def begin_statement(self) -> "TemporalStatementContext":
        """Capture a fresh statement instant while retaining the original transaction instant."""
        return TemporalStatementContext(self.clock, self.transaction_instant, _read(self.clock), self.resolver, self.timezone)


@dataclass(frozen=True, slots=True)
class TemporalStatementContext:
    """Immutable transaction and statement instants with explicit realtime clock access."""
    clock: TemporalClock
    transaction_instant: TemporalInstant
    statement_instant: TemporalInstant
    resolver: TemporalZoneResolver | None = None
    timezone: str = "Z"

    def __post_init__(self) -> None:
        _component(self.transaction_instant, TemporalInstant, "transaction_instant")
        _component(self.statement_instant, TemporalInstant, "statement_instant")
        _zone(self.timezone, self.resolver)

    def instant(self, mode: str = "statement") -> TemporalInstant:
        """Select the captured transaction/statement instant or read the realtime clock."""
        mode = _choice(mode, _CLOCK_MODES, "temporal_clock_mode")
        if mode == "transaction":
            return self.transaction_instant
        if mode == "statement":
            return self.statement_instant
        return _read(self.clock)

    def current(self, kind: str, *, mode: str = "statement", timezone: str | None = None) -> TemporalValue:
        """Build the requested native temporal kind in the selected clock mode and timezone."""
        kind = _choice(kind, _INSTANT_TYPES, "temporal_type")
        zone = self.timezone if timezone is None else timezone
        offset = _zone(zone, self.resolver)
        instant = self.instant(mode)
        if offset is None:
            # Provider required by _zone; do not replace unavailable data with UTC.
            assert self.resolver is not None
            zoned = self.resolver.at_instant(instant.seconds, instant.nanosecond, zone)
            _component(zoned, DateTimeValue, "temporal_zone_result")
            if (zoned.epoch_seconds, zoned.nanosecond, zoned.zone) != (instant.seconds, instant.nanosecond, zone):
                raise GrafxCorruptionDetected("Temporal zone provider changed the captured instant or zone identity.",
                                             field="temporal_zone_result")
        else:
            zoned = DateTimeValue.from_epoch_parts(instant.seconds, instant.nanosecond, offset_seconds=offset)
        if kind == "datetime":
            return zoned
        if kind == "localdatetime":
            return zoned.local
        if kind == "date":
            return zoned.local.date
        if kind == "localtime":
            return zoned.local.time
        return TimeValue(zoned.local.time, zoned.offset_seconds)


__all__ = [
    'TemporalTransactionContext',
    'TemporalStatementContext',
]
