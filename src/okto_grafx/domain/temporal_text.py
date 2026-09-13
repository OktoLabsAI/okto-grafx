"""Native temporal text construction; no ambient clock, rule source or storage tag."""

from __future__ import annotations

from fractions import Fraction
from re import compile as _compile_pattern, fullmatch as _fullmatch, search as _search, split as _split

from .errors import GrafxUnsupportedOperation
from .model.errors import SchemaMismatchError
from .model.temporal_values import (
    DateTimeValue, DateValue, DurationValue, LocalDateTimeValue, LocalTimeValue, TimeValue,
    _component, _integer, _offset, _zone_key, days_in_month,
)
from .ports.temporal_zone import TemporalZoneResolver

MAX_TEMPORAL_TEXT = 1024
_CLOCK = _compile_pattern(r"(?:([0-9]{1,2}):([0-9]{2})(?::([0-9]{2})(?:[.,]([0-9]{1,9}))?)?"
                    r"|([0-9]{2})(?:([0-9]{2})(?:([0-9]{2})(?:[.,]([0-9]{1,9}))?)?)?)")
_UNIT = _compile_pattern(r"([+-]?[0-9]+(?:[.,][0-9]+)?)([YMWDHS])")


def _invalid(kind: str) -> SchemaMismatchError:
    return SchemaMismatchError("Invalid native temporal text.", field=kind, reason="temporal_text_syntax")


def _text(value: str, kind: str) -> str:
    if type(value) is not str:
        raise SchemaMismatchError("Temporal text requires an exact string.", field=kind, reason="temporal_component_type")
    if not 1 <= len(value) <= MAX_TEMPORAL_TEXT:
        raise SchemaMismatchError("Temporal text is outside its bounded length.", field=kind,
                                  reason="temporal_text_bounds", maximum=MAX_TEMPORAL_TEXT)
    if not value.isascii():
        raise _invalid(kind)
    return value


def parse_date(text: str) -> DateValue:
    """Parse the supported bounded calendar, ordinal, week or quarter date spelling."""
    text = _text(text, "date")
    # Compact unsigned dates are unambiguous by their complete length.
    if _fullmatch(r"[0-9]{6,8}", text):
        year = int(text[:4])
        if len(text) == 7:
            return DateValue.from_ordinal_day(year, int(text[4:]))
        return DateValue(year, int(text[4:6]), int(text[6:]) if len(text) == 8 else 1)
    marker = _fullmatch(r"([0-9]{4}|[+-][0-9]{1,9})-?([WQ])([0-9-]+)", text)
    if marker:
        year, kind, tail = int(marker[1]), marker[2], marker[3]
        if "-" in tail:
            parts = tail.split("-")
            if len(parts) != 2 or not all(parts):
                raise _invalid("date")
            primary, day = parts
        else:
            width = 2 if kind == "W" else 1
            primary, day = tail[:width], tail[width:] or "1"
        if not (1 <= len(primary) <= (2 if kind == "W" else 1)) or not 1 <= len(day) <= (1 if kind == "W" else 2):
            raise _invalid("date")
        if kind == "W":
            return DateValue.from_week_date(year, int(primary), int(day))
        quarter = _integer(int(primary), "quarter", 1, 4)
        month = 3 * quarter - 2
        maximum = sum(days_in_month(year, m) for m in range(month, month + 3))
        return DateValue(year, month).add_days(_integer(int(day), "day_of_quarter", 1, maximum) - 1)
    match = _fullmatch(r"([0-9]{4}|[+-][0-9]{1,9})(?:-([0-9]{1,3})(?:-([0-9]{1,2}))?)?", text)
    if not match:
        raise _invalid("date")
    year, middle, day = int(match[1]), match[2], match[3]
    if middle is None:
        return DateValue(year)
    if day is None and len(middle) == 3:
        return DateValue.from_ordinal_day(year, int(middle))
    if len(middle) > 2 or (day is None and len(middle) != 2):
        raise _invalid("date")
    return DateValue(year, int(middle), int(day) if day is not None else 1)


def _clock_parts(text: str) -> tuple[int, int, int, int]:
    match = _CLOCK.fullmatch(text)
    if match is None:
        raise _invalid("time")
    parts = match.groups()[:4] if match[1] is not None else match.groups()[4:]
    return (*(int(part or "0") for part in parts[:3]), int((parts[3] or "0").ljust(9, "0")))


def parse_localtime(text: str) -> LocalTimeValue:
    """Parse a bounded local clock spelling into nanoseconds since midnight."""
    text = _text(text, "localtime")
    return LocalTimeValue.from_components(*_clock_parts(text.removeprefix("T")))


def parse_localdatetime(text: str) -> LocalDateTimeValue:
    """Parse an unzoned date and optional T-separated time, defaulting to midnight."""
    text = _text(text, "localdatetime")
    date, separator, time = text.partition("T")
    return LocalDateTimeValue(parse_date(date), parse_localtime(time) if separator else LocalTimeValue(0))


def parse_offset(text: str) -> int:
    """Parse a bounded UTC offset spelling into validated seconds east of UTC."""
    text = _text(text, "offset")
    if text == "Z":
        return 0
    match = _fullmatch(r"([+-])([0-9]{2})(?:(?::([0-9]{2})(?::([0-9]{2}))?)|([0-9]{2})([0-9]{2})?)?", text)
    if not match:
        raise _invalid("offset")
    minutes, seconds = int(match[3] or match[5] or 0), int(match[4] or match[6] or 0)
    _integer(minutes, "offset_minute", 0, 59)
    _integer(seconds, "offset_second", 0, 59)
    total = int(match[2]) * 3600 + minutes * 60 + seconds
    return _offset(-total if match[1] == "-" else total)


def _named_suffix(text: str) -> tuple[str, str | None]:
    zone = None
    if "[" in text or "]" in text:
        match = _fullmatch(r"([^\[\]]+)\[([^\[\]]+)\]", text)
        if not match:
            raise _invalid("timezone")
        text, zone = match[1], match[2]
        _zone_key(zone)
    return text, zone


def _zone_suffix(text: str) -> tuple[str, int | None, str | None]:
    text, zone = _named_suffix(text)
    # Called only on the clock portion, never on a signed calendar year.
    match = _search(r"(?:Z|[+-].*)\Z", text)
    return (text[:match.start()], parse_offset(match[0]), zone) if match else (text, None, zone)


def parse_datetime(text: str, *, resolver: TemporalZoneResolver | None = None,
                   default_offset: int = 0) -> DateTimeValue:
    """Parse datetime text with explicit offset or named-zone resolution."""
    text, zone = _named_suffix(_text(text, "datetime"))
    date, separator, clock = text.partition("T")
    clock, offset, _ = _zone_suffix(clock if separator else "00")
    local = LocalDateTimeValue(parse_date(date), parse_localtime(clock))
    if zone is not None:
        if resolver is None:
            raise GrafxUnsupportedOperation("Named datetime requires an explicit zone provider.", field="temporal_timezone_provider")
        return resolver.at_local(local, zone, offset_seconds=offset)
    return DateTimeValue(local, _offset(default_offset) if offset is None else offset)


def parse_time(text: str, *, resolver: TemporalZoneResolver | None = None,
               reference_date: DateValue | None = None, default_offset: int = 0) -> TimeValue:
    """Parse time text, requiring a resolver and reference date for a named zone."""
    clock, offset, zone = _zone_suffix(_text(text, "time"))
    local = parse_localtime(clock)
    if zone is not None:
        if resolver is None or reference_date is None:
            raise GrafxUnsupportedOperation("Named time requires an explicit date and zone provider.", field="temporal_timezone_provider")
        _component(reference_date, DateValue, "reference_date")
        resolved = resolver.at_local(LocalDateTimeValue(reference_date, local), zone, offset_seconds=offset)
        return TimeValue(resolved.local.time, resolved.offset_seconds)
    return TimeValue(local, _offset(default_offset) if offset is None else offset)


def parse_duration(text: str) -> DurationValue:
    """Parse a signed ISO-style duration without approximating calendar months as days."""
    text = _text(text, "duration").upper()
    sign = -1 if text.startswith("-") else 1
    text = text[1:] if text[:1] in ("+", "-") else text
    if not text.startswith("P"):
        raise _invalid("duration")
    body = text[1:]
    date, separator, clock = body.partition("T")
    # The alternate numeric form denotes quantities, not a validated calendar date.
    numeric = _fullmatch(r"([0-9]{4})-([0-9]{2})-([0-9]{2})|([0-9]{4})([0-9]{2})([0-9]{2})", date)
    if numeric or (not date and separator and clock and clock[-1:] not in "HMS"):
        years, months, days = (tuple(int(part) for part in (numeric.groups()[:3] if numeric[1] else numeric.groups()[3:]))
                              if numeric else (0, 0, 0))
        _integer(months, "months", 0, 12)
        _integer(days, "days", 0, 31)
        hour, minute, second, nano = _clock_parts(clock) if separator else (0, 0, 0, 0)
        _integer(hour, "hours", 0, 24)
        _integer(minute, "minutes", 0, 60)
        _integer(second, "seconds", 0, 60)
        return DurationValue(sign * (years * 12 + months), sign * days,
                             sign * (hour * 3600 + minute * 60 + second), sign * nano)
    if not body or (separator and not clock):
        raise _invalid("duration")
    amounts = []
    for part, order in ((date, "YMWD"), (clock, "HMS")):
        position, previous = 0, -1
        for match in _UNIT.finditer(part):
            unit, number = match[2], match[1]
            if match.start() != position or unit not in order or order.index(unit) <= previous:
                raise _invalid("duration")
            previous, position = order.index(unit), match.end()
            if amounts and amounts[-1][2]:
                raise _invalid("duration")
            fractional = "." in number or "," in number
            if unit == "S" and fractional and len(_split(r"[.,]", number)[1]) > 9:
                raise _invalid("duration")
            amounts.append((unit if order == "YMWD" else unit.lower(), Fraction(number.replace(",", ".")), fractional))
        if position != len(part):
            raise _invalid("duration")
    if not amounts:
        raise _invalid("duration")
    values = {unit: amount * sign for unit, amount, _ in amounts}
    months = values.get("Y", 0) * 12 + values.get("M", 0)
    days = values.get("W", 0) * 7 + values.get("D", 0) + (months - int(months)) * Fraction(2629746, 86400)
    seconds = (values.get("h", 0) * 3600 + values.get("m", 0) * 60 + values.get("s", 0)
               + (days - int(days)) * 86400)
    nanos = int(seconds * 1_000_000_000)  # exact decimal arithmetic; truncate sub-nanosecond remainder toward zero
    whole, remainder = divmod(nanos, 1_000_000_000)
    return DurationValue(int(months), int(days), whole, remainder)


__all__ = [
    'MAX_TEMPORAL_TEXT',
    'parse_date',
    'parse_localtime',
    'parse_localdatetime',
    'parse_offset',
    'parse_datetime',
    'parse_time',
    'parse_duration',
]
