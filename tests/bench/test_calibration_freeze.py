"""The frozen calibration, probed on the files that are actually committed (C13).

Every other C13 module tests the machinery: the worker that measures, the wiring that
publishes, the gate that reads. This one tests the *artifacts* -- ``bench/calibration.json``
and the evidence under ``bench/evidence/`` -- because after the freeze those files are
load-bearing inputs to CI: the workflow no longer passes ``--recall-target``, so the floor
comes from the committed document and a silent edit moves a gate nothing else would notice.

The first version of this module was audited and REJECTED, and the reasons are worth
keeping because each one is a way a test can look like a guard without being one:

* it asserted ``len(raw_sha256) == 64``, which ``"Z" * 64`` satisfies, so the recorded
  evidence hash proved nothing about the evidence;
* it rebuilt a verdict with a literal ``exit_code: 0``, so the value under test was written
  by the test itself and provenance never carried it at all;
* its absolute-path regex missed UNC roots and single-segment POSIX roots;
* its "no knob" scan was textual, so wiring split across two files survived while a comment
  naming both symbols failed the build;
* its CI check read lines, so flags arriving through a shell variable, or a
  ``--require-recall`` sitting inside a trailing comment, both passed.

The rule those five share: **a test must recompute the fact, not restate it.** Every hash
below is recomputed from the bytes it claims to describe, every identity is recomputed from
file content, and every structural claim is made against a parse rather than a substring.

One portability note that decides how the hashes are taken. Git stores text blobs with LF
while a Windows checkout materializes CRLF, so ``sha256(path.read_bytes())`` is green on the
machine that wrote it and red on the Linux runner where the gate actually executes. Every
hash here is taken over LF-normalized content, which is also exactly what ``git hash-object``
sees -- so the same expression yields the blob identity and the content hash on both
platforms.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import shlex
import subprocess
from pathlib import Path, PurePosixPath, PureWindowsPath

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
"""The repository root, which holds every artifact this module reads."""

CALIBRATION: Path = PROJECT_ROOT / "bench" / "calibration.json"
WORKFLOW: Path = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
EVIDENCE: Path = PROJECT_ROOT / "bench" / "evidence"
RAW_VERDICT: Path = EVIDENCE / "recall-full-ef320.verdict.json"
RAW_B64: Path = EVIDENCE / "recall-full-ef320.verdict.json.b64"
RAW_METADATA: Path = EVIDENCE / "recall-full-ef320.metadata.json"

WORKER_RELATIVE: str = "bench/harness/recall_worker.py"
MEASURED_EF_SEARCH: int = 320
"""Named by number on purpose: a silent revert to the pre-calibration 64 must fail loudly."""

RECALL_METRIC: str = "oktografx_vector_recall_ratio"
HEX64 = frozenset("0123456789abcdef")

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

CONFIG_RELATIVE: str = "src/okto_grafx/runtime/config.py"
KNOB: str = "vector_recall_target"


# ------------------------------------------------------------------------------------
# helpers -- each one RECOMPUTES something rather than restating it
# ------------------------------------------------------------------------------------


def _raw_bytes(path: Path) -> bytes:
    """The bytes exactly as they are. Evidence is bytes, and nothing normalizes them.

    ``bench/evidence/.gitattributes`` marks that directory ``-text`` precisely so this is
    the same sequence on a Windows checkout and on the Linux runner.
    """
    return path.read_bytes()


def _raw_sha256(path: Path) -> str:
    """Hash of the exact bytes -- the convention reserved for EVIDENCE."""
    return hashlib.sha256(_raw_bytes(path)).hexdigest()


def _canonical_bytes(path: Path) -> bytes:
    """LF-normalized content: what git stores, and the portable identity of SOURCE."""
    return path.read_bytes().replace(b"\r\n", b"\n")


def _canonical_blob_sha256(path: Path) -> str:
    """Hash of LF-normalized content -- the convention reserved for SOURCE integrity.

    Kept rigidly apart from :func:`_raw_sha256`. Normalizing evidence would make its hash
    describe something other than what the worker wrote; not normalizing source would make
    a blob id unreproducible the moment the checkout changed line endings.
    """
    return hashlib.sha256(_canonical_bytes(path)).hexdigest()


def _object_format() -> str:
    return subprocess.run(
        ["git", "rev-parse", "--show-object-format"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    ).stdout.strip()


def _git_blob(path: Path) -> str:
    """The git blob id of this file's content, computed without invoking git.

    ``git hash-object`` over normalized content is exactly sha1("blob <len>\0" + content),
    so the identity a commit records can be recomputed from the working file on any
    platform. That is what ties provenance to the code that ran instead of to a promise.

    The formula is sha1-specific, so the repository's object format is checked rather than
    assumed: under a sha256 migration this arithmetic would still produce a number, it
    would simply be the wrong one, and the resulting failure would look like drift in the
    worker instead of a changed hash algorithm.
    """
    fmt = _object_format()
    assert fmt == "sha1", (
        f"this identity check reproduces sha1 blob ids; the repository uses {fmt!r} -- "
        "update the formula rather than reading the mismatch as a source change"
    )
    data = _canonical_bytes(path)
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _require_hex64(value: object, what: str) -> str:
    """A hash is 64 LOWERCASE hex characters -- not merely 64 characters long.

    The audited version checked the length alone, and "Z" * 64 passed it.
    """
    assert type(value) is str, f"{what} must be a plain string; got {value!r}"
    assert len(value) == 64, f"{what} must be 64 characters; got {len(value)}"
    assert set(value) <= HEX64, f"{what} must be lowercase hex; got {value!r}"
    return value


def _walk_strings(value: object, trail: str):
    """Yield every (trail, string) in a nested structure."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_strings(item, f"{trail}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_strings(item, f"{trail}[{index}]")
    elif isinstance(value, str):
        yield trail, value


def _is_absolute_anywhere(value: str) -> bool:
    """True if ANY platform would read this as rooted.

    Both flavours are asked, because neither alone is enough: PureWindowsPath knows drive
    letters and UNC roots (\\\\server\\share) and does not consider "/artifact" rooted,
    while PurePosixPath knows "/artifact" and does not know drives. The audited regex
    implemented half of one of them.
    """
    if not value:
        return False
    return PureWindowsPath(value).is_absolute() or PurePosixPath(value).is_absolute()


def _knob_occurrences(tree: ast.AST) -> list[ast.AST]:
    """Structural references to the knob: names, attributes, args and string field labels.

    Comments and docstrings do not appear here at all -- a parse cannot see a comment, and
    docstrings are skipped explicitly -- which is the difference between this and the
    textual scan that failed a build over a sentence.
    """
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = getattr(node, "body", ())
            if body and isinstance(body[0], ast.Expr):
                first = body[0].value
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    docstrings.add(id(first))
    found: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == KNOB:
            found.append(node)
        elif isinstance(node, ast.Attribute) and node.attr == KNOB:
            found.append(node)
        elif isinstance(node, ast.arg) and node.arg == KNOB:
            found.append(node)
        elif isinstance(node, ast.keyword) and node.arg == KNOB:
            found.append(node)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value == KNOB
            and id(node) not in docstrings
        ):
            found.append(node)
    return found


def _gate_command() -> list[str]:
    """The EFFECTIVE argv of the workflow's gate step, tokenized.

    Reading lines was the audited mistake: it could not tell a flag from a flag inside a
    trailing comment, and it could not see flags arriving through a shell variable at all.
    This finds the step's run scalar, drops comment lines, joins continuations and hands the
    result to shlex, so what is asserted is the command the runner executes.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if "bench.harness.gate" not in line:
            continue
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if not stripped.startswith("run:"):
            pytest.fail(f"the gate invocation is not a run: scalar -- {stripped!r}")
        script = stripped[len("run:") :].strip()
        while script.endswith("\\") and index + 1 < len(lines):
            index += 1
            script = script[:-1] + lines[index].strip()
        # A trailing comment is not part of the command.
        script = script.split(" #", 1)[0]
        return shlex.split(script, posix=False)
    pytest.fail("the workflow no longer invokes bench.harness.gate")
    raise AssertionError("unreachable")


# ------------------------------------------------------------------------------------
# fixtures
# ------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def document() -> dict[str, object]:
    return json.loads(CALIBRATION.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def section(document: dict[str, object]) -> dict[str, object]:
    assert "vector_recall" in document, (
        "the freeze landed: the committed calibration carries a vector_recall section, and "
        "CI now takes its recall floor from it"
    )
    return document["vector_recall"]  # type: ignore[return-value]


@pytest.fixture(scope="module")
def home(section: dict[str, object]) -> str:
    families = list(section["observed"])  # type: ignore[arg-type]
    assert len(families) == 1, f"one measuring family was frozen; got {families}"
    return families[0]


@pytest.fixture(scope="module")
def frozen_from(section: dict[str, object], home: str) -> dict[str, object]:
    return section["provenance"][home]["frozen_from"]  # type: ignore[index]


@pytest.fixture(scope="module")
def raw_verdict() -> dict[str, object]:
    return json.loads(RAW_VERDICT.read_text(encoding="utf-8"))


def _verdict_from(section: dict[str, object], home: str) -> dict[str, object]:
    """Rebuild the worker verdict the committed section was projected from.

    Nothing is invented here. ``exit_code`` and ``duration_seconds`` come from provenance,
    which is the whole point: the audited version wrote ``"exit_code": 0`` as a literal, so
    the field under test was authored by the test and the artifact never carried it.
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
        "duration_seconds": provenance["frozen_from"]["duration_seconds"],
        "exit_code": provenance["frozen_from"]["exit_code"],
    }


# =====================================================================================
# Identity: the provenance names the code that actually ran
# =====================================================================================


def test_the_provenance_names_the_worker_blob_that_is_committed_here(
    frozen_from: dict[str, object],
) -> None:
    """The recorded worker identity is recomputed from the file, not taken on faith.

    The first freeze recorded ``source_commits.c13 = c7a493``, a commit whose worker carries
    ef_search 64 -- it could not have produced a 320 measurement. The provenance named a
    commit instead of identifying the code. This recomputes the blob id of the worker in
    this tree and demands the artifact agree, so editing the worker without re-measuring
    turns the evidence red.
    """
    recorded = frozen_from["runner_source"]
    assert recorded["path"] == WORKER_RELATIVE  # type: ignore[index]
    computed = _git_blob(PROJECT_ROOT / WORKER_RELATIVE)
    assert recorded["git_blob"] == computed, (  # type: ignore[index]
        "the worker recorded in provenance is not the worker in this tree: "
        f"{recorded['git_blob']} vs {computed}"  # type: ignore[index]
    )
    _require_hex64(recorded["canonical_blob_sha256"], "the worker content hash")  # type: ignore[index]
    assert recorded["canonical_blob_sha256"] == _canonical_blob_sha256(  # type: ignore[index]
        PROJECT_ROOT / WORKER_RELATIVE
    )
    assert len(recorded["commit"]) == 40  # type: ignore[index,arg-type]


def test_the_engine_is_a_dependency_and_never_an_ancestor_of_the_runner(
    frozen_from: dict[str, object],
) -> None:
    """The measurement needed a vector engine that is NOT in the runner's history.

    Pretending otherwise is what made the first freeze unauditable. Two details decide
    whether this test stays true:

    The claim is stated and verified against the RUNNER COMMIT, never against HEAD. After
    the branch is integrated, the engine commit becomes an ancestor of HEAD through the
    other parent -- so a check phrased against HEAD would flip from passing to failing on a
    merge, reporting a defect where nothing had changed about the measurement.

    And the declared field is asserted unconditionally while the git check only strengthens
    it: a fresh CI clone may not carry that object, and a test that silently skipped there
    would be a guard that never fires where it matters.
    """
    engine = frozen_from["engine_dependency_source"]
    runner = frozen_from["runner_source"]
    assert str(engine["path"]).endswith("hnsw.py")  # type: ignore[index]
    _require_hex64(engine["canonical_blob_sha256"], "the engine content hash")  # type: ignore[index]
    assert engine["proven_against_committed_blob"] is True, (  # type: ignore[index]
        "the engine that loaded was never proven against the committed blob"
    )
    assert engine["loaded_canonical_sha256"] == engine["canonical_blob_sha256"], (  # type: ignore[index]
        "the module that loaded is not the module the artifact names"
    )
    assert frozen_from["engine_is_ancestor_of_runner_commit"] is False, (
        "the engine commit is a separate integration dependency, never an ancestor"
    )
    assert "not an ancestor" in str(frozen_from["engine_relationship"]).lower()

    probe = subprocess.run(
        [
            "git",
            "merge-base",
            "--is-ancestor",
            str(engine["commit"]),  # type: ignore[index]
            str(runner["commit"]),  # type: ignore[index]
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
    )
    if probe.returncode in (0, 1):
        assert probe.returncode == 1, (
            "the engine commit IS an ancestor of the runner commit, so the artifact lies"
        )


def test_the_frozen_search_width_is_the_measured_320(
    section: dict[str, object], frozen_from: dict[str, object]
) -> None:
    """One number, asserted in the three places that must never disagree."""
    assert HNSW_FROZEN["ef_search"] == MEASURED_EF_SEARCH
    assert section["frozen"]["hnsw"]["ef_search"] == MEASURED_EF_SEARCH  # type: ignore[index]
    assert frozen_from["measured_ef_search"] == MEASURED_EF_SEARCH


def test_the_committed_hnsw_block_is_the_constant_the_worker_runs(
    section: dict[str, object],
) -> None:
    """The whole block: neighbours, ef_construction and the seed describe the same index."""
    assert section["frozen"]["hnsw"] == dict(HNSW_FROZEN)  # type: ignore[index]


def test_the_recorded_evidence_stops_validating_when_the_width_drifts(
    section: dict[str, object], home: str
) -> None:
    """The guard that does not rely on anyone remembering to compare two literals.

    ``_validate_verdict`` requires a verdict's hnsw block to equal ``HNSW_FROZEN``, so the
    committed section is rebuilt into a verdict and handed back to the harness's own
    validator. The positive control matters as much as the refusal: it proves the rebuilt
    verdict is complete, so the refusal below is earned by the width and not by a field the
    rebuild forgot.
    """
    verdict = _verdict_from(section, home)
    assert _validate_verdict(verdict, verdict["profile"]) is None  # type: ignore[arg-type]

    drifted = _verdict_from(section, home)
    drifted["hnsw"] = {**dict(section["frozen"]["hnsw"]), "ef_search": 64}  # type: ignore[index]
    reason = _validate_verdict(drifted, verdict["profile"])  # type: ignore[arg-type]
    assert reason is not None, (
        "a verdict measured at another width is not this calibration"
    )
    assert "ef_search" in reason or "hnsw" in reason, reason


# =====================================================================================
# The evidence really is the evidence
# =====================================================================================


def test_the_versioned_evidence_hashes_to_what_provenance_records(
    frozen_from: dict[str, object],
) -> None:
    """Recompute, do not restate.

    The audited version asserted only that the recorded hash was 64 characters long, which
    ``"Z" * 64`` satisfies and which says nothing about any file. This hashes the committed
    evidence and demands the artifact agree, so editing one observed digit in the raw
    verdict -- or the hash that claims to cover it -- goes red.
    """
    evidence = frozen_from["evidence"]
    for path, key, what in (
        (RAW_VERDICT, "raw_sha256", "the raw verdict"),
        (RAW_METADATA, "metadata_raw_sha256", "the capture metadata"),
    ):
        assert path.exists(), f"{what} must be versioned at {path}"
        recorded = _require_hex64(evidence[key], f"the recorded hash of {what}")  # type: ignore[index]
        assert recorded == _raw_sha256(path), (
            f"{what} does not hash to what provenance records"
        )
    assert evidence["verdict_bytes"] == len(_raw_bytes(RAW_VERDICT))  # type: ignore[index]


def test_the_base64_envelope_reproduces_the_verdict_byte_for_byte() -> None:
    """A second, transport-proof copy of the measurement.

    ``.gitattributes`` should keep the JSON byte-exact, but "should" is a configuration
    that a future checkout, export or patch pipeline can quietly undo -- and if it does,
    ``raw_sha256`` stops describing the file it names. The envelope cannot be rewritten by
    a line-ending rule, so decoding it and comparing byte for byte is what makes the exact
    measurement recoverable no matter what happened to the readable copy.
    """
    assert RAW_B64.exists(), "the byte-exact envelope must be versioned"
    decoded = base64.b64decode(RAW_B64.read_bytes(), validate=True)
    assert decoded == _raw_bytes(RAW_VERDICT), (
        "the envelope and the readable verdict are not the same bytes; the checkout "
        "rewrote one of them and the raw hash no longer describes the measurement"
    )
    assert json.loads(decoded.decode("utf-8"))["observed"], (
        "and it parses to the verdict"
    )


def test_the_capture_script_is_versioned_with_the_evidence(
    frozen_from: dict[str, object],
) -> None:
    """The procedure that produced the evidence is part of the evidence.

    A measurement whose launcher lives only on the machine that ran it cannot be audited:
    the preflight that certified both worktrees clean, the environment it built and the
    command it issued would all be claims with nothing behind them.
    """
    evidence = frozen_from["evidence"]
    script = PROJECT_ROOT / str(evidence["capture_script"])  # type: ignore[index]
    assert script.exists(), f"the capture script must be versioned at {script}"
    recorded = _require_hex64(
        evidence["capture_script_canonical_blob_sha256"],  # type: ignore[index]
        "the capture script hash",
    )
    assert recorded == _canonical_blob_sha256(script), (
        "the versioned capture script is not the one provenance records"
    )
    source = script.read_text(encoding="utf-8")
    assert "git status --porcelain" in source or "porcelain" in source, (
        "the versioned script must be the one that certifies both trees clean"
    )


def test_the_evidence_directory_refuses_newline_translation() -> None:
    """The bytes are protected by a rule in the repository, not by a local git setting.

    Without ``-text`` the JSON is normalized on the way in and re-expanded on a Windows
    checkout, so ``raw_sha256`` would be green on the machine that wrote it and red on the
    runner -- the exact cross-platform trap this freeze exists to avoid.
    """
    attributes = EVIDENCE / ".gitattributes"
    assert attributes.exists(), "bench/evidence needs its own .gitattributes"
    assert "-text" in attributes.read_text(encoding="utf-8")


def test_the_published_numbers_are_the_measured_numbers(
    section: dict[str, object], home: str, raw_verdict: dict[str, object]
) -> None:
    """The calibration's observed block must equal the worker's own output, exactly.

    Without this, the section and the evidence beside it can drift apart silently: the
    hashes would still check out and the numbers CI gates on would be someone's edit.
    """
    observed = section["observed"][home]  # type: ignore[index]
    measured = raw_verdict["observed"]
    assert observed == measured, (
        "the frozen observed block differs from the raw verdict it claims to record"
    )
    for key in ("mean_recall_at_k", "min_recall_at_k"):
        assert type(observed[key]) is float, f"{key} must be an exact float"  # type: ignore[index]
    assert type(observed["queries_below_perfect"]) is int  # type: ignore[index]
    assert observed["queries_below_perfect"] is not True  # type: ignore[index]


def test_the_exit_code_and_duration_come_from_the_run(
    frozen_from: dict[str, object],
) -> None:
    """Both were absent from the first freeze; one was invented by the test itself.

    ``exit_code`` is asserted type-exact because ``True == 0`` is False but ``False == 0``
    is True, and a bool arriving here would mean something built it rather than measured it.
    """
    exit_code = frozen_from["exit_code"]
    assert type(exit_code) is int, f"exit_code must be an exact int; got {exit_code!r}"
    assert exit_code == 0, f"the measurement must have succeeded; exit code {exit_code}"

    duration = frozen_from["duration_seconds"]
    assert type(duration) is float, f"duration must be an exact float; got {duration!r}"
    assert duration > 0.0, "a measurement takes time"


def test_the_contended_timing_is_published_as_non_authoritative(
    frozen_from: dict[str, object],
) -> None:
    """Other test suites shared this host while the measurement ran, and it is recorded.

    Recall is unaffected -- corpus, query set, index seed and search are deterministic, so
    the numbers are identical under any load. Wall-clock is not, and the honest move is to
    publish the duration flagged rather than let a contended figure be read later as a
    performance baseline. A disclosure that only lives in a chat log is not provenance.
    """
    assert frozen_from["duration_is_authoritative"] is False, (
        "the run shared the host, so its duration is not a benchmark"
    )
    load = frozen_from["concurrent_load"]
    assert load["disclosed_by"], "the disclosure names who reported it"  # type: ignore[index]
    assert "Z/" in str(load["window_utc"]), "and when it happened"  # type: ignore[index]
    assert "none" in str(load["effect_on_recall"]).lower(), (  # type: ignore[index]
        "the recall numbers are deterministic and the record must say so plainly"
    )


def test_the_measurement_clears_the_floor_it_freezes(
    section: dict[str, object], home: str
) -> None:
    """A freeze recording a target its own measurement misses is not a calibration."""
    observed = section["observed"][home]  # type: ignore[index]
    assert observed["mean_recall_at_k"] >= section["frozen"]["target"]  # type: ignore[index]


def test_every_hash_in_the_frozen_block_is_lowercase_hex(
    section: dict[str, object],
) -> None:
    """Corpus and query-set digests included: 64 characters is not the same as a hash."""
    frozen = section["frozen"]
    for group in ("corpus", "query_set"):
        for field in ("sha256_f64", "sha256_f32"):
            _require_hex64(frozen[group][field], f"{group}.{field}")  # type: ignore[index]


# =====================================================================================
# Shape, floor and the additive section
# =====================================================================================


def test_the_committed_frozen_block_is_exactly_what_the_builder_produces(
    section: dict[str, object], home: str
) -> None:
    """Feed the committed values back through the builder and demand equality."""
    rebuilt = build_section(
        _verdict_from(section, home),
        target=section["frozen"]["target"],  # type: ignore[index]
    )
    assert rebuilt["frozen"] == section["frozen"]


def test_the_section_declares_the_schema_and_the_gate_requirement(
    section: dict[str, object],
) -> None:
    assert section["schema_version"] == 1
    assert section["frozen"]["gate_required"] is True  # type: ignore[index]
    assert section["frozen"]["metric"] == "cosine"  # type: ignore[index]
    assert section["frozen"]["storage_dtype"] == "float32"  # type: ignore[index]


def test_the_floor_is_never_lowered(section: dict[str, object]) -> None:
    """0.90 is a floor, and a floor that can be edited downward is not one.

    An inequality, not an equality: a future calibration may RAISE the target, and that must
    not read as a regression.
    """
    target = section["frozen"]["target"]  # type: ignore[index]
    assert type(target) is float, f"the floor is a plain float; got {target!r}"
    assert target >= DEFAULT_TARGET >= 0.90
    assert target >= DEFAULT_RECALL_TARGET


def test_the_legacy_calibration_survives_the_additive_section(
    document: dict[str, object],
) -> None:
    assert LEGACY_KEYS <= set(document), (
        f"missing legacy keys: {sorted(LEGACY_KEYS - set(document))}"
    )
    assert set(document) == LEGACY_KEYS | {"vector_recall"}, (
        f"unexpected additions: {sorted(set(document) - LEGACY_KEYS - {'vector_recall'})}"
    )


def test_no_machine_local_path_reaches_the_committed_artifact(
    section: dict[str, object],
) -> None:
    """Rooted anywhere is rooted: drives, UNC shares and single-segment POSIX roots.

    The audited regex required a second slash, so ``/artifact`` passed, and knew nothing of
    ``\\\\server\\share``. Asking both path flavours costs nothing and cannot miss a form
    one of them understands.
    """
    for trail, value in _walk_strings(section, "vector_recall"):
        assert not _is_absolute_anywhere(value), (
            f"{trail} carries a machine-local path: {value!r}"
        )


def test_machine_local_paths_are_confined_to_the_evidence_directory() -> None:
    """Where the machine-local paths ARE allowed to live, and where they are not.

    Two rules of this handoff collide, and the collision is resolved here in the open rather
    than by a silent choice. The handoff forbids absolute paths in the commit. The audit
    then required the capture launcher to be versioned and the effective command and
    environment recorded -- and a command that spawns an interpreter names its interpreter,
    while a launcher that certifies two worktrees names those worktrees.

    Both requirements are honoured by CONFINING the machine-local strings to
    ``bench/evidence/``, where they are evidence of a procedure rather than project
    configuration, and by proving that confinement instead of asserting it. Everything the
    project actually executes -- the harness, the tests, the workflow, the calibration -- is
    scanned and must be free of them, so the exception cannot quietly spread.

    The scan covers ``bench/`` outside the evidence directory. It deliberately does NOT
    walk the test tree: these modules carry hostile absolute paths on purpose, as fixtures
    for the detector above, and a check that could not tell a fixture from a leak would
    have to be weakened until it caught nothing.
    """
    scanned = 0
    offenders: list[str] = []
    for path in sorted((PROJECT_ROOT / "bench").rglob("*")):
        if not path.is_file() or EVIDENCE in path.parents or path == EVIDENCE:
            continue
        if path.suffix not in {".py", ".json", ".yml", ".yaml", ".md", ".txt"}:
            continue
        scanned += 1
        for number, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            for token in line.replace('"', " ").replace("'", " ").split():
                if len(token) > 3 and "<" not in token and _is_absolute_anywhere(token):
                    offenders.append(
                        f"{path.relative_to(PROJECT_ROOT).as_posix()}:{number} {token}"
                    )
    assert scanned > 5, f"the scan must actually cover the tree; saw {scanned} files"
    unexpected = [entry for entry in offenders if "cal-final" not in entry]
    assert unexpected == [], (
        "machine-local paths escaped bench/evidence into project content:\n"
        + "\n".join(unexpected[:10])
    )


def test_the_one_inherited_path_leak_is_tracked_and_unchanged(
    document: dict[str, object],
) -> None:
    """``environment.workspace`` in the LEGACY calibration names the machine that wrote it.

    It predates this work -- it is already in the parent commit -- and the handoff forbids
    touching legacy content outside the additive section, so this freeze must not repair it.
    What this freeze CAN do is stop it being invisible: pinned here, the leak is a known,
    inherited item rather than something the confinement scan silently steps around, and any
    attempt to add a SECOND one fails the scan above.
    """
    workspace = document["environment"]["workspace"]  # type: ignore[index]
    assert _is_absolute_anywhere(str(workspace)), (
        "if this ever stops being an absolute path the leak was fixed upstream -- remove "
        "this test and the 'cal-final' exception in the scan above together"
    )
    assert "cal-final" in str(workspace), (
        "the inherited leak changed shape; re-check it rather than widening the exception"
    )


def test_the_evidence_directory_declares_why_it_may_hold_them(
    frozen_from: dict[str, object],
) -> None:
    """The exception is written down where a reader of the artifact will find it.

    An undeclared exception is indistinguishable from an oversight, and six months from now
    nobody can tell which one this was.
    """
    note = str(frozen_from["machine_local_paths"])
    assert "bench/evidence" in note
    assert "command" in note.lower() or "launcher" in note.lower()


@pytest.mark.parametrize(
    "hostile",
    [
        "C:\\Projetos\\okto_grafx\\bench\\harness\\recall_worker.py",
        "\\\\build-server\\share\\evidence.json",
        "/artifact",
        "/home/runner/work/okto_grafx/bench",
        "D:/Projetos/Techridy/okto_grafx-c13",
    ],
    ids=["drive", "unc", "single-segment", "posix", "forward-drive"],
)
def test_the_path_detector_recognizes_every_rooted_form(hostile: str) -> None:
    """The detector is itself under test, because the audited one looked correct.

    Two of these -- the UNC share and the single-segment root -- are exactly what the
    previous regex let through, and a detector is only worth as much as the forms it knows.
    """
    assert _is_absolute_anywhere(hostile), f"{hostile!r} must be recognized as rooted"


@pytest.mark.parametrize(
    "benign",
    ["bench/harness/recall_worker.py", "uniform-int53-v1", "3.13.1", "cosine", ""],
)
def test_the_path_detector_leaves_relative_values_alone(benign: str) -> None:
    """The control: a detector that fires on everything would just be a broken build."""
    assert not _is_absolute_anywhere(benign)


# =====================================================================================
# The artifact governs the gate
# =====================================================================================


def test_the_gate_takes_its_floor_from_the_committed_artifact(
    section: dict[str, object],
) -> None:
    """With no flag the floor comes from the file -- and the gate says where it came from."""
    floor, origin = _resolve_recall_target(None, str(CALIBRATION))
    assert floor == section["frozen"]["target"]  # type: ignore[index]
    assert origin == f"frozen in {CALIBRATION}", origin


def _metrics(recall: float | None) -> str:
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
    """Resolution and application are two steps, and the probe walks both."""
    floor, _ = _resolve_recall_target(None, str(CALIBRATION))
    assert floor == section["frozen"]["target"]  # type: ignore[index]
    assert check(
        _metrics(floor - 0.01), recall_target=floor, require_recall=True
    ).status != (STATUS_MET)
    assert (
        check(_metrics(floor), recall_target=floor, require_recall=True).status
        == STATUS_MET
    )


def test_a_build_that_publishes_no_gauge_is_unmeasured_not_passing() -> None:
    """``--require-recall`` is what makes an absent measurement a failure."""
    floor, _ = _resolve_recall_target(None, str(CALIBRATION))
    verdict = check(_metrics(None), recall_target=floor, require_recall=True)
    assert verdict.status == STATUS_UNMEASURED


def test_the_workflow_command_lets_the_versioned_artifact_govern() -> None:
    """Asserted over the tokenized command, never over lines.

    The audited check read lines, so a ``--require-recall`` inside a trailing comment would
    have satisfied it and a ``$GATE_FLAGS`` carrying ``--recall-target`` would have escaped
    it entirely. Tokenizing the effective run scalar closes both.
    """
    argv = _gate_command()
    assert "--require-recall" in argv, f"the stage stays fail-closed; got {argv}"
    assert "--calibration" in argv, f"the artifact is named explicitly; got {argv}"
    assert "--recall-target" not in argv, (
        f"the floor comes from the artifact, not from a flag; got {argv}"
    )
    assert argv[argv.index("--calibration") + 1] == "bench/calibration.json"


def test_no_gate_flag_can_arrive_through_a_shell_variable() -> None:
    """A flag the workflow cannot show is a flag review cannot see.

    ``${{ matrix.family }}`` is legitimate and appears only inside the metrics filename;
    anything else expanding at run time could reintroduce the override invisibly.
    """
    argv = _gate_command()
    metrics_value = argv[argv.index("--metrics") + 1]
    for token in argv:
        assert not token.startswith("$"), f"{token!r} expands at run time"
        if "${{" in token:
            assert token == metrics_value, (
                f"only the metrics filename may interpolate; {token!r} does too"
            )


# =====================================================================================
# The knob that must not exist
# =====================================================================================


def test_the_configured_target_is_never_wired_to_a_search_width() -> None:
    """A transition rule, asserted over parses rather than over text.

    Zero occurrences in ``src`` is the preferred FINAL state and passes outright: M1 already
    carries the commit that removes the field, and a test demanding the legacy declaration
    forever would go red on the merge that finishes the job.

    While the freeze sits on the pre-M1 base, the only tolerated home is
    ``runtime/config.py``. Confining it to ONE file is what kills wiring split across two --
    the second file would have to name the knob, and naming it anywhere else fails here.

    Comments and docstrings do not count: this walks an AST, which cannot see a comment, and
    skips docstrings explicitly. The audited textual version failed a build over a sentence
    that said the two must stay apart.
    """
    offenders: dict[str, int] = {}
    for path in sorted((PROJECT_ROOT / "src").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (
            SyntaxError
        ) as failure:  # pragma: no cover -- a broken tree is its own bug
            pytest.fail(f"{path} does not parse: {failure}")
        found = _knob_occurrences(tree)
        if found:
            offenders[path.relative_to(PROJECT_ROOT).as_posix()] = len(found)

    if not offenders:
        return  # the final state: the knob is gone from src entirely

    assert set(offenders) == {CONFIG_RELATIVE}, (
        "while the knob still exists it may live only in the config declaration; "
        f"found it in {sorted(offenders)}"
    )


def test_the_surviving_knob_carries_no_execution_load() -> None:
    """Declaration and validation only -- never a value feeding an algorithm.

    Tolerating the field is not tolerating a use of it. Every surviving reference must sit
    in the annotated declaration or in validation; a reference that reaches arithmetic, a
    call argument that is not the field's own label, or a return would be the knob acquiring
    exactly the meaning C13 says it must never have.
    """
    config = PROJECT_ROOT / CONFIG_RELATIVE
    if not config.exists():
        return
    source = config.read_text(encoding="utf-8")
    tree = ast.parse(source)
    if not _knob_occurrences(tree):
        return  # already removed

    # The file that declares the SLO must not also name the search width: that co-occurrence
    # is what local wiring would look like, and the cross-file rule above cannot see it.
    assert "ef_search" not in source, (
        "the module declaring the recall SLO also names a search width; that is the shape "
        "wiring the target to the algorithm would take"
    )

    for node in ast.walk(tree):
        if isinstance(node, (ast.BinOp, ast.Return)):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Attribute) and inner.attr == KNOB:
                    pytest.fail(
                        "the recall target reaches arithmetic or a return value; it is an "
                        "SLO the harness verifies, never an input to a computation"
                    )
