from __future__ import annotations

from pathlib import Path

import pytest

from tools.measure_control_publication import _nearest_rank, measure, summarize


def test_nearest_rank_and_summary_keep_exact_integer_samples() -> None:
    samples = [5, 1, 4, 2, 3]

    assert _nearest_rank(samples, 0.95) == 5
    assert summarize(samples) == {
        "count": 5,
        "minimum_ns": 1,
        "median_ns": 3,
        "p95_ns": 5,
        "maximum_ns": 5,
    }


@pytest.mark.parametrize("samples", [[], ()])
def test_empty_statistics_are_refused(samples: list[int] | tuple[()]) -> None:
    with pytest.raises(ValueError, match="at least one"):
        summarize(samples)


def test_the_spike_runs_only_in_its_given_root_and_verifies_both_readbacks(
    tmp_path: Path,
) -> None:
    root = tmp_path / "isolated"

    report = measure(root, warmups=1, samples=2, payload_size=16, slot_size=64)

    assert report["schema"] == "okto-grafx.ce1-publication-spike.v1"
    assert report["parameters"]["samples"] == 2
    assert report["atomic_replace"]["total"]["summary"]["count"] == 2
    assert report["two_slot"]["total"]["summary"]["count"] == 2
    assert (root / "atomic-replace" / "control" / "state").is_file()
    assert (root / "two-slot" / "control.state").stat().st_size == 128


def test_impossible_slot_layout_is_refused_before_opening_a_file(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="slot_size"):
        measure(tmp_path, warmups=1, samples=1, payload_size=64, slot_size=32)

    assert list(tmp_path.iterdir()) == []
