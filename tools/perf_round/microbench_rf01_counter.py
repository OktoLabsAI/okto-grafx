"""Diagnostic only: isolate RF01's per-candidate statistics update cost.

This is not the RF01 temporal qualification. It has no storage reads, so its
ratio cannot predict an end-to-end query ratio. The paired, interleaved samples
only decide whether reducing calls to ``add_work`` merits a focused patch.
"""

from __future__ import annotations

import argparse
import gc
from pathlib import Path
from statistics import median
import sys
from time import perf_counter_ns

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from okto_grafx.engine.statement_work import add_work


def per_candidate(statistics: dict[str, int], candidates: int) -> None:
    for _ in range(candidates):
        add_work(statistics, "landing_candidates")


def batched(statistics: dict[str, int], candidates: int) -> None:
    examined = 0
    try:
        for _ in range(candidates):
            examined += 1
    finally:
        add_work(statistics, "landing_candidates", examined)


def _timed(fn, candidates: int, repetitions: int) -> int:
    statistics: dict[str, int] = {}
    start = perf_counter_ns()
    for _ in range(repetitions):
        fn(statistics, candidates)
    elapsed = perf_counter_ns() - start
    assert statistics == {"landing_candidates": candidates * repetitions}
    return elapsed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=int, default=3840)
    parser.add_argument("--repetitions", type=int, default=100)
    parser.add_argument("--pairs", type=int, default=8)
    args = parser.parse_args()
    if min(args.candidates, args.repetitions, args.pairs) <= 0:
        parser.error("all dimensions must be positive")

    samples: list[tuple[int, int]] = []
    gc.collect()
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        for pair in range(args.pairs):
            order = (per_candidate, batched) if pair % 2 == 0 else (batched, per_candidate)
            elapsed = {fn.__name__: _timed(fn, args.candidates, args.repetitions) for fn in order}
            samples.append((elapsed["per_candidate"], elapsed["batched"]))
    finally:
        if was_enabled:
            gc.enable()
    ratios = [old / new for old, new in samples]
    print({
        "candidates_per_run": args.candidates,
        "repetitions_per_sample": args.repetitions,
        "pairs": args.pairs,
        "median_per_candidate_ns": round(median(old for old, _ in samples)),
        "median_batched_ns": round(median(new for _, new in samples)),
        "median_pair_ratio": round(median(ratios), 3),
        "pair_ratios": [round(value, 3) for value in ratios],
        "meaning": "isolated counter cost only; not RF01 temporal qualification",
    })


if __name__ == "__main__":
    main()
