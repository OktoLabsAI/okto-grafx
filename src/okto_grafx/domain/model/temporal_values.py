"""FP-5 native temporal primitives; query/storage integration remains in progress.

Calendar arithmetic uses proleptic Gregorian integer days, including year zero
and negative/expanded years. No platform datetime range, machine timezone, float
rounding or mutable decimal context participates in these value proofs.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import SchemaMismatchError

MIN_YEAR = -999_999_999
MAX_YEAR = 999_999_999
NANOSECONDS_PER_SECOND = 1_000_000_000
NANOSECONDS_PER_DAY = 86_400 * NANOSECONDS_PER_SECOND
_MIN_I64 = -(1 << 63)
_MAX_I64 = (1 << 63) - 1


def _integer(value: int, name: str, lower: int, upper: int) -> int:
    if type(value) is not int:
        raise SchemaMismatchError("Temporal components require exact integers.",
                                  field=name, reason="temporal_component_type")
    if not lower <= value <= upper:
        raise SchemaMismatchError("Temporal component is outside its range.",
                                  field=name, reason="temporal_component_bounds",
                                  minimum=lower, maximum=upper)
    return value


def _component(value: object, expected: type, name: str) -> None:
    if type(value) is not expected:
        raise SchemaMismatchError("Temporal values require native immutable components.",
                                  field=name, reason="temporal_component_type")


@dataclass(frozen=True, slots=True, order=True)
class TemporalInstant:
    """Exact POSIX seconds/nanoseconds, independent of any calendar or liveness clock."""

    seconds: int
    nanosecond: int = 0

    def __post_init__(self) -> None:
        _integer(self.seconds, "epoch_seconds", _MIN_I64, _MAX_I64)
        _integer(self.nanosecond, "nanosecond", 0, NANOSECONDS_PER_SECOND - 1)


def _offset(offset_seconds: int) -> int:
    return _integer(offset_seconds, "offset_seconds", -18 * 3600, 18 * 3600)


def _offset_text(offset_seconds: int) -> str:
    if offset_seconds == 0:
        return "Z"
    hours, remainder = divmod(abs(offset_seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    return ("+" if offset_seconds > 0 else "-") + f"{hours:02d}:{minutes:02d}" + (
        f":{seconds:02d}" if seconds else "")


def _zone_key(zone: str | None) -> None:
    """Validate a logical key, not its rules; only the zone adapter resolves rules."""
    if zone is None:
        return
    if (type(zone) is not str or not 1 <= len(zone) <= 255 or not zone.isascii()
            or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789/_+-." for c in zone)
            or any(part in ("", ".", "..") for part in zone.split("/"))):
        raise SchemaMismatchError("A named timezone requires a bounded logical IANA key.",
                                  field="timezone", reason="temporal_zone_key")


def is_leap_year(year: int) -> bool:
    """Test the proleptic Gregorian leap-year rule."""
    _integer(year, "year", MIN_YEAR, MAX_YEAR)
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def days_in_month(year: int, month: int) -> int:
    """Return the number of days in the requested Gregorian month."""
    _integer(month, "month", 1, 12)
    leap = is_leap_year(year)
    return (31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)[month - 1]


def _epoch_day(year: int, month: int, day: int) -> int:
    # March-based 400-year eras make negative years follow the same floor-division
    # rules as positive years; 719468 translates the origin to 1970-01-01.
    adjusted = year - (month <= 2)
    era, within = divmod(adjusted, 400)
    shifted_month = month - 3 if month > 2 else month + 9
    day_in_year = (153 * shifted_month + 2) // 5 + day - 1
    return era * 146097 + within * 365 + within // 4 - within // 100 + day_in_year - 719468


@dataclass(frozen=True, slots=True, order=True)
class DateValue:
    """A calendar date, not an instant or a machine-local datetime."""

    year: int
    month: int = 1
    day: int = 1

    def __post_init__(self) -> None:
        _integer(self.year, "year", MIN_YEAR, MAX_YEAR)
        _integer(self.month, "month", 1, 12)
        _integer(self.day, "day", 1, days_in_month(self.year, self.month))

    @property
    def epoch_day(self) -> int:
        """Return signed days relative to the Unix epoch date."""
        return _epoch_day(self.year, self.month, self.day)

    @property
    def day_of_week(self) -> int:
        """Return the ISO weekday number, Monday 1 through Sunday 7."""
        return (self.epoch_day + 3) % 7 + 1

    @property
    def ordinal_day(self) -> int:
        """Return the one-based day number within this calendar year."""
        return self.epoch_day - _epoch_day(self.year, 1, 1) + 1

    @property
    def quarter(self) -> int:
        """Return the calendar quarter number, 1 through 4."""
        return (self.month - 1) // 3 + 1

    @property
    def day_of_quarter(self) -> int:
        """Return the one-based day number within this calendar quarter."""
        return self.epoch_day - _epoch_day(self.year, 3 * (self.quarter - 1) + 1, 1) + 1

    @property
    def week_year(self) -> int:
        """Return the ISO week-numbering year, including boundary weeks."""
        def start(year: int) -> int:
            """Find the epoch day of the Monday beginning an ISO week-numbering year."""
            fourth = _epoch_day(year, 1, 4)
            return fourth - (fourth + 3) % 7
        if self.epoch_day < start(self.year):
            return self.year - 1
        return self.year + 1 if self.epoch_day >= start(self.year + 1) else self.year

    @property
    def week(self) -> int:
        """Return the one-based ISO week number within the week-numbering year."""
        fourth = _epoch_day(self.week_year, 1, 4)
        return (self.epoch_day - (fourth - (fourth + 3) % 7)) // 7 + 1

    @classmethod
    def from_epoch_day(cls, epoch_day: int) -> "DateValue":
        """Construct a date from a signed epoch-day count within the supported year range."""
        _integer(epoch_day, "epoch_day", _epoch_day(MIN_YEAR, 1, 1), _epoch_day(MAX_YEAR, 12, 31))
        era, day_of_era = divmod(epoch_day + 719468, 146097)
        year_in_era = (day_of_era - day_of_era // 1460 + day_of_era // 36524 - day_of_era // 146096) // 365
        year = year_in_era + era * 400
        day_of_year = day_of_era - (365 * year_in_era + year_in_era // 4 - year_in_era // 100)
        shifted_month = (5 * day_of_year + 2) // 153
        day = day_of_year - (153 * shifted_month + 2) // 5 + 1
        month = shifted_month + 3 if shifted_month < 10 else shifted_month - 9
        return cls(year + (month <= 2), month, day)

    @classmethod
    def from_ordinal_day(cls, year: int, ordinal_day: int) -> "DateValue":
        """Construct a date from a validated year and one-based ordinal day."""
        _integer(ordinal_day, "ordinal_day", 1, 366 if is_leap_year(year) else 365)
        return cls.from_epoch_day(_epoch_day(year, 1, 1) + ordinal_day - 1)

    @classmethod
    def from_week_date(cls, year: int, week: int, day_of_week: int = 1) -> "DateValue":
        """Construct a date from validated ISO week-year, week and weekday components."""
        january_fourth = cls(year, 1, 4)
        january_first = cls(year)
        maximum = 53 if january_first.day_of_week == 4 or (
            january_first.day_of_week == 3 and is_leap_year(year)) else 52
        _integer(week, "week", 1, maximum)
        _integer(day_of_week, "day_of_week", 1, 7)
        monday = january_fourth.epoch_day - january_fourth.day_of_week + 1
        return cls.from_epoch_day(monday + 7 * (week - 1) + day_of_week - 1)

    def add_days(self, days: int) -> "DateValue":
        """Add an exact signed number of days, validating the resulting date range."""
        _integer(days, "days", _MIN_I64, _MAX_I64)
        return type(self).from_epoch_day(self.epoch_day + days)

    def add_months(self, months: int) -> "DateValue":
        """Shift calendar months and clamp to the destination month's final day."""
        _integer(months, "months", _MIN_I64, _MAX_I64)
        year, month = divmod(self.year * 12 + self.month - 1 + months, 12)
        return type(self)(year, month + 1, min(self.day, days_in_month(year, month + 1)))

    def isoformat(self) -> str:
        """Format a calendar date with an explicitly signed extended year when needed."""
        year = (f"{self.year:04d}" if 0 <= self.year <= 9999 else
                ("-" if self.year < 0 else "+") + f"{abs(self.year):04d}")
        return f"{year}-{self.month:02d}-{self.day:02d}"


@dataclass(frozen=True, slots=True, order=True)
class LocalTimeValue:
    """Time of day at nanosecond precision, without a timezone or inferred date."""

    nanoseconds: int

    def __post_init__(self) -> None:
        _integer(self.nanoseconds, "nanoseconds_of_day", 0, NANOSECONDS_PER_DAY - 1)

    @classmethod
    def from_components(cls, hour: int, minute: int = 0, second: int = 0, nanosecond: int = 0) -> "LocalTimeValue":
        """Construct a local time from validated hour, minute, second and nanosecond fields."""
        _integer(hour, "hour", 0, 23)
        _integer(minute, "minute", 0, 59)
        _integer(second, "second", 0, 59)
        _integer(nanosecond, "nanosecond", 0, NANOSECONDS_PER_SECOND - 1)
        return cls((hour * 3600 + minute * 60 + second) * NANOSECONDS_PER_SECOND + nanosecond)

    @property
    def hour(self) -> int:
        """Return the hour component, 0 through 23."""
        return self.nanoseconds // (3600 * NANOSECONDS_PER_SECOND)

    @property
    def minute(self) -> int:
        """Return the minute component, 0 through 59."""
        return self.nanoseconds // (60 * NANOSECONDS_PER_SECOND) % 60

    @property
    def second(self) -> int:
        """Return the second component, 0 through 59."""
        return self.nanoseconds // NANOSECONDS_PER_SECOND % 60

    @property
    def nanosecond(self) -> int:
        """Return the fractional nanosecond component, 0 through 999999999."""
        return self.nanoseconds % NANOSECONDS_PER_SECOND

    def isoformat(self) -> str:
        """Format local clock components with the required fractional precision."""
        text = f"{self.hour:02d}:{self.minute:02d}"
        if self.second or self.nanosecond:
            text += f":{self.second:02d}"
        if self.nanosecond:
            digits = 3 if self.nanosecond % 1_000_000 == 0 else (6 if self.nanosecond % 1000 == 0 else 9)
            text += "." + f"{self.nanosecond:09d}"[:digits]
        return text


@dataclass(frozen=True, slots=True)
class DurationValue:
    """Separate calendar months/days and elapsed seconds; no fixed-month coercion."""

    months: int = 0
    days: int = 0
    seconds: int = 0
    nanoseconds: int = 0

    def __post_init__(self) -> None:
        for name in ("months", "days", "seconds", "nanoseconds"):
            _integer(getattr(self, name), name, _MIN_I64, _MAX_I64)
        carry, remainder = divmod(self.nanoseconds, NANOSECONDS_PER_SECOND)
        seconds = _integer(self.seconds + carry, "seconds", _MIN_I64, _MAX_I64)
        object.__setattr__(self, "seconds", seconds)
        object.__setattr__(self, "nanoseconds", remainder)

    def isoformat(self) -> str:
        """Format duration components without replacing calendar units with fixed seconds."""
        def signed(value: int, negative: bool) -> str:
            """Format a magnitude using the sign of its original duration component."""
            return ("-" if negative else "") + str(value)

        years, months = divmod(abs(self.months), 12)
        text = "P"
        if years:
            text += signed(years, self.months < 0) + "Y"
        if months:
            text += signed(months, self.months < 0) + "M"
        if self.days:
            text += str(self.days) + "D"
        total_nanos = self.seconds * NANOSECONDS_PER_SECOND + self.nanoseconds
        if total_nanos:
            negative = total_nanos < 0
            hours, remainder = divmod(abs(total_nanos), 3600 * NANOSECONDS_PER_SECOND)
            minutes, remainder = divmod(remainder, 60 * NANOSECONDS_PER_SECOND)
            seconds, nanos = divmod(remainder, NANOSECONDS_PER_SECOND)
            text += "T"
            if hours:
                text += signed(hours, negative) + "H"
            if minutes:
                text += signed(minutes, negative) + "M"
            if seconds or nanos:
                text += signed(seconds, negative)
                if nanos:
                    text += "." + f"{nanos:09d}".rstrip("0")
                text += "S"
        return "PT0S" if text == "P" else text


@dataclass(frozen=True, slots=True, order=True)
class LocalDateTimeValue:
    """Calendar date and nanosecond clock time; no implicit timezone or instant."""

    date: DateValue
    time: LocalTimeValue

    def __post_init__(self) -> None:
        _component(self.date, DateValue, "date")
        _component(self.time, LocalTimeValue, "time")

    @property
    def local_epoch_seconds(self) -> int:
        """Return local calendar seconds relative to the epoch, without applying a zone."""
        return self.date.epoch_day * 86400 + self.time.nanoseconds // NANOSECONDS_PER_SECOND

    @classmethod
    def from_local_epoch_parts(cls, seconds: int, nanosecond: int = 0) -> "LocalDateTimeValue":
        """Construct an unzoned datetime from local epoch seconds and a nanosecond remainder."""
        _integer(seconds, "seconds", _MIN_I64, _MAX_I64)
        _integer(nanosecond, "nanosecond", 0, NANOSECONDS_PER_SECOND - 1)
        days, seconds_in_day = divmod(seconds, 86400)
        return cls(DateValue.from_epoch_day(days), LocalTimeValue(seconds_in_day * NANOSECONDS_PER_SECOND + nanosecond))

    def isoformat(self) -> str:
        """Format the local date and time separated by T, without an offset suffix."""
        return self.date.isoformat() + "T" + self.time.isoformat()


@dataclass(frozen=True, slots=True)
class TimeValue:
    """Local time plus exact UTC offset, with no invented calendar date."""

    time: LocalTimeValue
    offset_seconds: int

    def __post_init__(self) -> None:
        _component(self.time, LocalTimeValue, "time")
        _offset(self.offset_seconds)

    @property
    def utc_nanoseconds(self) -> int:
        # Do not wrap midnight: offset-adjusted times preserve their day displacement.
        """Return nanoseconds relative to local midnight adjusted by the UTC offset."""
        return self.time.nanoseconds - self.offset_seconds * NANOSECONDS_PER_SECOND

    @property
    def sort_key(self) -> tuple[int, int]:
        """Return the UTC-position and offset tie-break key for total ordering."""
        return self.utc_nanoseconds, self.offset_seconds

    def isoformat(self) -> str:
        """Format the local clock value followed by its explicit UTC offset."""
        return self.time.isoformat() + _offset_text(self.offset_seconds)


@dataclass(frozen=True, slots=True)
class DateTimeValue:
    """Nanosecond instant with its local representation, offset and optional zone key.

    Offset/name agreement is proved by the zone adapter at construction/rebind,
    not by ambient global timezone data inside this immutable value container.
    """

    local: LocalDateTimeValue
    offset_seconds: int
    zone: str | None = None

    def __post_init__(self) -> None:
        _component(self.local, LocalDateTimeValue, "local")
        _offset(self.offset_seconds)
        _zone_key(self.zone)

    @property
    def epoch_seconds(self) -> int:
        """Return exact UTC epoch seconds after subtracting the stored offset."""
        return self.local.local_epoch_seconds - self.offset_seconds

    @property
    def nanosecond(self) -> int:
        """Return the fractional nanosecond component of the local clock."""
        return self.local.time.nanosecond

    @classmethod
    def from_epoch_parts(cls, seconds: int, nanosecond: int = 0, *,
                         offset_seconds: int = 0, zone: str | None = None) -> "DateTimeValue":
        """Construct an offset datetime from a UTC instant and optional zone identifier."""
        _integer(seconds, "seconds", _MIN_I64, _MAX_I64)
        _offset(offset_seconds)
        return cls(LocalDateTimeValue.from_local_epoch_parts(seconds + offset_seconds, nanosecond),
                   offset_seconds, zone)

    @property
    def sort_key(self) -> tuple[int, int, int, str]:
        """Return instant, offset and zone components used for deterministic total ordering."""
        return self.epoch_seconds, self.nanosecond, self.offset_seconds, self.zone or ""

    def isoformat(self) -> str:
        """Format the local datetime, explicit offset and optional bracketed zone name."""
        return self.local.isoformat() + _offset_text(self.offset_seconds) + (
            f"[{self.zone}]" if self.zone is not None else "")


TemporalValue = DateValue | LocalTimeValue | TimeValue | LocalDateTimeValue | DateTimeValue | DurationValue


__all__ = [
    'TemporalValue',
    'MIN_YEAR',
    'MAX_YEAR',
    'NANOSECONDS_PER_SECOND',
    'NANOSECONDS_PER_DAY',
    'TemporalInstant',
    'is_leap_year',
    'days_in_month',
    'DateValue',
    'LocalTimeValue',
    'DurationValue',
    'LocalDateTimeValue',
    'TimeValue',
    'DateTimeValue',
]
