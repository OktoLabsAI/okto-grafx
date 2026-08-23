"""The three D5 operations still drive the real engine, and bench/ still ships nowhere.

These are cheap, low-iteration runs. They are not measurements -- a calibration is taken by
``python -m bench.harness`` with proper iteration counts on a quiet machine -- but they are the
only thing that keeps the harness from rotting as the engine it drives changes underneath it.
Each asserts that the operation DID what it claims: a commit that wrote nothing and a replay that
read nothing would both be very fast and completely worthless (A72).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from bench.harness.grafx_ops import (
    DEFAULT_PARTITIONS_PER_TABLE,
    build_stack,
    measure_durable_commit,
    measure_open_with_replay,
    measure_point_read,
)

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
"""The repository root, which holds both ``bench`` and ``pyproject.toml``."""


@pytest.mark.slow
@pytest.mark.timeout(180)
def test_a_durable_commit_is_measured_through_the_real_commit_protocol(tmp_path: Path) -> None:
    """The commit path runs end to end on the shipped adapters and reports durable writes."""
    result = measure_durable_commit(tmp_path, iterations=2, warmup=1)
    assert result.ok, result.unmeasured
    assert result.iterations == 2
    assert result.median > 0
    assert "barrier" in result.detail


@pytest.mark.slow
@pytest.mark.timeout(180)
def test_a_point_read_returns_the_row_it_asked_for(tmp_path: Path) -> None:
    """The read path is asserted by identity, not only by timing."""
    result = measure_point_read(tmp_path, iterations=5, warmup=2, rows=16)
    assert result.ok, result.unmeasured
    assert result.iterations == 5


@pytest.mark.slow
@pytest.mark.timeout(180)
def test_a_replay_reads_every_record_that_was_written(tmp_path: Path) -> None:
    """A replay that saw fewer records than were written is an error, not a fast reading."""
    result = measure_open_with_replay(tmp_path, iterations=2, warmup=1, records=64)
    assert result.ok, result.unmeasured
    assert "LOWER BOUND" in result.detail


@pytest.mark.slow
@pytest.mark.timeout(180)
def test_the_bench_stack_is_the_shipped_stack(tmp_path: Path) -> None:
    """A bench measured on doubles measures the doubles; these are the adapters that ship."""
    stack = build_stack(tmp_path / "db")
    try:
        assert type(stack.device).__name__ == "LocalStorageDevice"
        assert type(stack.coordinator).__name__ == "LocalProcessCoordinator"
        assert type(stack.wal).__name__ == "WalManager"
        assert type(stack.manager).__name__ == "TransactionManager"
        assert stack.manager.partitions_per_table == DEFAULT_PARTITIONS_PER_TABLE
    finally:
        stack.close()


def test_nothing_at_the_repository_root_can_reach_the_wheel() -> None:
    """TR-9: package discovery is confined to src, so bench/ ships in no artefact.

    The artefact itself is asserted by the packaging suite, which builds a wheel from the whole
    tree and demands the top level be exactly the package and its dist-info. This is the cheap
    half: the configuration that makes that true, pinned where the bench lives.
    """
    manifest = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    discovery = manifest["tool"]["setuptools"]["packages"]["find"]
    assert discovery["where"] == ["src"]
    assert discovery["include"] == ["okto_grafx*"]
    assert (PROJECT_ROOT / "bench").is_dir(), "the exclusion is only evidence if bench exists"
    assert not (PROJECT_ROOT / "src" / "okto_grafx" / "bench").exists()


def test_an_engine_that_cannot_be_assembled_is_unmeasured_and_names_the_defect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sibling component that will not construct must not read as a fast operation (A75.2).

    This is C13's own property and it is asserted deterministically rather than waiting for a
    real breakage: the harness measures a tree that ten other components are writing, and the
    difference between "this ceiling was not measured, here is why" and an empty sample list is
    the difference between a finding and a green lie. The reason is carried verbatim, because the
    builder who has to fix it needs the exception, not a paraphrase.
    """

    def refuse(*_arguments: object, **_keywords: object) -> None:
        raise AttributeError("planted: this component does not construct today")

    monkeypatch.setattr("bench.harness.grafx_ops.build_stack", refuse)
    for measurement in (
        measure_durable_commit(tmp_path, iterations=2, warmup=1),
        measure_point_read(tmp_path, iterations=2, warmup=1, rows=4),
    ):
        assert not measurement.ok
        assert measurement.samples == ()
        assert "could not be assembled" in measurement.unmeasured
        assert "planted: this component does not construct today" in measurement.unmeasured
        assert "unmeasured" in measurement.to_dict()


def test_an_unmeasured_side_cannot_produce_a_ceiling_verdict(tmp_path: Path) -> None:
    """The whole point of the previous test: no multiple, and never a passing ceiling."""
    from bench.harness.measure import Measurement, from_samples, ratio

    unmeasured = Measurement(
        name="s", samples=(), warmup=(), unmeasured="the engine could not be assembled"
    )
    item = ratio("point_read", 5.0, unmeasured, from_samples("b", [0.001]))
    assert not item.ok
    assert not item.met
    assert "could not be assembled" in item.unmeasured


def test_the_calibration_records_which_checksum_answered() -> None:
    """The multiple means two different things depending on which implementation was measured.

    On one machine the durable-commit multiple is ~17x with the pure-Python checksum and ~5x with
    the native one (`okto-grafx[accel]`), so an artefact that records the multiple without
    recording this says two different things with one number -- and D5's verdict is read from that
    artefact. The field is descriptive, so it must never be able to break a calibration: an
    implementation that cannot be asked reports why, and the run continues.
    """
    from bench.harness.calibrate import _checksum_implementation

    answered = _checksum_implementation()
    assert answered in {"pure", "native"} or answered.startswith("unknown (")


def test_asking_which_checksum_answered_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import bench.harness.calibrate as calibrate
    import okto_grafx.domain.page.checksum as checksum_module

    def refuse() -> str:
        raise RuntimeError("the implementation cannot be named")

    monkeypatch.setattr(checksum_module, "crc32c_implementation", refuse)
    assert calibrate._checksum_implementation() == "unknown (RuntimeError)"
