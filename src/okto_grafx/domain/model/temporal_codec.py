"""V1 temporal wire contract shared by general value and tuple codecs.

These stable tags match ValueType; transaction/catalog admission publishes the
capability before values become durable. Encoding alone is not a store write.
"""

from __future__ import annotations

from enum import IntEnum
import struct

from ..errors import GrafxCorruptionDetected
from .errors import SchemaMismatchError
from .temporal_values import (
    DateValue, LocalTimeValue, TimeValue, LocalDateTimeValue, DateTimeValue, DurationValue,
    TemporalValue, _component, _integer,
)

TEMPORAL_VALUES_CAPABILITY = "temporal_values_v1"
TEMPORAL_VALUES_CAPABILITY_BIT = 1 << 23


class TemporalValueTag(IntEnum):
    """V1 temporal tags; existing stored tags 0..11 are unchanged."""

    DATE = 12
    LOCALTIME = 13
    TIME = 14
    LOCALDATETIME = 15
    DATETIME = 16
    DURATION = 17


_TYPES = {DateValue:TemporalValueTag.DATE, LocalTimeValue:TemporalValueTag.LOCALTIME,
          TimeValue:TemporalValueTag.TIME, LocalDateTimeValue:TemporalValueTag.LOCALDATETIME,
          DateTimeValue:TemporalValueTag.DATETIME, DurationValue:TemporalValueTag.DURATION}
_DATE = struct.Struct("<q")
_TIME = struct.Struct("<Q")
_OFFSET_TIME = struct.Struct("<Qi")
_LOCAL = struct.Struct("<qQ")
_ZONED = struct.Struct("<qIiB")
_DURATION = struct.Struct("<qqqI")
MAX_TEMPORAL_VALUE_BYTES = 1 + _ZONED.size + 255


def _date(value):
    _component(value,DateValue,"date")
    return DateValue(value.year,value.month,value.day)


def _time(value):
    _component(value,LocalTimeValue,"time")
    return LocalTimeValue(value.nanoseconds)


def _local(value):
    _component(value,LocalDateTimeValue,"localdatetime")
    return LocalDateTimeValue(_date(value.date),_time(value.time))


def encode_temporal_value(value: object) -> bytes:
    """Encode one bounded canonical frame, without granting storage admission."""
    kind = _TYPES.get(type(value))
    if kind is None:
        raise SchemaMismatchError("Temporal encoding requires an exact native value.",reason="temporal_codec_type")
    if kind is TemporalValueTag.DATE:
        body = _DATE.pack(_date(value).epoch_day)
    elif kind is TemporalValueTag.LOCALTIME:
        body = _TIME.pack(_time(value).nanoseconds)
    elif kind is TemporalValueTag.TIME:
        checked = TimeValue(_time(value.time),value.offset_seconds)
        body = _OFFSET_TIME.pack(checked.time.nanoseconds,checked.offset_seconds)
    elif kind is TemporalValueTag.LOCALDATETIME:
        checked = _local(value)
        body = _LOCAL.pack(checked.date.epoch_day,checked.time.nanoseconds)
    elif kind is TemporalValueTag.DATETIME:
        checked = DateTimeValue(_local(value.local),value.offset_seconds,value.zone)
        zone = checked.zone.encode("ascii") if checked.zone is not None else b""
        body = _ZONED.pack(checked.epoch_seconds,checked.nanosecond,checked.offset_seconds,len(zone))+zone
    else:
        # Defensive revalidation also prevents forged integer-like fields from
        # invoking __index__ through struct.pack. Decode separately forbids
        # noncanonical nanos instead of normalizing corrupt stored bytes.
        checked = DurationValue(value.months,value.days,value.seconds,value.nanoseconds)
        body = _DURATION.pack(checked.months,checked.days,checked.seconds,checked.nanoseconds)
    return bytes((kind,))+body


def decode_temporal_value(raw: bytes, offset: int = 0) -> tuple[TemporalValue, int]:
    """Decode one frame and return (native value, next offset), allowing siblings.

    Unknown/truncated/noncanonical frames refuse as corruption. No timezone
    lookup can reinterpret recorded offsets or zone names on this read path.
    """
    if type(raw) is not bytes or type(offset) is not int or not 0 <= offset < len(raw):
        raise GrafxCorruptionDetected("Invalid temporal frame buffer or cursor.",field="temporal_frame")
    start = offset
    try:
        kind = TemporalValueTag(raw[offset])
    except ValueError as error:
        raise GrafxCorruptionDetected("Unknown temporal wire tag.",field="temporal_tag",offset=offset) from error
    offset += 1
    shape = {TemporalValueTag.DATE:_DATE,TemporalValueTag.LOCALTIME:_TIME,
             TemporalValueTag.TIME:_OFFSET_TIME,TemporalValueTag.LOCALDATETIME:_LOCAL,
             TemporalValueTag.DATETIME:_ZONED,TemporalValueTag.DURATION:_DURATION}[kind]
    if len(raw)-offset < shape.size:
        raise GrafxCorruptionDetected("Truncated temporal frame.",field="temporal_frame",offset=start)
    parts = shape.unpack_from(raw,offset)
    offset += shape.size
    try:
        if kind is TemporalValueTag.DATE:
            value = DateValue.from_epoch_day(parts[0])
        elif kind is TemporalValueTag.LOCALTIME:
            value = LocalTimeValue(parts[0])
        elif kind is TemporalValueTag.TIME:
            value = TimeValue(LocalTimeValue(parts[0]),parts[1])
        elif kind is TemporalValueTag.LOCALDATETIME:
            value = LocalDateTimeValue(DateValue.from_epoch_day(parts[0]),LocalTimeValue(parts[1]))
        elif kind is TemporalValueTag.DATETIME:
            seconds,nanos,utc_offset,length = parts
            if len(raw)-offset < length:
                raise GrafxCorruptionDetected("Truncated temporal zone name.",field="temporal_zone",offset=offset)
            zone = raw[offset:offset+length].decode("ascii") if length else None
            value = DateTimeValue.from_epoch_parts(seconds,nanos,offset_seconds=utc_offset,zone=zone)
            offset += length
        else:
            _integer(parts[3],"nanosecond",0,999999999)
            value = DurationValue(*parts)
    except (SchemaMismatchError,UnicodeDecodeError) as error:
        raise GrafxCorruptionDetected("Invalid or noncanonical temporal payload.",field="temporal_value",offset=start) from error
    return value,offset


__all__ = [
    'TEMPORAL_VALUES_CAPABILITY',
    'TEMPORAL_VALUES_CAPABILITY_BIT',
    'TemporalValueTag',
    'MAX_TEMPORAL_VALUE_BYTES',
    'encode_temporal_value',
    'decode_temporal_value',
]
