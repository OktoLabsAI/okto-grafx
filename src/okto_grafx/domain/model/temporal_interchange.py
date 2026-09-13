"""Exact temporal components shared by JSON observations and typed transports.

No host datetime conversion or timezone resolution is allowed on this boundary.
The wire codec remains the authority for defensive native-value validation.
"""

from __future__ import annotations

from .temporal_values import (
    DateValue, LocalTimeValue, TimeValue, LocalDateTimeValue, DateTimeValue, DurationValue, TemporalValue,
)
from .temporal_codec import encode_temporal_value
from .errors import SchemaMismatchError
from re import fullmatch as _fullmatch


TEMPORAL_CLASSES_BY_NAME = {
    "DATE": DateValue, "LOCALTIME": LocalTimeValue, "TIME": TimeValue,
    "LOCALDATETIME": LocalDateTimeValue, "DATETIME": DateTimeValue, "DURATION": DurationValue,
}
_NAMES = {cls: name for name, cls in TEMPORAL_CLASSES_BY_NAME.items()}
_WIDE = {"epoch_day", "epoch_seconds", "months", "days", "seconds"}
_JSON_FIELDS = {
    "DATE": ("epoch_day",), "LOCALTIME": ("nanoseconds",),
    "TIME": ("nanoseconds", "offset_seconds"),
    "LOCALDATETIME": ("epoch_day", "nanoseconds"),
    "DATETIME": ("epoch_seconds", "nanosecond", "offset_seconds", "zone"),
    "DURATION": ("months", "days", "seconds", "nanoseconds"),
}


def temporal_components(value: object) -> dict[str, object]:
    """Return owned primitive coordinates after validating an exact native value."""
    encode_temporal_value(value)
    kind = type(value)
    if kind is DateValue:
        return {"epoch_day": value.epoch_day}
    if kind is LocalTimeValue:
        return {"nanoseconds": value.nanoseconds}
    if kind is TimeValue:
        return {"nanoseconds": value.time.nanoseconds, "offset_seconds": value.offset_seconds}
    if kind is LocalDateTimeValue:
        return {"epoch_day": value.date.epoch_day, "nanoseconds": value.time.nanoseconds}
    if kind is DateTimeValue:
        return {"epoch_seconds": value.epoch_seconds, "nanosecond": value.nanosecond,
                "offset_seconds": value.offset_seconds, "zone": value.zone}
    return {"months": value.months, "days": value.days, "seconds": value.seconds,
            "nanoseconds": value.nanoseconds}


def temporal_from_components(name: str, fields: dict[str, object]) -> TemporalValue:
    """Decode only canonical coordinates; no null children or implicit normalization."""
    if type(name) is not str or name not in TEMPORAL_CLASSES_BY_NAME or type(fields) is not dict:
        raise SchemaMismatchError("Invalid temporal transport shape.", reason="temporal_transport")
    try:
        if name == "DATE":
            value = DateValue.from_epoch_day(fields["epoch_day"])
        elif name == "LOCALTIME":
            value = LocalTimeValue(fields["nanoseconds"])
        elif name == "TIME":
            value = TimeValue(LocalTimeValue(fields["nanoseconds"]), fields["offset_seconds"])
        elif name == "LOCALDATETIME":
            value = LocalDateTimeValue(DateValue.from_epoch_day(fields["epoch_day"]), LocalTimeValue(fields["nanoseconds"]))
        elif name == "DATETIME":
            value = DateTimeValue.from_epoch_parts(fields["epoch_seconds"], fields["nanosecond"],
                                                 offset_seconds=fields["offset_seconds"], zone=fields["zone"])
        else:
            value = DurationValue(fields["months"], fields["days"], fields["seconds"], fields["nanoseconds"])
        if temporal_components(value) != fields:
            raise SchemaMismatchError("Noncanonical temporal coordinates.", reason="temporal_transport")
        return value
    except (KeyError, TypeError, ValueError, OverflowError) as failure:
        raise SchemaMismatchError("Invalid temporal coordinates.", reason="temporal_transport") from failure


def temporal_json_value(value: object) -> dict[str, object]:
    """Return the established entity-value JSON tags, also used by scalar CLI JSON."""
    components = temporal_components(value)
    name = _NAMES[type(value)]
    return {"type": name.lower(), **{
        key: str(item) if key in _WIDE or key == "nanoseconds" and name != "DURATION" else item
        for key, item in components.items()
    }}


def temporal_from_json_value(name: str, value: object) -> TemporalValue:
    """Decode the explicit JSON tags under an independently declared native type.

    Wide coordinates are canonical decimal strings, not lossy JSON doubles.
    No string/date inference or duration normalization is performed.
    """
    if (type(name) is not str or name not in _JSON_FIELDS or type(value) is not dict
            or set(value) != {"type", *_JSON_FIELDS[name]}
            or type(value["type"]) is not str or value["type"] != name.lower()):
        raise SchemaMismatchError("Invalid tagged temporal value.", reason="temporal_transport")
    fields = {}
    for key in _JSON_FIELDS[name]:
        item = value[key]
        if key in _WIDE or key == "nanoseconds" and name != "DURATION":
            if type(item) is not str or _fullmatch(r"(?:0|-?[1-9][0-9]{0,18})", item) is None:
                raise SchemaMismatchError("Invalid temporal decimal coordinate.", reason="temporal_transport")
            item = int(item)
        fields[key] = item
    return temporal_from_components(name, fields)


__all__ = [
    'TEMPORAL_CLASSES_BY_NAME',
    'temporal_components',
    'temporal_from_components',
    'temporal_json_value',
    'temporal_from_json_value',
]
