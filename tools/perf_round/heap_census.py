"""Pure aggregation for the untimed P0.3 heap census.

The caller owns all I/O and supplies each table's pages in the exact order returned by
``HeapStore.pages_of``.  Page identifiers are opaque labels here: a numeric difference between
two identifiers says nothing about their distance in a table chain because allocations for
different tables and overflow chains may be interleaved.

Only aggregate counts leave this module.  Record identities, physical references, page
identifiers and commit numbers are consumed transiently and never included in a report.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from okto_grafx.domain.ids import (
    NO_CSN,
    NO_PAGE,
    is_committed_csn,
    is_provisional_csn,
)
from okto_grafx.domain.model.record import RecordHeader


class HeapCensusRefused(ValueError):
    """The supplied chain or header population cannot support an exact census."""


@dataclass(frozen=True, slots=True)
class MvccHeaderSummary:
    """Unique physical-header counts under Grafx's maintenance ``live`` predicate."""

    versions_total: int
    live_committed_open: int
    dead_total: int
    dead_committed_ended: int
    dead_no_csn_birth: int
    dead_provisional_birth: int

    def as_dict(self) -> dict[str, int]:
        """Return an identifier-free JSON-compatible projection."""
        return {
            "versions_total": self.versions_total,
            "live_committed_open": self.live_committed_open,
            "dead_total": self.dead_total,
            "dead_committed_ended": self.dead_committed_ended,
            "dead_no_csn_birth": self.dead_no_csn_birth,
            "dead_provisional_birth": self.dead_provisional_birth,
        }


@dataclass(frozen=True, slots=True)
class TailDistanceSummary:
    """Exact nearest-rank summary of chain hops from an observation page to its tail."""

    total: int
    min: int | None
    p50: int | None
    p90: int | None
    p99: int | None
    max: int | None
    last_10_percent: int

    def as_dict(self) -> dict[str, int | float | None]:
        """Return a bounded, page-identifier-free JSON-compatible projection."""
        return {
            "total": self.total,
            "min": self.min,
            "p50": self.p50,
            "p90": self.p90,
            "p99": self.p99,
            "max": self.max,
            "last_10_percent": self.last_10_percent,
            "last_10_percent_ratio": (
                self.last_10_percent / self.total if self.total else None
            ),
        }


class _HeaderAccumulator:
    def __init__(self) -> None:
        self.live = 0
        self.ended = 0
        self.no_csn = 0
        self.provisional = 0

    def add(self, header: RecordHeader) -> bool:
        xmin = header.xmin
        xmax = header.xmax
        if not (is_committed_csn(xmax) or xmax == NO_CSN or is_provisional_csn(xmax)):
            raise HeapCensusRefused("a header carries an invalid xmax stamp")
        if is_committed_csn(xmin):
            if xmax == NO_CSN or is_provisional_csn(xmax):
                self.live += 1
                return True
            self.ended += 1
            return False
        if xmin == NO_CSN:
            self.no_csn += 1
            return False
        if is_provisional_csn(xmin):
            self.provisional += 1
            return False
        raise HeapCensusRefused("a header carries an invalid xmin stamp")

    def summary(self) -> MvccHeaderSummary:
        dead = self.ended + self.no_csn + self.provisional
        return MvccHeaderSummary(
            versions_total=self.live + dead,
            live_committed_open=self.live,
            dead_total=dead,
            dead_committed_ended=self.ended,
            dead_no_csn_birth=self.no_csn,
            dead_provisional_birth=self.provisional,
        )


class _TailAccumulator:
    def __init__(self) -> None:
        self._distances: dict[int, int] = {}
        self._total = 0
        self._last_tenth = 0

    def add_chain(
        self, pages_in_chain_order: Sequence[int], page_weights: Mapping[int, int]
    ) -> None:
        pages = tuple(pages_in_chain_order)
        if not pages:
            if page_weights:
                raise HeapCensusRefused("an empty chain cannot contain observations")
            return
        if any(
            isinstance(page, bool)
            or not isinstance(page, int)
            or page <= 0
            or page == NO_PAGE
            for page in pages
        ):
            raise HeapCensusRefused(
                "a heap chain contains an invalid data-page identifier"
            )
        positions = {page: ordinal for ordinal, page in enumerate(pages)}
        if len(positions) != len(pages):
            raise HeapCensusRefused("a heap chain repeats a page identifier")

        tail_width = max(1, math.ceil(len(pages) * 0.10))
        for page, weight in page_weights.items():
            if (
                isinstance(page, bool)
                or not isinstance(page, int)
                or page <= 0
                or page == NO_PAGE
            ):
                raise HeapCensusRefused(
                    "an observation carries an invalid data-page identifier"
                )
            if page not in positions:
                raise HeapCensusRefused(
                    "an observation page is absent from its table chain"
                )
            if isinstance(weight, bool) or not isinstance(weight, int) or weight < 0:
                raise HeapCensusRefused(
                    "a page observation weight must be a non-negative integer"
                )
            if weight == 0:
                continue
            # This is chain-order arithmetic. Page ids are deliberately never subtracted.
            distance = len(pages) - 1 - positions[page]
            self._distances[distance] = self._distances.get(distance, 0) + weight
            self._total += weight
            if distance < tail_width:
                self._last_tenth += weight

    def summary(self) -> TailDistanceSummary:
        def percentile(fraction: float) -> int | None:
            if not self._total:
                return None
            rank = math.ceil(fraction * self._total)
            seen = 0
            for distance in sorted(self._distances):
                seen += self._distances[distance]
                if seen >= rank:
                    return distance
            raise AssertionError("distance histogram did not reconcile with its total")

        ordered = sorted(self._distances)
        return TailDistanceSummary(
            total=self._total,
            min=ordered[0] if ordered else None,
            p50=percentile(0.50),
            p90=percentile(0.90),
            p99=percentile(0.99),
            max=ordered[-1] if ordered else None,
            last_10_percent=self._last_tenth,
        )


def summarize_tail_distances(
    samples: Iterable[tuple[Sequence[int], Mapping[int, int]]],
) -> TailDistanceSummary:
    """Summarize weighted observations against explicit table-chain order.

    ``page_weights`` may represent workload hits or any other explicitly named population.  The
    output deliberately does not guess that one population is another.
    """
    accumulator = _TailAccumulator()
    for pages, page_weights in samples:
        accumulator.add_chain(pages, page_weights)
    return accumulator.summary()


def summarize_mvcc_headers(headers: Iterable[RecordHeader]) -> MvccHeaderSummary:
    """Classify each supplied physical header exactly once without decoding its payload."""
    accumulator = _HeaderAccumulator()
    for header in headers:
        accumulator.add(header)
    return accumulator.summary()


def summarize_heap_census(
    tables: Iterable[tuple[Sequence[int], Iterable[tuple[int, RecordHeader]]]],
) -> dict[str, Any]:
    """Aggregate unique MVCC headers and current-live-version physical locality.

    The input for each table is ``(pages_of(table), physical_headers)``.  A physical header is a
    ``(page_id, RecordHeader)`` pair yielded once by a header-only walk.  This population is named
    ``live_version_tail_distance_pages`` rather than "hits": actual workload hits require their
    own observed page weights and :func:`summarize_tail_distances`.
    """
    headers = _HeaderAccumulator()
    tails = _TailAccumulator()
    for pages_in_chain_order, observations in tables:
        pages = tuple(pages_in_chain_order)
        # Validate the chain even when it has no live versions. Otherwise a damaged table made
        # solely of dead versions could pass the locality half of the census unnoticed.
        tails.add_chain(pages, {})
        chain_pages = frozenset(pages)
        live_page_weights: dict[int, int] = {}
        for page, header in observations:
            if (
                isinstance(page, bool)
                or not isinstance(page, int)
                or page <= 0
                or page == NO_PAGE
                or page not in chain_pages
            ):
                raise HeapCensusRefused(
                    "a physical header page is absent from its validated table chain"
                )
            if headers.add(header):
                live_page_weights[page] = live_page_weights.get(page, 0) + 1
        tails.add_chain(pages, live_page_weights)
    return {
        "mvcc_headers": headers.summary().as_dict(),
        "live_version_tail_distance_pages": tails.summary().as_dict(),
    }
