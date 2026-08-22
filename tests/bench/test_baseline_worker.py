"""The LadybugDB 0.16 baseline worker, run for real (SPEC-M1 FR-15, IR-1).

This module needs the ``[bench]`` extra and says so with the marker the skip gate checks, so a
run without LadybugDB attributes its own absence rather than vanishing (A54, A54.1). That makes
it the tree's own witness for the cross-family rule: on a job installed WITHOUT the extra the
whole module is skipped at collection, and ``bench.coverage`` then requires it to have run on
some OTHER job of the matrix -- which is exactly why the CI matrix installs the extras on one
profile per family instead of hoping nobody notices.

The baseline is always taken in a child process, so what is asserted here is the parent's
contract: a reading comes back with samples, and a worker that cannot run comes back UNMEASURED
with a reason rather than as an empty, instantaneous result.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ladybug = pytest.importorskip("ladybug")

from bench.harness.calibrate import run_baseline  # noqa: E402

pytestmark = pytest.mark.optional_dependency("ladybug")


@pytest.mark.slow
@pytest.mark.timeout(300)
def test_the_baseline_commit_reading_comes_back_with_samples(tmp_path: Path) -> None:
    """A real LadybugDB auto-commit, timed in a child process."""
    measurement, _ = run_baseline(
        "durable_commit", root=tmp_path / "commit", iterations=3, warmup=1
    )
    assert measurement.ok, measurement.unmeasured
    assert measurement.iterations == 3
    assert measurement.median > 0
    assert len(measurement.warmup) == 1


@pytest.mark.slow
@pytest.mark.timeout(300)
def test_the_baseline_replay_fixture_really_replays(tmp_path: Path) -> None:
    """A72: the crashed database is proved to hold the rows the killed writer wrote."""
    _, crashed = run_baseline(
        "crash", root=tmp_path / "crash", iterations=1, warmup=0, extra=("--records", "32")
    )
    assert crashed.get("records") == 32
    crashed_path = Path(str(crashed["crashed"]))
    assert crashed_path.exists(), "the crashed writer left no database behind"
    measurement, _ = run_baseline(
        "open_replay",
        root=tmp_path / "replay",
        iterations=2,
        warmup=1,
        extra=("--crashed", str(crashed_path), "--records", "32"),
    )
    # The worker refuses to report unless the replayed database holds all 32 rows, so a reading
    # arriving here at all is the fixture proof.
    assert measurement.ok, measurement.unmeasured
    assert measurement.iterations == 2


def test_a_worker_that_cannot_run_is_unmeasured_and_not_zero(tmp_path: Path) -> None:
    """A75.2 through the real spawn path: a refused operation names its reason."""
    measurement, payload = run_baseline(
        "open_replay", root=tmp_path / "nothing", iterations=1, warmup=0, timeout=120
    )
    assert not measurement.ok
    assert measurement.samples == ()
    assert "exited with" in measurement.unmeasured
    assert payload == {}


def test_the_reference_engine_is_the_one_the_extra_pins() -> None:
    """The baseline is only a baseline if it is the engine D5 names."""
    version = str(getattr(ladybug, "__version__", ""))
    assert version.startswith("0.16"), version
