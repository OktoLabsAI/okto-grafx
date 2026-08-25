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
import copy
import hashlib
import json
import re
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
ENGINE_RELATIVE: str = "src/okto_grafx/domain/vector/hnsw.py"
MEASURED_EF_SEARCH: int = 320
"""Named by number on purpose: a silent revert to the pre-calibration 64 must fail loudly."""

RECALL_METRIC: str = "oktografx_vector_recall_ratio"
HEX64 = frozenset("0123456789abcdef")

RUNNER_ROLE: str = "the bench worker that produced the measurement"
ENGINE_ROLE: str = "the vector engine imported by the measurement"

EXPECTED_GATE_ARGV: tuple[str, ...] = (
    "python",
    "-m",
    "bench.harness.gate",
    "--metrics",
    "metrics-${{ matrix.family }}.json",
    "--require-recall",
    "--calibration",
    "bench/calibration.json",
)

PORTABLE_PLACEHOLDER_PATH = re.compile(
    r"<[A-Z][A-Z0-9_]*>(?:[\\/][A-Za-z0-9_.-]+)*\Z"
)

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


def _git_bytes(*args: str) -> bytes:
    """Run git without a shell and require a successful, byte-exact result."""
    completed = subprocess.run(
        ["git", *args],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, (
        f"git {' '.join(args)} failed with {completed.returncode}: "
        f"{completed.stderr.decode('utf-8', errors='replace').strip()}"
    )
    return completed.stdout


def _git_text(*args: str) -> str:
    return _git_bytes(*args).decode("utf-8").strip()


def _object_format() -> str:
    return _git_text("rev-parse", "--show-object-format")


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


def _require_sha1_oid(value: object, what: str) -> str:
    """A sha1 object id is exactly 40 lowercase hexadecimal characters."""
    assert type(value) is str, f"{what} must be a plain string; got {value!r}"
    assert len(value) == 40, f"{what} must be 40 characters; got {len(value)}"
    assert set(value) <= HEX64, f"{what} must be lowercase hex; got {value!r}"
    return value


def _assert_committed_source(
    recorded: dict[str, object],
    *,
    expected_path: str,
    expected_role: str,
    extra_keys: frozenset[str] = frozenset(),
) -> tuple[str, str, str]:
    """Prove commit:path -> blob -> SHA256, instead of trusting adjacent strings."""
    common = {
        "canonical_blob_sha256",
        "commit",
        "git_blob",
        "path",
        "role",
    }
    expected_keys = common | set(extra_keys)
    assert set(recorded) == expected_keys, (
        f"unexpected source identity fields: {sorted(set(recorded) - common - set(extra_keys))}; "
        f"missing: {sorted(expected_keys - set(recorded))}"
    )
    assert recorded["path"] == expected_path
    assert recorded["role"] == expected_role
    commit = _require_sha1_oid(recorded["commit"], f"the commit for {expected_path}")
    blob = _require_sha1_oid(recorded["git_blob"], f"the blob for {expected_path}")
    content_sha256 = _require_hex64(
        recorded["canonical_blob_sha256"], f"the content hash for {expected_path}"
    )

    resolved = _git_text("rev-parse", "--verify", f"{commit}^{{commit}}")
    assert resolved == commit, f"{commit} does not resolve to the recorded commit"
    committed_blob = _git_text("rev-parse", "--verify", f"{commit}:{expected_path}")
    assert committed_blob == blob, (
        f"{commit}:{expected_path} is blob {committed_blob}, not recorded blob {blob}"
    )
    committed_bytes = _git_bytes("show", f"{commit}:{expected_path}")
    computed_sha256 = hashlib.sha256(committed_bytes).hexdigest()
    assert computed_sha256 == content_sha256, (
        f"{commit}:{expected_path} hashes to {computed_sha256}, not {content_sha256}"
    )
    return commit, blob, content_sha256


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
    if not value or PORTABLE_PLACEHOLDER_PATH.fullmatch(value):
        return False
    # Inspect whitespace-separated pieces too: wrappers such as ``<C:\\Users\\...>`` and
    # prefixes such as ``path=C:\\...`` must not hide a machine path from pathlib.
    candidates = {value}
    candidates.update(value.split())
    for candidate in candidates:
        cleaned = candidate.strip("\"'`()[]{}<>,;:")
        if not cleaned or PORTABLE_PLACEHOLDER_PATH.fullmatch(cleaned):
            continue
        # Remove a prose label without removing the drive colon in ``C:\\...``.
        if "=" in cleaned:
            cleaned = cleaned.rsplit("=", 1)[-1]
        windows = PureWindowsPath(cleaned)
        posix = PurePosixPath(cleaned)
        if cleaned not in {"/", "\\"} and (windows.root or posix.root):
            return True
    # The token walk handles normal values. These searches close embedded/wrapped forms
    # where punctuation remains attached (including an angle-bracket bypass).
    return bool(
        re.search(r"(?i)[a-z]:[\\/]", value)
        or re.search(r"\\\\[^\\\s]+[\\/]", value)
        or re.search(r"(?:^|[\s=<'\"])[\\/](?![\\/])[^\s]", value)
    )


def _call_name(node: ast.Call) -> str | None:
    return node.func.id if isinstance(node.func, ast.Name) else None


def _knob_execution_offenders(tree: ast.AST) -> list[ast.AST]:
    """Return references outside the declaration and its two validation calls."""
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    allowed_calls: set[ast.Call] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        expected_arity = {"_require_positive_number": 2, "_reject": 3}.get(name)
        if (
            expected_arity is None
            or len(node.args) != expected_arity
            or node.keywords
        ):
            continue
        label, value = node.args[:2]
        if (
            isinstance(label, ast.Constant)
            and label.value == KNOB
            and isinstance(value, ast.Attribute)
            and value.attr == KNOB
        ):
            allowed_calls.add(node)

    offenders: list[ast.AST] = []
    for occurrence in _knob_occurrences(tree):
        parent = parents.get(occurrence)
        if (
            isinstance(parent, ast.AnnAssign)
            and parent.target is occurrence
            and isinstance(occurrence, ast.Name)
        ):
            continue
        cursor: ast.AST | None = occurrence
        while cursor is not None and not isinstance(cursor, ast.Call):
            cursor = parents.get(cursor)
        if isinstance(cursor, ast.Call) and cursor in allowed_calls:
            continue
        offenders.append(occurrence)
    return offenders


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


def _gate_commands(workflow_text: str | None = None) -> list[list[str]]:
    """Every EFFECTIVE argv that invokes the workflow gate, tokenized.

    Reading lines was the audited mistake: it could not tell a flag from a flag inside a
    trailing comment, and it could not see flags arriving through a shell variable at all.
    Returning from the first match was another version of that mistake: a second step could
    run a weaker gate while every assertion inspected only the first. This collects every
    matching run scalar, drops comments, joins continuations and hands each result to shlex,
    so cardinality and the argv the runner executes are both governed.
    """
    text = (
        WORKFLOW.read_text(encoding="utf-8")
        if workflow_text is None
        else workflow_text
    )
    lines = text.splitlines()
    commands: list[list[str]] = []
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
        if "bench.harness.gate" not in script:
            continue
        expressions: dict[str, str] = {}

        def protect_expression(match: re.Match[str]) -> str:
            marker = f"__GITHUB_EXPRESSION_{len(expressions)}__"
            expressions[marker] = match.group(0)
            return marker

        protected = re.sub(r"\$\{\{[^{}]*\}\}", protect_expression, script)
        argv = shlex.split(protected, posix=True)
        restored: list[str] = []
        for token in argv:
            for marker, expression in expressions.items():
                token = token.replace(marker, expression)
            restored.append(token)
        commands.append(restored)
    return commands


def _gate_command(workflow_text: str | None = None) -> list[str]:
    """The sole workflow gate command; absence and duplication both fail closed."""
    commands = _gate_commands(workflow_text)
    assert len(commands) == 1, (
        "the workflow must invoke bench.harness.gate exactly once; "
        f"found {len(commands)} invocations: {commands}"
    )
    return commands[0]


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


@pytest.fixture(scope="module")
def raw_metadata() -> dict[str, object]:
    return json.loads(RAW_METADATA.read_text(encoding="utf-8"))


def _captured_verdict(
    raw_verdict: dict[str, object], raw_metadata: dict[str, object]
) -> dict[str, object]:
    """Join the worker bytes to the wrapper facts, never to the calibration under test."""
    captured = copy.deepcopy(raw_verdict)
    assert "duration_seconds" not in captured
    assert "exit_code" not in captured
    captured["duration_seconds"] = raw_metadata["duration_seconds"]
    captured["exit_code"] = raw_metadata["exit_code"]
    return captured


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
    assert isinstance(recorded, dict)
    _, blob, _ = _assert_committed_source(
        recorded,
        expected_path=WORKER_RELATIVE,
        expected_role=RUNNER_ROLE,
    )
    computed = _git_blob(PROJECT_ROOT / WORKER_RELATIVE)
    assert blob == computed, (
        "the worker recorded in provenance is not the worker in this tree: "
        f"{blob} vs {computed}"
    )
    assert recorded["canonical_blob_sha256"] == _canonical_blob_sha256(  # type: ignore[index]
        PROJECT_ROOT / WORKER_RELATIVE
    )


def test_the_recorded_object_format_is_the_repository_format(
    frozen_from: dict[str, object],
) -> None:
    """OID widths and blob arithmetic are meaningful only under the named format."""
    assert frozen_from["object_format"] == "sha1"
    assert frozen_from["object_format"] == _object_format()


def test_the_suite_checkout_carries_the_commits_provenance_names() -> None:
    """The commit:path proof cannot run in actions/checkout's depth-1 default clone."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    suite = workflow.split("\n  suite:\n", 1)[1].split("\n  coverage:\n", 1)[0]
    assert re.search(
        r"(?m)^      - uses: actions/checkout@v4\n"
        r"        with:\n"
        r"(?:          #.*\n)*"
        r"          fetch-depth: 0$",
        suite,
    ), "the suite needs full Git history to certify both recorded source commits"


def test_a_well_shaped_but_nonexistent_runner_commit_is_rejected(
    frozen_from: dict[str, object],
) -> None:
    """Forty hex characters are not proof that an object exists."""
    mutated = copy.deepcopy(frozen_from["runner_source"])
    assert isinstance(mutated, dict)
    mutated["commit"] = "0" * 40
    with pytest.raises(AssertionError, match=r"git rev-parse .* failed"):
        _assert_committed_source(
            mutated,
            expected_path=WORKER_RELATIVE,
            expected_role=RUNNER_ROLE,
        )


def test_a_wrong_engine_blob_is_rejected_even_when_the_commit_exists(
    frozen_from: dict[str, object],
) -> None:
    """The commit, path and blob are one identity chain, not independent labels."""
    mutated = copy.deepcopy(frozen_from["engine_dependency_source"])
    assert isinstance(mutated, dict)
    mutated["git_blob"] = "0" * 40
    with pytest.raises(AssertionError, match="not recorded blob"):
        _assert_committed_source(
            mutated,
            expected_path=ENGINE_RELATIVE,
            expected_role=ENGINE_ROLE,
            extra_keys=frozenset(
                {
                    "loaded_canonical_sha256",
                    "loaded_module_basename",
                    "proven_against_committed_blob",
                }
            ),
        )


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

    Both objects are mandatory. A missing object, invalid revision, wrong path, wrong blob or
    wrong content hash is a provenance failure; Git return code 128 is never treated as a
    reason to skip the relationship check.
    """
    engine = frozen_from["engine_dependency_source"]
    runner = frozen_from["runner_source"]
    assert isinstance(engine, dict)
    assert isinstance(runner, dict)
    engine_commit, _, engine_sha256 = _assert_committed_source(
        engine,
        expected_path=ENGINE_RELATIVE,
        expected_role=ENGINE_ROLE,
        extra_keys=frozenset(
            {
                "loaded_canonical_sha256",
                "loaded_module_basename",
                "proven_against_committed_blob",
            }
        ),
    )
    runner_commit, _, _ = _assert_committed_source(
        runner,
        expected_path=WORKER_RELATIVE,
        expected_role=RUNNER_ROLE,
    )
    assert engine["proven_against_committed_blob"] is True, (  # type: ignore[index]
        "the engine that loaded was never proven against the committed blob"
    )
    assert engine["loaded_canonical_sha256"] == engine_sha256, (  # type: ignore[index]
        "the module that loaded is not the module the artifact names"
    )
    assert engine["loaded_module_basename"] == Path(ENGINE_RELATIVE).name
    assert frozen_from["engine_is_ancestor_of_runner_commit"] is False, (
        "the engine commit is a separate integration dependency, never an ancestor"
    )
    assert "not an ancestor" in str(frozen_from["engine_relationship"]).lower()

    probe = subprocess.run(
        [
            "git",
            "merge-base",
            "--is-ancestor",
            engine_commit,
            runner_commit,
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
    )
    assert probe.returncode == 1, (
        "the engine relationship is not the recorded non-ancestry: "
        f"git returned {probe.returncode}; stderr={probe.stderr.decode('utf-8', errors='replace')!r}"
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
    raw_verdict: dict[str, object], raw_metadata: dict[str, object]
) -> None:
    """The guard that does not rely on anyone remembering to compare two literals.

    ``_validate_verdict`` requires a verdict's hnsw block to equal ``HNSW_FROZEN``, so the
    committed section is rebuilt into a verdict and handed back to the harness's own
    validator. The positive control matters as much as the refusal: it proves the rebuilt
    verdict is complete, so the refusal below is earned by the width and not by a field the
    rebuild forgot.
    """
    verdict = _captured_verdict(raw_verdict, raw_metadata)
    assert _validate_verdict(verdict, verdict["profile"]) is None  # type: ignore[arg-type]

    drifted = copy.deepcopy(verdict)
    drifted["hnsw"] = {**dict(verdict["hnsw"]), "ef_search": 64}  # type: ignore[arg-type]
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


def test_every_metadata_field_is_parsed_and_cross_checked(
    section: dict[str, object],
    home: str,
    frozen_from: dict[str, object],
    raw_verdict: dict[str, object],
    raw_metadata: dict[str, object],
) -> None:
    """Authenticated metadata is useful only if every field reaches an independent fact."""
    assert set(raw_metadata) == {
        "blas_environment",
        "command",
        "cwd",
        "duration_seconds",
        "engine",
        "engine_relationship",
        "exit_code",
        "loaded_modules",
        "numpy",
        "platform",
        "python",
        "python_full",
        "pythonpath_note",
        "raw_verdict_bytes",
        "raw_verdict_sha256",
        "what",
        "worker",
        "worker_stderr_tail",
        "worker_stdout_tail",
    }

    evidence = frozen_from["evidence"]
    runner = frozen_from["runner_source"]
    engine = frozen_from["engine_dependency_source"]
    assert isinstance(evidence, dict)
    assert isinstance(runner, dict)
    assert isinstance(engine, dict)
    assert set(evidence) == {
        "capture_script",
        "capture_script_canonical_blob_sha256",
        "metadata",
        "metadata_raw_sha256",
        "raw_sha256",
        "verdict",
        "verdict_base64",
        "verdict_bytes",
    }
    assert evidence["metadata"] == RAW_METADATA.relative_to(PROJECT_ROOT).as_posix()
    assert evidence["verdict"] == RAW_VERDICT.relative_to(PROJECT_ROOT).as_posix()
    assert evidence["verdict_base64"] == RAW_B64.relative_to(PROJECT_ROOT).as_posix()

    def metadata_identity(source: dict[str, object]) -> dict[str, object]:
        return {
            "blob": source["git_blob"],
            "blob_sha256": source["canonical_blob_sha256"],
            "commit": source["commit"],
            "path": source["path"],
        }

    assert raw_metadata["worker"] == metadata_identity(runner)
    assert raw_metadata["engine"] == metadata_identity(engine)
    assert raw_metadata["raw_verdict_sha256"] == evidence["raw_sha256"]
    assert raw_metadata["raw_verdict_sha256"] == _raw_sha256(RAW_VERDICT)
    assert raw_metadata["raw_verdict_bytes"] == evidence["verdict_bytes"]
    assert raw_metadata["raw_verdict_bytes"] == len(_raw_bytes(RAW_VERDICT))

    provenance = section["provenance"][home]  # type: ignore[index]
    for field in ("blas_environment", "numpy", "platform", "python"):
        assert raw_metadata[field] == frozen_from[field]
    assert raw_metadata["blas_environment"] == raw_verdict["blas_environment"]
    assert raw_metadata["blas_environment"] == provenance["blas_environment"]
    assert raw_metadata["numpy"] == raw_verdict["numpy"] == provenance["numpy"]
    for field in ("profile", "gt_path_used"):
        assert type(raw_verdict[field]) is str
        assert type(provenance[field]) is str
        assert provenance[field] == raw_verdict[field], (
            f"provenance.{field} must come from the authenticated worker verdict"
        )
    assert type(raw_metadata["python"]) is str
    assert type(provenance["python"]) is str
    assert provenance["python"] == raw_metadata["python"], (
        "provenance.python must come from the authenticated capture metadata"
    )
    assert raw_metadata["duration_seconds"] == frozen_from["duration_seconds"]
    assert raw_metadata["duration_seconds"] == provenance["duration_seconds"]
    assert raw_metadata["exit_code"] == frozen_from["exit_code"]

    loaded = raw_metadata["loaded_modules"]
    assert isinstance(loaded, dict)
    assert set(loaded) == {
        "hnsw",
        "hnsw_lf_sha256",
        "matches_committed_blob",
        "okto_grafx",
    }
    assert loaded["hnsw"] == engine["loaded_module_basename"]
    assert loaded["hnsw_lf_sha256"] == engine["loaded_canonical_sha256"]
    assert loaded["matches_committed_blob"] is True
    assert loaded["matches_committed_blob"] is engine["proven_against_committed_blob"]
    assert loaded["okto_grafx"] == "__init__.py"

    template = frozen_from["command_template"]
    command = raw_metadata["command"]
    assert isinstance(template, dict)
    assert isinstance(command, list) and all(type(item) is str for item in command)
    assert command[1:-1] == [
        "-m",
        "bench.harness.recall_worker",
        "--profile",
        "full",
        "--gt",
        "auto",
        "--out",
    ]
    assert _is_absolute_anywhere(command[0]), "the exact interpreter that ran is recorded"
    assert _is_absolute_anywhere(command[-1]), "the exact output path is recorded"
    assert template["argv"] == ["<PYTHON>", *command[1:-1], "<VERDICT_OUTPUT_PATH>"]
    assert template["cwd"] == "<RUNNER_WORKTREE>"
    assert template["pythonpath"] == "<ENGINE_WORKTREE>/src"
    assert template["environment"] == raw_metadata["blas_environment"]

    assert raw_metadata["cwd"] == "the C13 worktree"
    assert raw_metadata["what"] == (
        "C13 full-profile vector recall measurement, re-taken from clean trees"
    )
    assert str(raw_metadata["python_full"]).split()[0] == raw_metadata["python"]
    assert "PYTHONPATH" in str(raw_metadata["pythonpath_note"])
    assert "verified against the committed blob" in str(raw_metadata["pythonpath_note"])
    assert raw_metadata["worker_stderr_tail"] == ""
    assert raw_metadata["worker_stdout_tail"] == (
        f"recall worker: profile {raw_verdict['profile']} mean recall@k "
        f"{raw_verdict['gauge']:.4f} via {raw_verdict['gt_path_used']}\n"
    )

    relationship = str(raw_metadata["engine_relationship"])
    assert str(engine["commit"]) in relationship
    assert "NOT an ancestor" in relationship
    assert "SEPARATE INTEGRATION DEPENDENCY" in relationship
    assert "not an ancestor" in str(frozen_from["engine_relationship"]).lower()


def test_every_frozen_provenance_field_is_governed(
    section: dict[str, object], home: str, frozen_from: dict[str, object]
) -> None:
    """A new provenance field cannot arrive without a test deciding what proves it."""
    assert set(section["provenance"][home]) == {  # type: ignore[index]
        "blas_environment",
        "duration_seconds",
        "frozen_from",
        "gt_path_used",
        "numpy",
        "profile",
        "python",
    }
    assert set(frozen_from) == {
        "blas_environment",
        "command_template",
        "concurrent_load",
        "duration_is_authoritative",
        "duration_seconds",
        "engine_dependency_source",
        "engine_is_ancestor_of_runner_commit",
        "engine_relationship",
        "evidence",
        "exit_code",
        "hash_convention",
        "machine_local_paths",
        "measured_ef_search",
        "numpy",
        "object_format",
        "platform",
        "python",
        "runner_source",
    }
    convention = str(frozen_from["hash_convention"])
    for required in ("raw_sha256", "EXACT bytes", "-text", "canonical_blob_sha256", "LF"):
        assert required in convention
    assert "never interchanged" in convention


def test_the_frozen_projection_comes_from_the_raw_verdict_not_itself(
    section: dict[str, object],
    home: str,
    raw_verdict: dict[str, object],
    raw_metadata: dict[str, object],
) -> None:
    """The raw worker document and harness constants independently rebuild the freeze."""
    assert set(raw_verdict) == {
        "blas_environment",
        "corpus_size",
        "dimension",
        "failure",
        "gauge",
        "generator",
        "gt_path_used",
        "hashes",
        "hnsw",
        "k",
        "numpy",
        "observed",
        "ok",
        "oracle",
        "profile",
        "queries",
    }
    captured = _captured_verdict(raw_verdict, raw_metadata)
    assert _validate_verdict(captured, str(captured["profile"])) is None

    rebuilt = build_section(captured, target=DEFAULT_TARGET)
    assert rebuilt["frozen"] == section["frozen"]
    rebuilt_home = next(iter(rebuilt["observed"]))  # type: ignore[arg-type]
    assert rebuilt["observed"][rebuilt_home] == raw_verdict["observed"]  # type: ignore[index]
    assert raw_verdict["observed"] == section["observed"][home]  # type: ignore[index]


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
    for path in (RAW_METADATA, RAW_VERDICT, RAW_B64):
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        reported: dict[str, str] = {}
        for line in _git_text(
            "check-attr", "text", "diff", "--", relative
        ).splitlines():
            _, attribute, value = line.split(": ", 2)
            reported[attribute] = value
        assert reported == {"text": "unset", "diff": "unset"}, (
            f"{relative} must be byte-stable and binary to diff machinery; comments do not "
            f"set attributes, and git reports {reported}"
        )


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
    frozen_from: dict[str, object], raw_metadata: dict[str, object]
) -> None:
    """Both were absent from the first freeze; one was invented by the test itself.

    ``exit_code`` is asserted type-exact because ``True == 0`` is False but ``False == 0``
    is True, and a bool arriving here would mean something built it rather than measured it.
    """
    exit_code = frozen_from["exit_code"]
    assert type(exit_code) is int, f"exit_code must be an exact int; got {exit_code!r}"
    assert exit_code == 0, f"the measurement must have succeeded; exit code {exit_code}"
    assert exit_code == raw_metadata["exit_code"]

    duration = frozen_from["duration_seconds"]
    assert type(duration) is float, f"duration must be an exact float; got {duration!r}"
    assert duration > 0.0, "a measurement takes time"
    assert duration == raw_metadata["duration_seconds"]


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
    assert "none" in str(load["effect_on_recall"]).lower(), (  # type: ignore[index]
        "the recall numbers are deterministic and the record must say so plainly"
    )


def test_every_contended_window_is_recorded_in_order() -> None:
    """BOTH windows, and the count is asserted rather than the presence of one.

    The audited commit recorded only the first. The second was disclosed after the inbox
    pull that preceded it, and a fact that existed did not reach the artifact -- which is the
    same failure mode as the provenance blockers, one layer up: the record has to be complete
    or it is not a record.

    Asserting the exact count is what makes this a guard. "At least one window" would have
    passed the commit that was blocked, and "contains 05:10" would pass a list that dropped
    the first. Order is asserted too, because a chronology that does not increase is not one.
    """
    document = json.loads(CALIBRATION.read_text(encoding="utf-8"))
    home = next(iter(document["vector_recall"]["provenance"]))
    load = document["vector_recall"]["provenance"][home]["frozen_from"][
        "concurrent_load"
    ]

    windows = load["windows"]
    assert type(windows) is list, "the windows are a list, in chronological order"
    assert len(windows) == 2, (
        f"exactly two windows were disclosed; found {len(windows)}"
    )
    assert load["count"] == len(windows), "the count and the list must agree"

    starts = [str(window["window_utc"]).split("/")[0] for window in windows]
    assert starts == sorted(starts), (
        f"the windows are out of chronological order: {starts}"
    )

    assert windows[0]["window_utc"] == "2026-08-25T05:00Z/2026-08-25T05:04Z"
    assert windows[1]["window_utc"] == "2026-08-25T05:10Z/2026-08-25T05:15Z"
    assert type(load["count"]) is int
    assert type(windows[1]["test_count"]) is int
    assert windows[1]["test_count"] == 147

    for index, window in enumerate(windows):
        assert window["disclosed_by"], f"window {index} names who reported it"
        assert "none" in str(window["effect_on_recall"]).lower(), (
            f"window {index} must state plainly that recall is unaffected"
        )
        assert "not authoritative" in str(window["effect_on_timing"]).lower(), (
            f"window {index} must state that timing is contended"
        )


def test_the_command_template_names_no_machine(
    frozen_from: dict[str, object],
) -> None:
    """A portable projection of the command lives beside the exact one.

    The exact argv is preserved unredacted in the evidence metadata, because a redacted
    command would be a command nobody ran. But a reader on another host needs something they
    can actually follow, and that copy must carry no filesystem but their own -- so every
    machine-local position is a placeholder, and this proves it rather than trusting it.
    """
    template = frozen_from["command_template"]
    assert isinstance(template, dict)
    assert set(template) == {"argv", "cwd", "environment", "note", "pythonpath"}
    for trail, value in _walk_strings(template, "command_template"):
        assert not _is_absolute_anywhere(value), (
            f"{trail} carries a machine-local path: {value!r}"
        )
    argv = template["argv"]  # type: ignore[index]
    assert argv == [
        "<PYTHON>",
        "-m",
        "bench.harness.recall_worker",
        "--profile",
        "full",
        "--gt",
        "auto",
        "--out",
        "<VERDICT_OUTPUT_PATH>",
    ]
    assert template["cwd"] == "<RUNNER_WORKTREE>"
    assert template["pythonpath"] == "<ENGINE_WORKTREE>/src"
    assert set(template["environment"]) == {  # type: ignore[index]
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    }, "the BLAS pin is part of the procedure, not an incidental detail"


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
    section: dict[str, object],
    raw_verdict: dict[str, object],
    raw_metadata: dict[str, object],
) -> None:
    """Feed the authenticated raw capture through the builder and demand equality."""
    rebuilt = build_section(
        _captured_verdict(raw_verdict, raw_metadata),
        target=DEFAULT_TARGET,
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
                if len(token) > 3 and _is_absolute_anywhere(token):
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
        "\\Users\\builder\\evidence.json",
        "/artifact",
        "/home/runner/work/okto_grafx/bench",
        "D:/Projetos/Techridy/okto_grafx-c13",
        "<C:\\Users\\builder\\evidence.json>",
        "<PLACEHOLDER>C:\\Users\\builder\\evidence.json",
    ],
    ids=[
        "drive",
        "unc",
        "windows-root-relative",
        "single-segment",
        "posix",
        "forward-drive",
        "angle-wrapped-drive",
        "angle-prefixed-drive",
    ],
)
def test_the_path_detector_recognizes_every_rooted_form(hostile: str) -> None:
    """The detector is itself under test, because the audited one looked correct.

    Two of these -- the UNC share and the single-segment root -- are exactly what the
    previous regex let through, and a detector is only worth as much as the forms it knows.
    """
    assert _is_absolute_anywhere(hostile), f"{hostile!r} must be recognized as rooted"


@pytest.mark.parametrize(
    "benign",
    [
        "bench/harness/recall_worker.py",
        "uniform-int53-v1",
        "3.13.1",
        "cosine",
        "<PYTHON>",
        "<ENGINE_WORKTREE>/src",
        "",
    ],
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
    assert tuple(argv) == EXPECTED_GATE_ARGV, (
        "the effective gate command changed; review every token rather than allowing an "
        f"implicit flag or shell operator: {argv}"
    )


def test_a_second_workflow_gate_cannot_hide_after_the_governed_one() -> None:
    """The first matching step is not authority over a second executable gate."""
    command = " ".join(EXPECTED_GATE_ARGV)
    smuggled = "\n".join(
        (
            "steps:",
            "  - name: Governed gate",
            f"    run: {command}",
            "  - name: Second gate (smuggled)",
            f"    run: {command} --recall-target 0.10",
        )
    )
    with pytest.raises(AssertionError, match="exactly once"):
        _gate_command(smuggled)


def test_no_gate_flag_can_arrive_through_a_shell_variable() -> None:
    """A flag the workflow cannot show is a flag review cannot see.

    ``${{ matrix.family }}`` is legitimate and appears only inside the metrics filename;
    anything else expanding at run time could reintroduce the override invisibly.
    """
    argv = _gate_command()
    metrics_value = argv[argv.index("--metrics") + 1]
    for token in argv:
        without_matrix = token.replace("${{ matrix.family }}", "")
        assert "$" not in without_matrix, f"{token!r} expands at run time"
        assert "`" not in without_matrix, f"{token!r} executes a command substitution"
        if "${{" in token:
            assert token == metrics_value, (
                f"only the metrics filename may interpolate; {token!r} does too"
            )


def test_quoted_shell_variables_are_still_visible_after_tokenization() -> None:
    """The old posix=False split left the quote ahead of '$' and hid the expansion."""
    argv = shlex.split(
        'python -m bench.harness.gate "$GATE_FLAGS" --require-recall',
        posix=True,
    )
    assert "$GATE_FLAGS" in argv
    assert any("$" in token for token in argv)


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

    offenders = _knob_execution_offenders(tree)
    assert offenders == [], (
        "the recall target escaped its declaration or exact validation calls at lines "
        f"{sorted({getattr(node, 'lineno', 0) for node in offenders})}"
    )


def test_a_call_cannot_turn_the_surviving_knob_into_an_execution_input() -> None:
    """Regression for the AST hole: calls are neither BinOp nor Return nodes."""
    hostile = ast.parse(
        "def mutate(self):\n"
        "    apply_search_beam(self.vector_recall_target)\n"
    )
    assert _knob_execution_offenders(hostile), (
        "apply_search_beam(self.vector_recall_target) must fail the structural gate"
    )
    smuggled = ast.parse(
        "def mutate(self):\n"
        "    _require_positive_number(\n"
        "        'vector_recall_target', self.vector_recall_target,\n"
        "        self.vector_recall_target,\n"
        "    )\n"
    )
    assert _knob_execution_offenders(smuggled), (
        "the validation-call whitelist must not authorize extra execution arguments"
    )
