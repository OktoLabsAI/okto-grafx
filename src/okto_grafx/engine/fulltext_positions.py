"""Operation-owned positional evidence; never substitutes for heap/snapshot validation."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from bisect import bisect_right
from okto_grafx.domain.ids import RecordRef
from okto_grafx.domain.index.entry import IndexEntry

__all__ = ["PositionEvidence", "phrase_fields"]

from okto_grafx.domain.errors import GrafxCorruptionDetected
from okto_grafx.domain.index.text_positions import CHUNK, decode_position


def _refuse():
    return GrafxCorruptionDetected("Positional postings disagree with complete visible term coverage.", field="text_positions")


class PositionEvidence:
    """Capture bounded chunks for one term within the parent index read certificate."""

    def __init__(self, term: str, charge: Callable[[int], None]) -> None:
        self.term = term
        self.charge = charge
        self.expected = {}
        self.seen = {}

    def add(self, entry: IndexEntry, fields: Sequence[Sequence[str]]) -> None:
        """Validate exact chunk identity/positions against the same visible native heap row."""
        term, field, chunk, length, positions = decode_position(entry.key)
        if term != self.term or field >= len(fields) or length != len(fields[field]):
            raise _refuse()
        key = (entry.ref, field)
        expected = self.expected.get(key)
        if expected is None:
            self.charge(128 + 8 * len(fields[field]))
            expected = tuple(index for index, token in enumerate(fields[field]) if token == term)
            self.expected[key] = expected
        if positions != expected[chunk * CHUNK:(chunk + 1) * CHUNK]:
            raise _refuse()
        self.charge(128 + 8 * len(positions))
        chunks = self.seen.setdefault(key, {})
        if chunk in chunks:
            raise _refuse()
        chunks[chunk] = positions

    def finish(self, documents: Mapping[int, tuple[RecordRef, Sequence[Sequence[str]]]]) -> dict[int, tuple[tuple[int, ...], ...]]:
        """Refuse missing chunks/lexical counterparts; return actual validated persisted positions."""
        covered = set()
        result = {}
        for rid, (ref, fields) in documents.items():
            per_field = []
            for field, tokens in enumerate(fields):
                expected = tuple(index for index, token in enumerate(tokens) if token == self.term)
                key = (ref, field)
                chunks = self.seen.get(key, {})
                if set(chunks) != set(range((len(expected) + CHUNK - 1) // CHUNK)):
                    raise _refuse()
                actual = tuple(position for index in sorted(chunks) for position in chunks[index])
                if actual != expected:
                    raise _refuse()
                if chunks:
                    covered.add(key)
                per_field.append(actual)
            result[rid] = tuple(per_field)
        if covered != set(self.seen):
            raise _refuse()
        return result


def phrase_fields(postings: Mapping[str, Sequence[Sequence[int]]], ordered_terms: Sequence[str], width: int,
                  *, slop: int = 0, work: Callable[[int], None] | None = None) -> tuple[bool, ...]:
    """Ordered proximity: total intervening tokens <= slop, strict increasing positions per field."""
    result = []
    for field in range(width):
        if slop:
            lists = [postings.get(term, ((),) * width)[field] for term in ordered_terms]
            matched = False
            if lists and all(lists):
                for start in lists[0]:
                    previous = start
                    if work is not None:
                        work(1)
                    for positions in lists[1:]:
                        if work is not None:
                            work(1 + len(positions).bit_length())
                        index = bisect_right(positions, previous)
                        if index == len(positions):
                            break
                        previous = positions[index]
                        if previous - start > slop + len(lists) - 1:
                            break
                    else:
                        matched = True
                        break
            result.append(matched)
            continue
        starts = None
        for offset, term in enumerate(ordered_terms):
            positions = postings.get(term)
            candidates = set() if positions is None else {position - offset for position in positions[field] if position >= offset}
            starts = candidates if starts is None else starts.intersection(candidates)
            if not starts:
                break
        result.append(bool(starts))
    return tuple(result)
