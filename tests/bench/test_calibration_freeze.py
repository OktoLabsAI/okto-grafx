"""The frozen calibration, probed on the file that is actually committed (C13).

Every other C13 module tests the machinery: the worker that measures, the wiring that
publishes, the gate that reads. This one tests the *artifact* -- ``bench/calibration.json``
as it sits in the repository -- because that file is now a load-bearing input to CI. After
the freeze the workflow no longer passes ``--recall-target``; the floor comes from
``vector_recall.frozen.target`` in the committed document, so a silent edit to that file
moves a gate that nothing else would notice.

Three failure modes are worth a test here, and each of them is a way the *file* and the
*code* can stop agreeing:

**Drift.** ``ef_search`` is frozen at 320 because that is what was measured; 64, the
pre-calibration default, measures 0.5055 on the full profile. The constant lives in
:mod:`bench.harness.recall_worker` and a copy of it is recorded in the artifact. If one
moves and the other does not, the artifact describes a run that never happened. The
strongest guard is not a comparison of two literals but the validator itself:
``_validate_verdict`` requires a verdict's own ``hnsw`` block to equal ``HNSW_FROZEN``, so
the recorded evidence *stops validating* the moment the constant drifts.

**Shape.** The section was produced by :func:`build_section`, not assembled by hand. A
later hand-edit could add a key, drop one, or change ``metric`` to something the harness
never measured. Feeding the committed values back through the builder and demanding an
exact match is what makes hand-assembly detectable.

**The knob that must not exist.** ``vector_recall_target`` is a configuration field and a
verifiable SLO of the harness. It is NOT a search width: nothing may read it and turn it
into ``ef_search``, because a target that silently widens the search is a target that can
never fail. The last test in this module says so about the source tree itself.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from bench.harness.gate import (
    DEFAULT_RECALL_TARGET,
    STATUS_MET,
    STATUS_UNMEASURED,
    _resolve_recall_target,
    check,
)
from bench.harness.recall import DEFAULT_TARGET, _validate_verdict, build_section
from bench.harness.recall_worker import HNSW_FROZEN

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
"""The repository root, which holds the artifact this module reads."""

CALIBRATION: Path = PROJECT_ROOT / "bench" / "calibration.json"
"""The real committed calibration. Every probe below reads THIS file, never a copy."""

WORKFLOW: Path = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
"""The real CI definition, for the same reason."""

MEASURED_EF_SEARCH: int = 320
"""Named as a literal on purpose: a silent revert to 64 must fail loudly, by number."""

LEGACY_KEYS: frozenset[str] = frozenset(
    {
        "ceilings",
        "environment",
        "exit_code",
        "format",
        "notes",
        "partitions_per_table",
        "status",
        "version",
    }
)
"""What the calibration held BEFORE the freeze. ``vector_recall`` is purely additive."""

RECALL_METRIC: str = "oktografx_vector_recall_ratio"

_ABSOLUTE_PATH = re.compile(r"(?:[A-Za-z]:[\\/])|(?:^/[^/\s]+/)")
"""A Windows drive prefix or a rooted POSIX path -- neither belongs in a committed file."""


@pytest.fixture(scope="module")
def document() -> dict[str, object]:
    """The committed calibration, parsed once."""
    return json.loads(CALIBRATION.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def section(document: dict[str, object]) -> dict[str, object]:
    """The frozen ``vector_recall`` section of the committed calibration."""
    assert "vector_recall" in document, (
        "the freeze landed: the committed calibration carries a vector_recall section, "
        "and CI now takes its recall floor from it"
    )
    return document["vector_recall"]  # type: ignore[return-value]


@pytest.fixture(scope="module")
def home(section: dict[str, object]) -> str:
    """The family that produced the recorded measurement.

    Read from the artifact rather than from :func:`family`, so the probes stay honest on a
    machine whose family is not the one that measured.
    """
    families = list(section["observed"])  # type: ignore[arg-type]
    assert len(families) == 1, f"one measuring family was frozen; got {families}"
    return families[0]


def _verdict_from(section: dict[str, object], home: str) -> dict[str, object]:
    """Rebuild the worker verdict the committed section was built from.

    The section is a projection of a verdict, and every field the validator needs survived
    that projection -- so the artifact can be fed back through the harness's own validator
    instead of being compared against a hand-written expectation. That round trip is what
    makes the drift probe below real: it exercises ``_validate_verdict``, not a literal.
    """
    frozen = section["frozen"]
    observed = section["observed"][home]  # type: ignore[index]
    provenance = section["provenance"][home]  # type: ignore[index]
    return {
        "ok": True,
        "failure": "",
        "profile": provenance["profile"],
        "generator": frozen["corpus"]["generator"],  # type: ignore[index]
        "oracle": frozen["gt"]["oracle"],  # type: ignore[index]
        "gt_path_used": provenance["gt_path_used"],
        "numpy": provenance["numpy"],
        "k": frozen["k"],  # type: ignore[index]
        "queries": frozen["queries"],  # type: ignore[index]
        "corpus_size": frozen["corpus"]["size"],  # type: ignore[index]
        "dimension": frozen["dimension"],  # type: ignore[index]
        "hashes": {
            "corpus_sha256_f64": frozen["corpus"]["sha256_f64"],  # type: ignore[index]
            "corpus_sha256_f32": frozen["corpus"]["sha256_f32"],  # type: ignore[index]
            "query_sha256_f64": frozen["query_set"]["sha256_f64"],  # type: ignore[index]
            "query_sha256_f32": frozen["query_set"]["sha256_f32"],  # type: ignore[index]
        },
        "gauge": observed["mean_recall_at_k"],
        "observed": dict(observed),
        "blas_environment": dict(provenance["blas_environment"]),
        "hnsw": dict(frozen["hnsw"]),  # type: ignore[index]
        "duration_seconds": provenance["duration_seconds"],
        "exit_code": 0,
    }


# =====================================================================================
# Drift: the constant the worker runs and the constant the artifact records
# =====================================================================================


def test_the_committed_hnsw_block_is_the_constant_the_worker_runs(
    section: dict[str, object],
) -> None:
    """The whole block, not just the width: neighbours, ef_construction and the index seed
    describe the index the measurement was taken on, and any of them drifting makes the
    recorded numbers describe a different index."""
    assert section["frozen"]["hnsw"] == dict(HNSW_FROZEN), (  # type: ignore[index]
        "the artifact records the HNSW parameters the worker actually runs"
    )


def test_the_frozen_search_width_is_the_measured_320(
    section: dict[str, object],
) -> None:
    """Named by number so a revert to the pre-calibration 64 cannot pass quietly.

    64 was not merely a different setting: it measured 0.5055 mean recall on the full
    profile, roughly half the frozen 0.90 floor.
    """
    assert HNSW_FROZEN["ef_search"] == MEASURED_EF_SEARCH
    assert section["frozen"]["hnsw"]["ef_search"] == MEASURED_EF_SEARCH  # type: ignore[index]


def test_the_recorded_evidence_stops_validating_when_the_width_drifts(
    section: dict[str, object], home: str
) -> None:
    """The guard that does not depend on anyone remembering to compare two literals.

    ``_validate_verdict`` requires a verdict's ``hnsw`` block to equal ``HNSW_FROZEN``. So
    the recorded measurement is only a valid measurement while the constant stays where it
    was measured -- change the default and the frozen evidence is refused by the harness's
    own validator.

    The positive control matters as much as the refusal: it proves the rebuilt verdict is
    complete, so the refusal below is earned by the width and not by some missing field.
    """
    verdict = _verdict_from(section, home)
    assert _validate_verdict(verdict, verdict["profile"]) is None, (  # type: ignore[arg-type]
        "the committed section round-trips into a verdict the harness accepts"
    )

    drifted = _verdict_from(section, home)
    drifted["hnsw"] = {**dict(section["frozen"]["hnsw"]), "ef_search": 64}  # type: ignore[index]
    reason = _validate_verdict(drifted, verdict["profile"])  # type: ignore[arg-type]
    assert reason is not None, (
        "a verdict measured at another width is not this calibration"
    )
    assert "ef_search" in reason or "hnsw" in reason, (
        f"and the refusal names the parameter that drifted; got {reason!r}"
    )


# =====================================================================================
# Shape: the section is what the builder produces, not hand assembly
# =====================================================================================


def test_the_committed_frozen_block_is_exactly_what_the_builder_produces(
    section: dict[str, object], home: str
) -> None:
    """Feed the committed values back through :func:`build_section` and demand equality.

    Only ``frozen`` is compared: ``observed`` and ``provenance`` are keyed by the family
    running the test, which is not necessarily the family that measured. ``frozen`` is the
    family-independent half, and it is the half the gate and the freeze comparison read.
    """
    rebuilt = build_section(
        _verdict_from(section, home), target=section["frozen"]["target"]
    )  # type: ignore[index]
    assert rebuilt["frozen"] == section["frozen"], (
        "the section came from the harness's builder; a hand-edited key would show here"
    )


def test_the_section_declares_the_schema_and_the_gate_requirement(
    section: dict[str, object],
) -> None:
    """``gate_required`` is what makes the floor mandatory rather than advisory."""
    assert section["schema_version"] == 1
    assert section["frozen"]["gate_required"] is True  # type: ignore[index]
    assert section["frozen"]["metric"] == "cosine"  # type: ignore[index]
    assert section["frozen"]["storage_dtype"] == "float32"  # type: ignore[index]


def test_the_floor_is_never_lowered(section: dict[str, object]) -> None:
    """0.90 is a floor, and a floor that can be edited downward is not one.

    The target may be RAISED by a future calibration; it may never drop below the value
    the project committed to. Written as an inequality on purpose -- an equality would
    make a legitimate raise look like a regression.
    """
    target = section["frozen"]["target"]  # type: ignore[index]
    assert type(target) is float, f"the floor is a plain float; got {target!r}"
    assert target >= DEFAULT_TARGET >= 0.90, (
        f"the frozen floor {target} is below the {DEFAULT_TARGET} the project committed to"
    )
    assert target >= DEFAULT_RECALL_TARGET, "and below the gate's own built-in default"


def test_the_measurement_actually_clears_the_floor_it_freezes(
    section: dict[str, object], home: str
) -> None:
    """A freeze that records a target its own measurement misses is not a calibration."""
    observed = section["observed"][home]  # type: ignore[index]
    assert observed["mean_recall_at_k"] >= section["frozen"]["target"], (  # type: ignore[index]
        "the frozen evidence clears the frozen floor"
    )


# =====================================================================================
# The artifact governs the gate
# =====================================================================================


def test_the_gate_takes_its_floor_from_the_committed_artifact() -> None:
    """With no flag, the floor comes from the file -- and the gate says where it came from.

    This is the whole point of dropping ``--recall-target`` from the workflow: the origin
    string is what a CI log shows, and ``frozen in ...`` is how a reader knows the
    versioned artifact governed rather than a built-in nobody chose.
    """
    floor, origin = _resolve_recall_target(None, str(CALIBRATION))
    assert (
        floor
        == json.loads(CALIBRATION.read_text(encoding="utf-8"))["vector_recall"][
            "frozen"
        ]["target"]
    )
    assert origin == f"frozen in {CALIBRATION}", origin


def _metrics(recall: float | None) -> str:
    """A published metrics document, optionally carrying the recall gauge."""
    entries: list[dict[str, object]] = [
        {
            "name": "oktografx_baseline_ceiling_multiple",
            "samples": [
                {"value": 1.0, "labels": {"ceiling": "durable_commit"}},
                {"value": 1.0, "labels": {"ceiling": "point_read"}},
                {"value": 1.0, "labels": {"ceiling": "open_replay"}},
            ],
        }
    ]
    if recall is not None:
        entries.append(
            {
                "name": RECALL_METRIC,
                "kind": "gauge",
                "unit": "ratio",
                "samples": [{"value": recall}],
            }
        )
    return json.dumps({"metrics": entries})


def test_the_floor_from_the_artifact_is_the_one_the_gate_applies(
    section: dict[str, object],
) -> None:
    """Resolution and application are two steps, and the probe walks both.

    A floor read correctly and then applied against a different number would still be a
    broken gate, so the resolved value is fed to :func:`check` and the verdict is taken
    just below and just at the boundary.
    """
    floor, _ = _resolve_recall_target(None, str(CALIBRATION))
    assert floor == section["frozen"]["target"]  # type: ignore[index]

    below = check(_metrics(floor - 0.01), recall_target=floor, require_recall=True)
    assert below.status != STATUS_MET, "a run under the frozen floor does not pass"

    at = check(_metrics(floor), recall_target=floor, require_recall=True)
    assert at.status == STATUS_MET, "and the floor itself is passing, not failing"


def test_a_build_that_publishes_no_gauge_is_unmeasured_not_passing(
    section: dict[str, object],
) -> None:
    """``--require-recall`` is what makes an absent measurement a failure.

    The gauge is published LAST, so an interrupted run leaves the section without it. That
    state must read as UNMEASURED -- never as a silent pass.
    """
    floor, _ = _resolve_recall_target(None, str(CALIBRATION))
    verdict = check(_metrics(None), recall_target=floor, require_recall=True)
    assert verdict.status == STATUS_UNMEASURED


def test_the_workflow_lets_the_versioned_artifact_govern() -> None:
    """The workflow's gate invocation, read out of the real file.

    Before the freeze the step carried ``--recall-target 0.90`` because the committed
    calibration had no frozen target and the gate refuses to guess. That override is gone:
    keeping it would mean the floor lives in a workflow line instead of in a reviewed,
    versioned file, and the two could disagree without anyone seeing it.
    """
    invocations = [
        line
        for line in WORKFLOW.read_text(encoding="utf-8").splitlines()
        if "bench.harness.gate" in line and not line.lstrip().startswith("#")
    ]
    assert invocations, "the workflow still runs the gate"
    for line in invocations:
        assert "--recall-target" not in line, (
            f"the floor comes from the artifact, not from a flag; got {line.strip()!r}"
        )
        assert "--require-recall" in line, "and the stage stays fail-closed"
        assert "--calibration" in line, "and the artifact is named explicitly"


# =====================================================================================
# The additive section, and the knob that must not exist
# =====================================================================================


def test_the_legacy_calibration_survives_the_additive_section(
    document: dict[str, object],
) -> None:
    """``vector_recall`` was ADDED. Nothing the calibration already carried was disturbed."""
    assert LEGACY_KEYS <= set(document), (
        f"missing legacy keys: {sorted(LEGACY_KEYS - set(document))}"
    )
    assert set(document) == LEGACY_KEYS | {"vector_recall"}, (
        f"unexpected additions: {sorted(set(document) - LEGACY_KEYS - {'vector_recall'})}"
    )


def test_the_provenance_records_the_evidence_without_absolute_paths(
    section: dict[str, object], home: str
) -> None:
    """Provenance says which measurement this is, in terms that survive leaving this machine.

    Hashes, versions and source commits identify the run; a path from the machine that
    produced it identifies nothing to anyone else and is exactly what the handoff forbids
    in a commit. Provenance sits outside every hash and comparison -- the deterministic
    projection covers ``frozen`` and ``observed`` only -- so recording it cannot move a
    gate decision either way.
    """
    provenance = section["provenance"]
    assert set(provenance) == {home}, "provenance stays keyed by family alone"  # type: ignore[arg-type]
    frozen_from = provenance[home]["frozen_from"]  # type: ignore[index]
    assert len(frozen_from["raw_sha256"]) == 64  # type: ignore[index]
    assert frozen_from["measured_ef_search"] == MEASURED_EF_SEARCH  # type: ignore[index]

    def walk(value: object, trail: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, f"{trail}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{trail}[{index}]")
        elif isinstance(value, str):
            assert not _ABSOLUTE_PATH.search(value), (
                f"{trail} carries a machine-local path: {value!r}"
            )

    walk(section, "vector_recall")


def test_no_source_turns_the_configured_target_into_a_search_width() -> None:
    """``vector_recall_target`` is an SLO the harness verifies, never a knob the engine reads.

    Wiring it to ``ef_search`` would make the target self-fulfilling: ask for more recall
    and the search silently widens until it is met, so the number could never fail and
    would stop meaning anything. The invariant is stated over the source tree rather than
    over one file, because the drift this prevents is a future edit somewhere else.

    Scoped to ``src/`` deliberately. This module and the worker BOTH mention the two names
    -- in prose, saying they must stay apart -- and a check that could not tell an
    explanation from a wiring would either fail on its own documentation or be quietly
    weakened until it caught nothing.
    """
    offenders = [
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in (PROJECT_ROOT / "src").rglob("*.py")
        if "vector_recall_target" in (text := path.read_text(encoding="utf-8"))
        and "ef_search" in text
    ]
    assert offenders == [], (
        f"these sources mention both the SLO and the search width: {offenders}"
    )
