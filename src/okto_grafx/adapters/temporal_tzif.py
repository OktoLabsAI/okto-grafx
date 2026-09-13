"""Bounded TZif history with exact Gregorian-cycle evaluation of annual tails.

Only the POSIX annual footer is evaluated through host datetime/ZoneInfo. Recorded
transitions are never remapped to another year. No private zoneinfo API is used.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
import re
from struct import pack, unpack_from
from typing import Callable
from zoneinfo import ZoneInfo

from okto_grafx.domain.errors import GrafxUnsupportedOperation

MAX_RULE_BYTES = 262_144
_CYCLE_SECONDS = 146097 * 86400
_CYCLE_ORIGIN = 946684800  # 2000-01-01 UTC
_HOST_ORIGIN = datetime(2000, 1, 1, tzinfo=timezone.utc)
_NAME = r"(?:[A-Za-z]{3,}|<[A-Za-z0-9+\-]{3,}>)"
_DELTA = r"[+\-]?[0-9]{1,3}(?::[0-9]{2}(?::[0-9]{2})?)?"
_PREFIX = re.compile(rf"{_NAME}({_DELTA})(?:({_NAME})({_DELTA})?)?", re.ASCII)


def _footer_offsets(footer: bytes) -> tuple[int, ...]:
    prefix = footer.decode("ascii").split(",", 1)[0]
    match = _PREFIX.fullmatch(prefix)
    if match is None:
        raise ValueError("Invalid annual timezone prefix")

    def offset(text: str) -> int:
        """Decode the reversed-sign POSIX footer offset into seconds east of UTC."""
        pieces = [int(piece) for piece in text.lstrip("+-").split(":")]
        if any(piece > 59 for piece in pieces[1:]):
            raise ValueError("Invalid offset minutes/seconds")
        seconds = sum(piece * unit for piece, unit in zip(pieces, (3600, 60, 1)))
        return seconds if text.startswith("-") else -seconds

    standard = offset(match[1])
    if match[2] is None:
        return (standard,)
    return (standard, offset(match[3]) if match[3] is not None else standard + 3600)


@dataclass(frozen=True, slots=True)
class TemporalZoneRule:
    """Immutable bounded TZif transitions and optional recurring future zone rules."""
    transitions: tuple[int, ...]
    transition_offsets: tuple[int | None, ...]
    initial_offset: int | None
    offsets: tuple[int, ...]
    annual: ZoneInfo | None
    annual_unspecified: bool = False

    def offset_at(self, seconds: int) -> int | None:
        """Resolve the offset at a UTC second, or return None when rules do not specify it."""
        index = bisect_right(self.transitions, seconds)
        if self.transitions and index == 0:
            return self.initial_offset
        if self.annual_unspecified and (not self.transitions or seconds > self.transitions[-1]):
            return None
        if self.annual is not None and (not self.transitions or seconds > self.transitions[-1]):
            # Gregorian dates/weekdays and every supported POSIX annual rule repeat
            # exactly after 146097 days. Only ask the host for an offset; the user's
            # actual date/instant/nanoseconds are never converted into this surrogate.
            host = _HOST_ORIGIN + timedelta(seconds=(seconds - _CYCLE_ORIGIN) % _CYCLE_SECONDS)
            delta = host.astimezone(self.annual).utcoffset()
            if delta is None or delta.microseconds:
                raise ValueError("Annual provider returned an invalid offset")
            return delta.days * 86400 + delta.seconds
        return self.transition_offsets[index - 1] if index else self.initial_offset

    def gap_shift(self, seconds: int) -> int | None:
        # An old offset is valid just before the missing wall interval, the new
        # offset just after it. Mutual checks discard unrelated historical offsets.
        """Find the smallest positive local-time gap shift consistent with both offsets."""
        shifts = []
        for old in self.offsets:
            new = self.offset_at(seconds - old)
            if new is not None and new > old and self.offset_at(seconds - new) == old:
                shifts.append(new - old)
        return min(shifts) if shifts else None


def _block(data: bytes, start: int, width: int):
    if len(data) - start < 44 or data[start:start + 4] != b"TZif":
        raise ValueError("Truncated or invalid TZif header")
    version = data[start + 4:start + 5]
    if version not in (b"\0", b"2", b"3", b"4") or any(data[start + 5:start + 20]):
        raise ValueError("Invalid TZif version/reserved header")
    utc_count, std_count, leaps, count, types, chars = unpack_from(">6I", data, start + 20)
    if not 1 <= types <= 256 or chars == 0 or utc_count not in (0, types) or std_count not in (0, types):
        raise ValueError("Invalid TZif counts")
    size = count * (width + 1) + types * 6 + chars + leaps * (width + 4) + std_count + utc_count
    position = start + 44
    end = position + size
    if end > len(data):
        raise ValueError("Truncated TZif block")
    transitions = unpack_from(f">{count}{'q' if width == 8 else 'i'}", data, position)
    if any(left >= right for left, right in zip(transitions, transitions[1:])):
        raise ValueError("Unordered TZif transitions")
    position += count * width
    indices = data[position:position + count]
    if any(index >= types for index in indices):
        raise ValueError("Invalid transition type index")
    position += count
    records = [unpack_from(">iBB", data, position + index * 6) for index in range(types)]
    position += types * 6
    designations = data[position:position + chars]
    for offset, dst, name_index in records:
        if offset == -(1 << 31) or dst not in (0, 1) or name_index >= chars:
            raise ValueError("Invalid TZif time type")
        name_end = designations.find(b"\0", name_index)
        if name_end < 0:
            raise ValueError("Unterminated TZif designation")
    position += chars + leaps * (width + 4)
    std_flags = data[position:position + std_count]
    utc_flags = data[position + std_count:end]
    if any(flag not in (0, 1) for flag in std_flags + utc_flags):
        raise ValueError("Invalid TZif indicators")
    if any(flag and (not std_flags or not std_flags[index]) for index, flag in enumerate(utc_flags)):
        raise ValueError("UTC indicator requires standard-time indicator")
    return version, end, transitions, indices, records, designations, leaps


def parse_zone_rule(data: bytes, key: str, *, factory: Callable = ZoneInfo.from_file) -> TemporalZoneRule:
    """Parse bounded TZif data and retain explicit unsupported or unspecified rule states."""
    if len(data) > MAX_RULE_BYTES:
        raise GrafxUnsupportedOperation("Timezone rule file exceeds the bounded provider budget.",
                                        field="temporal_timezone_data", maximum=MAX_RULE_BYTES)
    parsed = _block(data, 0, 4)
    version = parsed[0]
    if version != b"\0":
        parsed = _block(data, parsed[1], 8)
        if parsed[0] != version:
            raise ValueError("TZif headers disagree on version")
    _, end, transitions, indices, records, designations, leaps = parsed
    if leaps:
        raise GrafxUnsupportedOperation("Leap-adjusted TZif cannot resolve POSIX temporal instants.",
                                        field="temporal_timezone_data", value=key)
    known_offsets = tuple(None if designations[index:designations.find(b"\0", index)] == b"-00" else offset
                          for offset, _, index in records)
    footer = b""
    if version == b"\0":
        if end != len(data):
            raise ValueError("Trailing bytes after TZif v1")
    else:
        tail = data[end:]
        if len(tail) < 2 or tail[:1] != b"\n" or tail[-1:] != b"\n" or b"\n" in tail[1:-1]:
            raise ValueError("Malformed TZif footer framing")
        footer = tail[1:-1]
    offsets = {record[0] for record in records}
    annual = None
    annual_unspecified = footer.startswith(b"<-00>")
    if footer:
        offsets.update(_footer_offsets(footer))
        # Feed only annual rules to the public stdlib API, so explicit historical
        # transitions cannot contaminate the equivalent Gregorian-cycle year.
        header = b"TZif3" + bytes(15) + pack(">6I", 0, 0, 0, 0, 1, 1)
        block = pack(">iBB", 0, 0, 0) + b"\0"
        annual = factory(BytesIO((header + block) * 2 + b"\n" + footer + b"\n"), key=key)
    if any(not -64800 <= offset <= 64800 for offset in offsets):
        raise GrafxUnsupportedOperation("Timezone offset exceeds the native temporal offset range.",
                                        field="temporal_timezone_data", value=key)
    return TemporalZoneRule(transitions, tuple(known_offsets[index] for index in indices),
                            known_offsets[0], tuple(sorted(offsets)), annual, annual_unspecified)


__all__ = [
    'MAX_RULE_BYTES',
    'TemporalZoneRule',
    'parse_zone_rule',
]
