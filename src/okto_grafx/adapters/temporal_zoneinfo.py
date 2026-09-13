"""Package-backed IANA rules; no system TZPATH or process-global timezone mutation.

Recorded transition lookup and annual-rule evaluation retain the native expanded
year range. FP-5 query/storage integration remains in progress.
"""

from __future__ import annotations

from collections import OrderedDict
from importlib import resources
from struct import error as StructError
from threading import RLock
from zoneinfo import ZoneInfo

from okto_grafx.adapters.temporal_tzif import MAX_RULE_BYTES, TemporalZoneRule, parse_zone_rule
from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxUnsupportedOperation
from okto_grafx.domain.model.errors import SchemaMismatchError
from okto_grafx.domain.model.temporal_values import (
    DateTimeValue, LocalDateTimeValue, _MAX_I64, _MIN_I64,
    _component, _integer, _offset, _zone_key,
)


class ZoneInfoTemporalResolver:
    """A bounded rule cache backed only by the declared tzdata dependency."""

    def __init__(self) -> None:
        try:
            import tzdata
        except ImportError as error:
            raise GrafxUnsupportedOperation("Named temporal zones require the tzdata package.",
                                            field="temporal_timezone_data") from error
        self._version = f"tzdata:{tzdata.__version__};iana:{tzdata.IANA_VERSION}"
        self._rules: OrderedDict[str, TemporalZoneRule] = OrderedDict()
        self._guard = RLock()

    @property
    def data_version(self) -> str:
        """Return the resolver's declared timezone data version."""
        return self._version

    def _rule(self, zone: str) -> TemporalZoneRule:
        _zone_key(zone)
        if zone is None:
            raise SchemaMismatchError("A named zone must be provided.", field="timezone",
                                      reason="temporal_zone_key")
        with self._guard:
            if zone in self._rules:
                self._rules.move_to_end(zone)
                return self._rules[zone]
            try:
                resource = resources.files("tzdata.zoneinfo").joinpath(*zone.split("/"))
                with resource.open("rb") as stream:
                    result = parse_zone_rule(stream.read(MAX_RULE_BYTES + 1), zone, factory=ZoneInfo.from_file)
            except (FileNotFoundError, IsADirectoryError) as error:
                raise SchemaMismatchError("Named timezone is not present in the declared rules data.",
                                          field="timezone", value=zone, reason="temporal_unknown_zone") from error
            except (ValueError, EOFError, StructError) as error:
                raise GrafxCorruptionDetected("Named timezone data is malformed.", field="temporal_timezone_data",
                                             value=zone, source=self._version) from error
            except (OSError, ModuleNotFoundError) as error:
                raise GrafxUnsupportedOperation("Declared timezone data could not be read.",
                                                field="temporal_timezone_data", value=zone,
                                                source=self._version) from error
            if len(self._rules) >= 128:
                self._rules.popitem(last=False)
            self._rules[zone] = result
            return result

    def at_local(self, local: LocalDateTimeValue, zone: str, *,
                 offset_seconds: int | None = None) -> DateTimeValue:
        """Resolve a local datetime, validating an optional offset and permitting gap adjustment."""
        if offset_seconds is not None:
            _offset(offset_seconds)
        return self._at_local(local, zone, offset_seconds=offset_seconds, permit_gap=True)

    def _at_local(self, local: LocalDateTimeValue, zone: str, *,
                  offset_seconds: int | None, permit_gap: bool) -> DateTimeValue:
        _component(local, LocalDateTimeValue, "local")
        rule = self._rule(zone)
        candidates: dict[int, DateTimeValue] = {}
        unspecified = False
        for offset in rule.offsets:
            actual = rule.offset_at(local.local_epoch_seconds - offset)
            unspecified |= actual is None
            if actual == offset:
                candidates[offset] = DateTimeValue(local, offset, zone)
        if offset_seconds is not None:
            if offset_seconds not in candidates:
                raise SchemaMismatchError("Explicit UTC offset disagrees with named timezone rules.",
                                          field="timezone", value=zone, reason="temporal_offset_mismatch")
            return candidates[offset_seconds]
        if candidates:
            return min(candidates.values(), key=lambda value: (value.epoch_seconds, value.nanosecond))
        if unspecified:
            raise GrafxUnsupportedOperation("Timezone data does not specify this local-time interval.",
                                            field="temporal_timezone_interval", value=zone)
        shift = rule.gap_shift(local.local_epoch_seconds) if permit_gap else None
        if shift is not None:
            shifted = LocalDateTimeValue.from_local_epoch_parts(
                local.local_epoch_seconds + shift, local.time.nanosecond)
            return self._at_local(shifted, zone, offset_seconds=None, permit_gap=False)
        raise SchemaMismatchError("Local time cannot be resolved by the named timezone rules.",
                                  field="timezone", value=zone, reason="temporal_local_time_gap")

    def at_instant(self, seconds: int, nanosecond: int, zone: str) -> DateTimeValue:
        """Convert a validated UTC instant using the named zone's authoritative offset."""
        _integer(seconds, "seconds", _MIN_I64, _MAX_I64)
        _integer(nanosecond, "nanosecond", 0, 999999999)
        rule = self._rule(zone)
        offset = rule.offset_at(seconds)
        if offset is None:
            raise GrafxUnsupportedOperation("Timezone data does not specify this instant.",
                                            field="temporal_timezone_interval", value=zone)
        return DateTimeValue.from_epoch_parts(seconds, nanosecond, offset_seconds=offset, zone=zone)


__all__ = [
    'ZoneInfoTemporalResolver',
]
