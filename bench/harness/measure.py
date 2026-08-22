"""Timing primitives: a measurement is a distribution, never one number.

Rules this module encodes, so that a reading here can be reproduced or refuted:

* **Warm-up is discarded, not averaged in.** The first iterations of anything on this stack pay
  for imports, page allocation, a cold file system cache and a cold branch predictor. They are
  measured and reported separately rather than deleted, so a reader can see how big the warm-up
  effect was instead of taking it on trust.
* **Every sample is kept.** The record carries the whole sample list, so median, spread and
  outliers are recomputable from the artefact without re-running anything.
* **The headline is the MEDIAN, and it never travels alone.** A ratio of two medians is the
  D5 multiple; the ratio of the two 95th percentiles and the ratio of the two minima travel with
  it, because a ceiling met at the median and missed at the tail is a different fact from a
  ceiling met everywhere.
* **A failed measurement is UNMEASURED.** Zero samples is not a fast operation (A75.2), so
  :class:`Measurement` refuses to report a statistic it does not have and :class:`Ratio` carries
  the reason instead of a number.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

__all__ = ["Measurement", "Ratio", "from_samples", "measure", "ratio"]


@dataclass(frozen=True, slots=True)
class Measurement:
    """One operation, timed many times, with everything needed to recompute the statistics."""

    name: str
    """What was measured, in en-US, as it appears in the artefact."""

    samples: tuple[float, ...]
    """Every kept sample, in seconds, in the order taken."""

    warmup: tuple[float, ...]
    """The discarded warm-up samples, in seconds, kept so the discard can be judged."""

    unmeasured: str = ""
    """Why there is nothing to report, when there is nothing to report."""

    detail: str = ""
    """One line about what the operation actually did, for the artefact."""

    @property
    def ok(self) -> bool:
        """Return True when this measurement holds samples and may be reported."""
        return not self.unmeasured and len(self.samples) > 0

    @property
    def iterations(self) -> int:
        """Return how many samples were kept."""
        return len(self.samples)

    @property
    def median(self) -> float:
        """Return the median sample in seconds; the headline of every ratio."""
        return statistics.median(self.samples)

    @property
    def mean(self) -> float:
        """Return the arithmetic mean in seconds."""
        return statistics.fmean(self.samples)

    @property
    def stdev(self) -> float:
        """Return the sample standard deviation in seconds, or 0.0 for a single sample."""
        return statistics.stdev(self.samples) if len(self.samples) > 1 else 0.0

    @property
    def minimum(self) -> float:
        """Return the fastest sample in seconds."""
        return min(self.samples)

    @property
    def maximum(self) -> float:
        """Return the slowest sample in seconds."""
        return max(self.samples)

    @property
    def p95(self) -> float:
        """Return the 95th percentile by nearest rank, which needs no interpolation to defend."""
        ordered = sorted(self.samples)
        rank = max(1, int(round(0.95 * len(ordered))))
        return ordered[min(rank, len(ordered)) - 1]

    @property
    def relative_spread(self) -> float:
        """Return the coefficient of variation, the one-number answer to "how noisy was this"."""
        centre = self.mean
        return self.stdev / centre if centre > 0 else 0.0

    def to_dict(self) -> dict[str, object]:
        """Return the measurement as plain data, samples included."""
        if not self.ok:
            return {"name": self.name, "unmeasured": self.unmeasured, "detail": self.detail}
        return {
            "name": self.name,
            "detail": self.detail,
            "iterations": self.iterations,
            "warmup_discarded": len(self.warmup),
            "median_seconds": self.median,
            "mean_seconds": self.mean,
            "stdev_seconds": self.stdev,
            "min_seconds": self.minimum,
            "max_seconds": self.maximum,
            "p95_seconds": self.p95,
            "relative_spread": self.relative_spread,
            "samples_seconds": list(self.samples),
            "warmup_seconds": list(self.warmup),
        }


@dataclass(frozen=True, slots=True)
class Ratio:
    """One D5 multiple: how many times slower Okto Grafx is than the reference."""

    ceiling: str
    """The D5 ceiling this ratio answers to: ``durable_commit``, ``point_read`` or ``open_replay``."""

    limit: float
    """The multiple D5 allows."""

    subject: Measurement
    """The Okto Grafx measurement."""

    baseline: Measurement
    """The LadybugDB 0.16 measurement."""

    @property
    def ok(self) -> bool:
        """Return True when both sides were measured and a multiple exists."""
        return self.subject.ok and self.baseline.ok and self.baseline.median > 0

    @property
    def unmeasured(self) -> str:
        """Return why no multiple exists, or an empty string."""
        if self.subject.unmeasured:
            return f"okto grafx side unmeasured: {self.subject.unmeasured}"
        if self.baseline.unmeasured:
            return f"baseline side unmeasured: {self.baseline.unmeasured}"
        if not self.subject.ok or not self.baseline.ok:
            return "one side produced no sample"
        if self.baseline.median <= 0:
            return "the baseline median is zero, so no multiple is defined"
        return ""

    @property
    def multiple(self) -> float:
        """Return the ratio of the medians, which is the number D5 speaks about."""
        return self.subject.median / self.baseline.median

    @property
    def multiple_p95(self) -> float:
        """Return the ratio of the 95th percentiles, so a tail regression is visible."""
        return self.subject.p95 / self.baseline.p95 if self.baseline.p95 > 0 else 0.0

    @property
    def multiple_best(self) -> float:
        """Return the ratio of the fastest samples, the most favourable honest reading."""
        return self.subject.minimum / self.baseline.minimum if self.baseline.minimum > 0 else 0.0

    @property
    def met(self) -> bool:
        """Return True when the median multiple is within the D5 ceiling."""
        return self.ok and self.multiple <= self.limit

    def to_dict(self) -> dict[str, object]:
        """Return the ratio as plain data, both sides included."""
        payload: dict[str, object] = {
            "ceiling": self.ceiling,
            "limit": self.limit,
            "subject": self.subject.to_dict(),
            "baseline": self.baseline.to_dict(),
        }
        if self.ok:
            payload.update(
                {
                    "multiple": self.multiple,
                    "multiple_p95": self.multiple_p95,
                    "multiple_best": self.multiple_best,
                    "met": self.met,
                }
            )
        else:
            payload["unmeasured"] = self.unmeasured
        return payload


def measure(
    operation: Callable[[int], None],
    *,
    name: str,
    iterations: int,
    warmup: int,
    detail: str = "",
) -> Measurement:
    """Time ``operation`` ``warmup + iterations`` times and return the kept samples.

    The operation receives the iteration index, so an operation that must not repeat itself --
    a commit of a new row, a read of a different key -- can vary with it. Any exception is
    caught and returned as UNMEASURED: a harness that dies half way through must not leave a
    partial sample list looking like a fast result.
    """
    warmup_samples: list[float] = []
    samples: list[float] = []
    try:
        for index in range(warmup):
            start = time.perf_counter()
            operation(index)
            warmup_samples.append(time.perf_counter() - start)
        for index in range(iterations):
            start = time.perf_counter()
            operation(warmup + index)
            samples.append(time.perf_counter() - start)
    except Exception as error:  # noqa: BLE001 - the reason is the result here
        return Measurement(
            name=name,
            samples=(),
            warmup=tuple(warmup_samples),
            unmeasured=f"{type(error).__name__}: {error}",
            detail=detail,
        )
    return Measurement(
        name=name, samples=tuple(samples), warmup=tuple(warmup_samples), detail=detail
    )


def from_samples(
    name: str, samples: Sequence[float], *, warmup: Sequence[float] = (), detail: str = "", unmeasured: str = ""
) -> Measurement:
    """Return a measurement built from samples taken elsewhere, for example in a subprocess."""
    return Measurement(
        name=name,
        samples=tuple(samples),
        warmup=tuple(warmup),
        detail=detail,
        unmeasured=unmeasured,
    )


def ratio(ceiling: str, limit: float, subject: Measurement, baseline: Measurement) -> Ratio:
    """Return the D5 multiple of one operation against its baseline."""
    return Ratio(ceiling=ceiling, limit=limit, subject=subject, baseline=baseline)
