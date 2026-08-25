"""Parent side of the C13 recall stage: spawn the worker, shape the frozen section (v4 §2-3).

This module owns process isolation and document shaping, never the mathematics: it launches
:mod:`bench.harness.recall_worker` in a FRESH interpreter with the BLAS thread variables set in
the child environment before Python starts (the only placement that cannot arrive after a numpy
import), reads the worker's JSON verdict back from a temporary file, and turns it into the
``vector_recall`` calibration section — ``frozen`` (bit-comparable), ``observed`` per family
(deterministic same-machine), ``provenance`` per family (everything volatile, outside every
hash). Publication order — section first, gauge last — belongs to the harness wiring, which
calls :func:`run_recall` and appends; nothing here writes calibration or metrics documents.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
import time
from fractions import Fraction
from pathlib import Path

from bench.recall_corpus import (
    generate_vectors,
    sha256_hex,
    vector_bytes_f32,
    vector_bytes_f64,
)
from bench.harness.recall_worker import (
    ACCEL_EPS_REL,
    CORPUS_SEED,
    DIFFERENTIAL_QUERIES,
    DIFFERENTIAL_SLICE,
    DTYPE_MEAN_OVERLAP_MIN,
    DTYPE_PER_QUERY_OVERLAP_MIN,
    GENERATOR_NAME,
    HNSW_FROZEN,
    PROFILES,
    QUERY_SEED,
    _BLAS_THREAD_VARIABLES,
)

RECALL_METRIC: str = "oktografx_vector_recall_ratio"
"""The SPEC-VEC FR-8 gauge the gate reads; published LAST by the harness wiring."""

DEFAULT_TARGET: float = 0.90
"""The frozen recall floor; the anti-drift test pins it equal to the gate's default."""

REGIME_THRESHOLD: int = 4096
"""``vector_exact_scan_threshold`` at the frozen SHA; smoke and full stay above it."""


class RecallStageError(RuntimeError):
    """A fail-closed recall outcome: the caller publishes nothing and exits non-zero."""


def family() -> str:
    """Return the calibration family of this platform, matching the CI matrix."""
    return "windows" if os.name == "nt" else "posix"


PROFILE_TIMEOUTS: dict[str, float] = {"tiny": 300.0, "smoke": 1800.0, "full": 9600.0}
"""Wall-clock ceilings per profile, measured rather than guessed. The full profile run
took ~53 minutes end to end on the freeze machine (faster than the CI runners), so its
ceiling is 160 minutes -- whole-run, with margin, under the scheduled job's 180-minute
budget. What IS separately measured about the phases: a 2048x384 build takes ~3
seconds, and per-query searches at the frozen ef visit most of that graph (the ACORN
pruning weakens while the result set stays unfilled). The phase breakdown of the full
8192x384 run was NOT instrumented separately, so no phase is blamed here -- the
ceiling simply covers all of them. Smoke keeps the original 30 minutes and tiny stays
test-sized. ``run_recall`` resolves these when the caller passes no explicit timeout;
an explicit value always wins after validation."""


def _is_a(value: object, kind: object) -> bool:
    """isinstance(), guarded: the check itself runs the INSPECTED object's code.

    ``__instancecheck__`` and ``__subclasshook__`` (every ABC check, Mapping included)
    and a ``__class__`` property (any check that falls back to it) are controlled by the
    value being examined, so the SHAPE of what they raise is chosen by hostile data
    rather than by an interrupt of our work -- the same reasoning that makes _describe
    absorb everything. Widening the surrounding guard does not help: those guards catch
    Exception, and a SystemExit raised from __instancecheck__ walks straight through the
    line that decides "this is not a document".

    A value whose type cannot be determined is not of that type. That single
    conservative default is right for both uses here: an undecidable document value is
    refused, and an undecidable exception is treated as NOT an ordinary Exception, so
    the primary is re-raised instead of being converted into a typed refusal.
    """
    try:
        return isinstance(value, kind)  # type: ignore[arg-type]
    except BaseException:  # noqa: BLE001 -- the OBJECT chose this shape, not the process
        return False


_NATIVE_PATH = type(Path("."))
"""This platform's concrete Path class, captured at import.

Round-11 (1): a Path is only a Path if the constructor gave us the class this module was
written against. Captured here, before anything in a test or an environment can replace
the name, and compared with `type(...) is` so that deciding whether an object is
trustworthy never touches one of its methods.
"""


def _exact_str(value: object, what: str) -> str:
    """Return value when it is an EXACT str; otherwise refuse.

    Round-11: a str subclass survives every honest check and then answers __getitem__,
    __format__ or __eq__ however it likes, one step later and in someone else's frame.
    Type-exactness is checked with `type(...) is`, which runs none of its code.
    """
    if type(value) is not str:
        raise RuntimeError(f"{what} is not a plain string; refusing to use it")
    return value


def _exact_int(value: object, what: str, minimum: int) -> int:
    """Return value when it is an EXACT int at or above minimum; otherwise refuse.

    bool is excluded by the type-exact test, which matters: True passes every
    `isinstance(x, int)` and every `x > 0`, and has certified a lock that was never
    written and a write that never happened.
    """
    if type(value) is not int:
        raise RuntimeError(f"{what} is not a plain integer; refusing to use it")
    if value < minimum:
        raise RuntimeError(f"{what} is below {minimum}; refusing to use it")
    return value


def _trusted_path(name: object) -> Path:
    """Build a Path from an exact name and prove the constructor honoured it.

    Round-11 (1) corrects round-10 twice over. The CALL is work: a real interrupt of it
    must propagate, and the round-10 version absorbed every shape including KI/SE. And
    the RESULT is not proved by behaviour: round-10 accepted anything whose __fspath__
    round-tripped, which a hostile object supplies for free while keeping a read_text
    that exits. The class is what is checked, with `type(...) is`, before any attribute
    of the object is touched.
    """
    exact = _exact_str(name, "a path name")
    candidate = Path(exact)  # WORK: a genuine interrupt here propagates.
    if type(candidate) is not _NATIVE_PATH:
        raise RuntimeError(
            f"the path constructor did not yield a native path for {exact!r}"
        )
    return candidate


def _exact_clock(reading: object) -> float:
    """Prove a monotonic reading is an exact finite float before it is used."""
    if type(reading) is not float or not math.isfinite(reading):
        raise RecallStageError(
            "the monotonic clock did not return a finite float; the run cannot be timed."
        )
    return reading


def _caller_path(value: object, what: str) -> Path:
    """Reduce a CALLER's object to a path of OURS, with both boundaries inside.

    Round-12 (A). The separation is right but four rounds have shown it does not survive
    being written by hand at each site: resolve/stat, the lock names, run_recall's
    scratch and the wiring's workspace each got it wrong once. So the two halves live
    here, and call sites do not repeat them.

    The first half is DATA: os.fspath runs the ARGUMENT's __fspath__, so every shape it
    chooses becomes a typed refusal. The second is WORK: building our own path, where an
    ordinary failure is a refusal and a genuine interrupt belongs to the caller and
    propagates. A call site that wraps this whole function in one clause would undo that,
    which is why the wrappers below catch Exception, never BaseException.
    """
    try:
        name = os.fspath(value)
    except BaseException as failure:  # noqa: BLE001 -- the ARGUMENT chose the shape
        raise RecallStageError(
            f"{what} is not a usable path ({_describe(failure)})"
        ) from None
    return _trusted_path(name)


def _certify_none(result: object, what: str) -> None:
    """Prove a syscall that reports completion by returning None actually did.

    Round-12 (C): os.close, os.replace and os.unlink return None. A replacement
    answering True is not merely odd -- it is a call whose EFFECT is unknown, and the
    stage treated it as success. A replace that may not have happened published nothing
    while reporting 0; an unlink that may not have happened left locks and temporaries
    behind the same way. An unproved completion is INCONCLUSIVE, never done.
    """
    if result is not None:
        raise RuntimeError(f"{what} did not report completion; the outcome is unknown")


def _claim_temp(made: object) -> tuple[int | None, str | None, str | None]:
    """Snapshot BOTH halves of a mkstemp result before judging either.

    Round-12 (B): a pair whose descriptor was real and whose name was a str subclass
    closed the descriptor and left the FILE; a tuple subclass left both. The two are
    read first, through non-dispatchable builtin access so a tuple subclass cannot
    answer with its own code, and only then judged.

    Returns (descriptor, name, refusal). When refusal is None the pair is usable. When
    it is not, the descriptor and the name are whatever could be RECLAIMED -- a name
    derived from a str subclass through str.__str__, which does not dispatch to the
    override, so it can be used to remove the file without ever being accepted
    operationally. A shape from which nothing can be reclaimed returns both as None with
    a constant text, because ownership is then genuinely inconclusive.
    """
    if not _is_a(made, tuple):
        return None, None, "the temp file result is not a pair"
    try:
        if tuple.__len__(made) != 2:
            return None, None, "the temp file result is not a pair"
        first = tuple.__getitem__(made, 0)
        second = tuple.__getitem__(made, 1)
    except BaseException:  # noqa: BLE001 -- an unreadable result owns nothing we know
        return None, None, "the temp file result could not be read"
    descriptor = first if type(first) is int and first >= 0 else None
    name: str | None = None
    if type(second) is str:
        name = second if second else None
    elif _is_a(second, str):
        try:
            derived = str.__str__(second)
        except BaseException:  # noqa: BLE001 -- nothing reclaimable from this one
            derived = ""
        name = derived if type(derived) is str and derived else None
    # A tuple SUBCLASS is never accepted operationally, however well-formed its
    # contents: its own __len__ and __getitem__ decide what the rest of the program
    # sees, and this function only reached the real halves by refusing to ask it.
    # Reclaiming from it is fine -- using it is not.
    if (
        type(made) is tuple
        and descriptor is not None
        and type(second) is str
        and second
    ):
        return descriptor, second, None
    return descriptor, name, "the temp file result is not a usable pair"


def _describe(value: object) -> str:
    """repr(), guarded and bounded: ``repr(10**10000)`` raises past CPython's digit
    limit, and the refusal message must not crash the refusal (mirrors gate._describe).
    """
    try:
        text = repr(value)
    except BaseException:  # noqa: BLE001 -- a diagnostic NEVER decides an outcome
        # Round-6: this caught Exception only, so a __repr__ raising SystemExit or
        # KeyboardInterrupt escaped the one helper whose entire purpose is to keep a
        # refusal from crashing. Everywhere else in this stage KI/SE propagate, and
        # they should: those places do WORK, and an interrupt must be able to stop it.
        # Nothing is done here -- a value is formatted for a message -- and the shape is
        # chosen by the object being described, which makes an escaping SystemExit not
        # the process asking to exit but hostile data walking through the guard.
        # Absorbing it is the only way the promise this function exists to keep is true.
        # CONSTANT fallback: even type(value).__name__ can execute a hostile
        # metaclass property. The refusal path touches the offender zero more times.
        return "<value whose repr raises>"
    return _bounded(text)


def _bounded(text: object) -> str:
    """The length rule, applied only to an EXACT str and under the same guard.

    Round-7 (1): len() and the slice were outside the try, so a str SUBCLASS returned by
    repr could raise from __len__/__getitem__ after the guard had already been passed.
    Type-exactness comes first because it removes the object's code from the path
    entirely; the guard stays anyway, since a diagnostic that can raise is not one.
    """
    try:
        if type(text) is not str:
            # Round-7 (A): repr() is only CONVENTIONALLY a str -- it may return a
            # str SUBCLASS whose __len__, __getitem__ or __format__ raises. Guarding
            # len() alone would not fix that: the object would leave this function
            # intact and explode later, inside the f-string of whoever formats the
            # refusal. Type-exact never runs the object's code; anything else becomes
            # a constant.
            return "<value whose repr is not a plain str>"
        return text if len(text) <= 80 else text[:77] + "..."
    except BaseException:  # noqa: BLE001 -- a diagnostic NEVER decides an outcome
        return "<value whose repr cannot be measured>"


def _emit(line: str) -> None:
    """print(), guarded: a diagnostic must never become the outcome it describes.

    Round-6: the lock-residue warnings are printed from inside a ``finally``, so a print
    that raises -- a closed stdout, a hostile replacement -- did not merely lose the
    message: it REPLACED the primary exception with the failure of the message about it.
    The exit code is this stage's contract and the text is the courtesy, so every shape
    is absorbed here. A refusal that cannot be printed is still a refusal.
    """
    try:
        print(line)
    except BaseException:  # noqa: BLE001 -- a diagnostic NEVER decides an outcome
        pass


def run_recall(
    profile: str,
    *,
    gt_mode: str = "auto",
    scratch: Path,
    timeout_seconds: float | None = None,
) -> dict[str, object]:
    """Run the worker subprocess for one profile and return the parsed verdict.

    The child environment carries every BLAS thread variable pinned to ``1`` BEFORE the
    interpreter starts; a fresh process cannot have imported numpy earlier, so the pin can
    never be late. A worker that exits non-zero, times out, or writes no verdict raises
    :class:`RecallStageError` — the harness then publishes nothing vectorial and fails.
    """
    if profile not in PROFILES:
        raise RecallStageError(
            f"unknown recall profile {profile!r}; use one of {sorted(PROFILES)}"
        )
    if timeout_seconds is None:
        timeout_seconds = PROFILE_TIMEOUTS[profile]
    invalid = _is_a(timeout_seconds, bool) or type(timeout_seconds) not in (
        int,
        float,
    )
    if not invalid:
        try:
            # float(10**10000) raises OverflowError BEFORE isfinite could refuse it, so
            # the conversion itself is guarded -- the reaudit's huge-integer probe.
            as_float = float(timeout_seconds)
        except OverflowError:
            invalid = True
        else:
            invalid = not math.isfinite(as_float) or not as_float > 0.0
    if invalid:
        raise RecallStageError(
            "timeout_seconds must be a finite positive number; "
            f"got {_describe(timeout_seconds)} "
            "-- refused before any directory or process exists."
        )
    timeout_seconds = as_float
    # Round-11 (9): `scratch` is the caller's object, and every use of it -- the join in
    # the caller, the mkdir here, the mkstemp below -- ran its code. It is reduced to a
    # trusted native path first, so nothing hostile survives into the filesystem calls.
    try:
        scratch = _caller_path(scratch, "the scratch directory")
    except Exception as failure:  # noqa: BLE001 -- KI/SE propagate; this is OUR work
        raise RecallStageError(
            f"the scratch directory could not be prepared ({_describe(failure)}); "
            "nothing was created and nothing was spawned."
        ) from None
    scratch.mkdir(parents=True, exist_ok=True)
    # Reaudit HIGH-2: a DETERMINISTIC verdict path let a worker that exited 0 without
    # writing hand back a PREVIOUS run's file as this run's result. Every run now gets a
    # unique fresh file (mkstemp; the handle closes at once so the Windows child can open
    # it), an empty fresh file is a typed refusal -- absence is never acceptance -- and
    # the cleanup in the finally below is outcome-neutral.
    made = tempfile.mkstemp(  # WORK: a genuine interrupt propagates.
        prefix=f"recall-{profile}-", suffix=".json", dir=str(scratch)
    )
    descriptor, temp_name, refusal = _claim_temp(made)
    if refusal is not None:
        # Round-12 (B): whatever could be RECLAIMED is released -- the descriptor closed
        # exactly once, the file removed by a name derived without dispatching to a
        # subclass -- and each independently, so one failing does not strand the other.
        if descriptor is not None:
            try:
                os.close(descriptor)
            except BaseException:  # noqa: BLE001 -- never replaces the refusal
                pass
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except BaseException:  # noqa: BLE001 -- never replaces the refusal
                pass
        raise RecallStageError(
            f"the fresh per-run verdict file could not be prepared ({refusal}); "
            "nothing was spawned."
        )
    try:
        _certify_none(os.close(descriptor), "closing the fresh verdict file")
    except BaseException as failure:  # noqa: BLE001 -- ONE attempt, every shape
        # Round-5 blocker 2: the handler used to be Exception-only, so a
        # KeyboardInterrupt/SystemExit here left the fresh temp file behind with no
        # cleanup at all. Every shape now runs the SAME cleanup -- one close attempt
        # already spent, so the descriptor is never closed twice, and the temp file is
        # removed best-effort -- after which an ordinary failure becomes the typed
        # refusal and KI/SE propagate untouched. Nothing is spawned on either path.
        try:
            os.unlink(temp_name)
        except BaseException:  # noqa: BLE001 -- cleanup NEVER replaces the primary
            pass
        if not _is_a(failure, Exception):
            raise
        raise RecallStageError(
            "the fresh per-run verdict file could not be prepared "
            f"({_describe(failure)}); nothing was spawned."
        ) from failure
    # Round-7 (D): even Path(temp_name) sits inside the region now. It was the last
    # statement between the descriptor closing and the try, and a Path that refuses to
    # be built left the file orphaned with no cleanup and no spawn -- the same gap as
    # round-6 item 7, one statement narrower. Everything the cleanup needs is the
    # BUILTIN string the mkstemp returned, so no object this function constructs can
    # make the removal unreachable.
    # Round-6 item 7: the file is OURS from the moment the mkstemp descriptor closed, so
    # the cleanup region starts HERE. It used to start after the environment copy, the
    # command construction and time.monotonic() -- three ordinary calls that can fail
    # (a hostile os.environ, a patched clock) and left the fresh verdict file orphaned
    # with no cleanup, no spawn and no diagnosis. Everything that follows the acquisition
    # is now inside the same state machine that removes it.
    cleanup_interrupt: BaseException | None = None
    try:
        verdict_path = _trusted_path(temp_name)
        # Round-11 (6): os.environ is a mapping this module does not own, and copying
        # it iterates it. Only exact string pairs are carried into the child; anything
        # else is a hostile shape and the stage refuses BEFORE the spawn rather than
        # handing it to a subprocess.
        try:
            environment = {
                _exact_str(key, "an environment name"): _exact_str(
                    value, "an environment value"
                )
                for key, value in os.environ.items()
            }
        except BaseException as failure:  # noqa: BLE001 -- the MAPPING chose the shape
            raise RecallStageError(
                f"the process environment could not be copied ({_describe(failure)}); "
                "nothing was spawned."
            ) from None
        for name in _BLAS_THREAD_VARIABLES:
            environment[name] = "1"
        command = [
            sys.executable,
            "-m",
            "bench.harness.recall_worker",
            "--profile",
            profile,
            "--gt",
            gt_mode,
            "--out",
            temp_name,
        ]
        started = _exact_clock(time.monotonic())
        try:
            completed = subprocess.run(
                command,
                env=environment,
                timeout=timeout_seconds,
                capture_output=True,
                text=True,
                check=False,
            )
        except subprocess.TimeoutExpired as failure:
            raise RecallStageError(
                f"the recall worker exceeded {timeout_seconds:g}s on profile "
                f"{profile!r}; nothing was published."
            ) from failure
        # Round-11 (8): both readings are proved exact and finite, and the ordering
        # is checked, BEFORE the subtraction -- a clock that goes backwards or answers
        # with something that is not a float produced a duration the verdict then
        # carried as measurement.
        try:
            # Round-11 (3): the CompletedProcess is snapshotted ONCE, here, and proved.
            # returncode went into comparisons and stdout/stderr into slices and
            # f-strings without ever being shown to be an int and a str -- and every one
            # of those reads happens on a failure path, where a second failure is
            # hardest to see.
            returncode = _exact_int(
                completed.returncode, "the worker exit code", -(2**31)
            )
            stdout = _exact_str(completed.stdout, "the worker stdout")
            stderr = _exact_str(completed.stderr, "the worker stderr")
        except BaseException as failure:  # noqa: BLE001 -- the RESULT chose the shape
            raise RecallStageError(
                f"the recall worker's result could not be read ({_describe(failure)}); "
                "nothing was published."
            ) from None
        ended = _exact_clock(time.monotonic())
        if ended < started:
            raise RecallStageError(
                "the monotonic clock went backwards during the run; the duration "
                "cannot be measured and the verdict would carry a fiction."
            )
        duration = ended - started
        try:
            # The READ is work and keeps its interrupts; the TEXT it returned is
            # data. Boundary review (6): proving it inside this clause meant a str
            # subclass raised RuntimeError past the (OSError, UnicodeError) tuple and
            # left run_recall with a raw error instead of its typed one.
            raw = verdict_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as failure:
            # UnicodeError: a verdict file that is not valid UTF-8 is unreadable, and
            # before this clause it escaped run_recall as UnicodeDecodeError instead
            # of the typed RecallStageError; the finally still cleans the fresh file.
            raise RecallStageError(
                f"the recall worker left no readable verdict "
                f"(exit {returncode}); stdout: {stdout[-400:]!r} "
                f"stderr: {stderr[-400:]!r}"
            ) from failure
        try:
            raw = _exact_str(raw, "the verdict text")
        except Exception as failure:  # noqa: BLE001 -- KI/SE propagate
            raise RecallStageError(
                "the recall worker's verdict is not plain text "
                f"({_describe(failure)}); refusing it."
            ) from None
        if not raw.strip():
            raise RecallStageError(
                "the recall worker wrote nothing into its fresh per-run file "
                f"(exit {returncode}); a stale file can never be mistaken "
                f"for this run's result. stdout: {stdout[-400:]!r} "
                f"stderr: {stderr[-400:]!r}"
            )
        try:
            verdict = json.loads(raw)
        except Exception as failure:  # noqa: BLE001 -- KI/SE still propagate
            # Round-5 blocker 3: the tuple caught only ValueError/RecursionError, so a
            # json.loads raising any other shape (the auditor's EvilError probe) escaped
            # this function untyped; and {failure} re-executed a hostile __str__ inside
            # the refusal itself. Whatever the parse raises, the verdict is unreadable
            # and the stage says so through the guarded describer.
            raise RecallStageError(
                f"the recall worker's verdict is not readable JSON "
                f"(exit {returncode}): {_describe(failure)}"
            ) from failure
        # Round-8 (A): requiring an exact dict at the TOP was not enough -- the
        # values inside it are still whatever the parse produced, so verdict["ok"]
        # could carry a __bool__ that raises and verdict["failure"] a __format__ that
        # raises, both reached below while reporting a failure. The whole tree is
        # rebuilt into exact builtins here, before ANY access, and a document that
        # cannot be rebuilt is refused.
        try:
            # Round-9: this sat OUTSIDE the parse's try, so anything the rebuild raised
            # left run_recall without the RecallStageError its callers are promised.
            # The rebuild contains the deep-tree case itself now; this clause is the
            # local contract, so no future shape can leak past it either.
            verdict = _canonical_json(verdict)
        except Exception as failure:  # noqa: BLE001 -- KI/SE still propagate
            raise RecallStageError(
                "the recall worker's verdict could not be rebuilt as plain JSON-native "
                f"data (exit {returncode}): {_describe(failure)}"
            ) from failure
        if type(verdict) is not dict:
            raise RecallStageError(
                "the recall worker's verdict is not plain JSON-native data; refusing it."
            )
    finally:
        try:
            # Round-7 (D): os.unlink on the BUILTIN name, never verdict_path.unlink().
            # The Path object is built inside the region above, so on the failure path
            # that removal exists it may not exist at all -- and a cleanup that depends
            # on the object that failed is not a cleanup.
            # Round-12 (C): an unlink answering True may not have removed anything, and
            # the run reported success with the temporary still there. Completion is
            # certified, and anything else counts as residue.
            _certify_none(os.unlink(temp_name), "removing the fresh verdict file")
        except BaseException as failure:  # noqa: BLE001 -- never replaces a primary
            # Round-5 blocker 2: this runs inside a finally, so ANY exception raised
            # here replaces what was propagating -- the worker's own typed failure, or
            # the user's KeyboardInterrupt. An Exception-only clause let a KI/SE raised
            # by the unlink itself become the reported outcome, erasing the primary. A
            # leftover temp file is the lesser harm and is not silent: the SUCCESS path
            # below refuses to report success over residue.
            cleanup_failed = True
            if not _is_a(failure, Exception):
                # Round-8 (F): swallowing is only justified while a primary is
                # propagating. If this finally runs on the SUCCESS path, a real
                # interrupt of the unlink syscall is the only event there is, and
                # turning it into a RecallStageError reports a stage failure that did
                # not happen while losing the interrupt that did.
                cleanup_interrupt = failure
        else:
            cleanup_failed = False
    if cleanup_interrupt is not None:
        # Reached only when nothing else was propagating: the lines after a finally do
        # not run while a primary is in flight.
        raise cleanup_interrupt
    if cleanup_failed:
        # Round-5 blocker 2: Path.exists() sat outside every guard, and it can raise --
        # an OSError the stdlib does not fold into False, or a hostile path object --
        # which escaped run_recall untyped. A check that cannot answer counts as
        # RESIDUE: the conservative side of the only honest doubt here.
        try:
            answer = os.path.exists(temp_name)
            # Round-8 (D): the guard covered the CALL but not the value it returned --
            # a replacement exists() could hand back an object whose __bool__ raises,
            # and the truth of it was taken outside this try. Only an exact bool is an
            # answer; anything else leaves the question open, which counts as residue.
            residual = answer if type(answer) is bool else True
        except BaseException:  # noqa: BLE001 -- a diagnosis NEVER decides an outcome
            # Round-6 item 5: this caught Exception only, so exists() raising SystemExit
            # escaped -- and SystemExit(0) is the worst shape available here, because
            # the process would exit SUCCESS with the temp still on disk and nothing
            # published. Asking whether a file exists is a DIAGNOSIS, not work: every
            # shape is absorbed and an unanswerable question counts as residue, which
            # is the conservative side of the only honest doubt in this function.
            residual = True
        if residual:
            raise RecallStageError(
                "the worker verdict was parsed but its fresh per-run file could not be "
                "removed; refusing to report success over residue."
            )
    verdict["duration_seconds"] = duration
    verdict["exit_code"] = returncode
    if returncode != 0 or verdict.get("ok") is not True:
        # Round-8 (A): identity, not truthiness. The verdict is canonical by now, so
        # this is belt and braces -- but "ok" means the worker said True, and any other
        # value, however truthy, is not that. The failure text is described rather than
        # interpolated for the same reason.
        raise RecallStageError(
            f"recall stage failed closed on profile {profile!r}: "
            f"{_describe(verdict.get('failure', f'worker exit {returncode}'))}"
        )
    return verdict


_EXPECTED_HASHES: dict[str, dict[str, str]] = {}


def _expected_hashes(profile) -> dict[str, str]:
    """The recomputed corpus/query digests for one profile, cached per process.

    Pre-review (b): 64 well-formed hex characters are not IDENTITY -- four zeroed
    digests still froze. The only honest comparison is a fresh recomputation from the
    frozen recipe, the same recomputation the freeze test pins.
    """
    cached = _EXPECTED_HASHES.get(profile.name)
    if cached is None:
        corpus = generate_vectors(CORPUS_SEED, profile.corpus_size, profile.dimension)
        queries = generate_vectors(QUERY_SEED, profile.queries, profile.dimension)
        cached = {
            "corpus_sha256_f64": sha256_hex(vector_bytes_f64(corpus)),
            "corpus_sha256_f32": sha256_hex(vector_bytes_f32(corpus)),
            "query_sha256_f64": sha256_hex(vector_bytes_f64(queries)),
            "query_sha256_f32": sha256_hex(vector_bytes_f32(queries)),
        }
        _EXPECTED_HASHES[profile.name] = cached
    return cached


_REACHABLE_MEANS: dict[tuple[int, int, int, int, int], frozenset[float]] = {}


def _reachable_means(
    queries: int, k: int, below: int, m_min: int, total: int
) -> frozenset[float]:
    """Every float mean some REAL histogram produces via fsum([m/k]*counts)/q.

    The auditor's exact DP, benchmarked at 0.56s cold for the worst real case
    (q=256, k=10, below=256, total=1294) and cached per (q, k, below, m_min, total).
    Each grid value's float error e_m = fl(m/k) - m/k is exact as a Fraction; scaling
    by the LCM of their denominators makes them TINY integers (the denominators are
    5*2^t, so |err_int| stays single-digit). One occurrence of m_min is fixed; the
    below-1 remaining non-perfect picks choose from [m_min, k-1] under the exact
    units target; perfect queries contribute error zero. A bitset DP over
    (count, units) -> achievable shifted error sums enumerates every reachable exact
    dyadic fsum, and float(Fraction(total, k) + error) / q reproduces math.fsum's
    correctly rounded sum followed by the one division -- the worker's own pipeline.
    For dyadic k every error is zero and the set collapses to the single exact mean.
    """
    key = (queries, k, below, m_min, total)
    cached = _REACHABLE_MEANS.get(key)
    if cached is not None:
        return cached
    base = Fraction(total, k)
    if below == 0:
        result = frozenset({float(base) / queries})
        _REACHABLE_MEANS[key] = result
        return result
    errors = [Fraction(m / k) - Fraction(m, k) for m in range(k + 1)]
    scale = math.lcm(*[fraction.denominator for fraction in errors])
    err_int = [int(fraction * scale) for fraction in errors]
    choices = list(range(m_min, k))
    free = below - 1
    units_free = (total - (queries - below) * k) - m_min - free * m_min
    span = k - 1 - m_min
    reachable_error_ints: set[int] = set()
    if free == 0:
        if units_free == 0:
            reachable_error_ints.add(err_int[m_min])
    elif 0 <= units_free <= free * span:
        shift_by_value = {m: err_int[m] - min(err_int[m_min:k]) for m in choices}
        smallest = min(err_int[m_min:k])
        # dp[count][units] = bitset of achievable shifted error sums
        dp = [[0] * (units_free + 1) for _ in range(free + 1)]
        dp[0][0] = 1
        for m in choices:
            value_units = m - m_min
            value_shift = shift_by_value[m]
            for count in range(1, free + 1):
                row = dp[count]
                previous = dp[count - 1]
                for units in range(value_units, units_free + 1):
                    source = previous[units - value_units]
                    if source:
                        row[units] |= source << value_shift
        bits = dp[free][units_free]
        offset = err_int[m_min] + free * smallest
        position = 0
        while bits:
            if bits & 1:
                reachable_error_ints.add(offset + position)
            bits >>= 1
            position += 1
    result = frozenset(
        float(base + Fraction(error, scale)) / queries for error in reachable_error_ints
    )
    _REACHABLE_MEANS[key] = result
    return result


class _NotCanonical(Exception):
    """Raised inside the rebuild when a node is not JSON-native with EXACT types."""


_UNCANONICAL = object()
"""Sentinel: the document could not be rebuilt. NOT None -- ``null`` is valid JSON."""


def _canonical_json(node: object) -> object:
    """Deep-rebuild a parsed document into EXACT builtin types, or _UNCANONICAL.

    Round-8 CRITICAL. Parsing is not the same as trusting: json.loads is replaceable, and
    what it returns can be a dict subclass whose __setitem__ silently does nothing, whose
    .get raises, or whose keys are str subclasses that lie to the consumer's __eq__. Every
    guard added around such an object protects one call site; this removes the object.

    The rebuild accepts only None, bool, int, float, str, dict and list -- type-exact,
    subclasses refused -- and requires every mapping key to be an exact str. What comes
    back is JSON-native data with no code of its own, so nothing downstream can be
    hostile: not the reads, not the mutations, not the keys the caller later exports.
    Failure returns the _UNCANONICAL sentinel rather than None, because ``null`` is a
    perfectly valid JSON document and None is its honest rebuild. Conflating the two
    would turn a hostile document into an ABSENT one -- and absent is TOLERATED without
    --require-recall, while malformed never is. That is the difference between a gate
    that refuses a corrupt publication and one that shrugs at it.
    """

    def rebuild(value: object) -> object:
        if value is None or type(value) in (bool, int, float, str):
            return value
        if type(value) is dict:
            rebuilt: dict[str, object] = {}
            for key, item in value.items():
                if type(key) is not str:
                    raise _NotCanonical()
                rebuilt[key] = rebuild(item)
            return rebuilt
        if type(value) is list:
            return [rebuild(item) for item in value]
        raise _NotCanonical()

    try:
        return rebuild(node)
    except (_NotCanonical, RecursionError):
        # Round-9: the rebuild is RECURSIVE, so a type-exact tree deeper than the
        # interpreter's limit raises RecursionError from inside it -- a shape the
        # _NotCanonical clause never saw. The sentinel already means "this cannot be
        # rebuilt", and a document too deep to walk is precisely that: refusing it is
        # the same fail-closed answer, reached for a different reason. Nothing else is
        # caught here, so a genuine KeyboardInterrupt or SystemExit still propagates.
        return _UNCANONICAL


def _canonical_verdict(verdict: object) -> dict[str, object] | None:
    """A deep rebuild into EXACT builtin types, or None -- the TOCTOU antidote.

    Round 3 HIGH-2: a dict subclass can keep ``get`` coherent while ``__getitem__``
    lies later, so validating one read and publishing another lets an impossible pair
    through (section mean 0.0 beside gauge 1.0). The verdict is snapshotted ONCE into
    pure dict/list/str/int/float/bool/None -- exact types only, subclasses refused --
    and that single canonical instance must feed BOTH the validator and the
    publication. Hostile structures that raise during the rebuild surface to the
    caller's boundary as ordinary exceptions.
    """

    result = _canonical_json(verdict)
    return result if type(result) is dict else None


def _validate_verdict(verdict: dict[str, object], profile_name: str) -> str | None:
    """The reason this verdict cannot be published, or None when it is fully coherent.

    Reaudit HIGH-1: the wiring validated only the gauge, so an adulterated verdict --
    gauge 0.99 beside observed 0.01, a foreign hnsw block -- was published while
    ``build_section`` stamped the FROZEN constants over whatever the verdict claimed.
    Everything the section will freeze or the gate will read is therefore checked against
    the declared contract here, and a contradiction is REFUSED, never normalized.
    """
    profile = PROFILES.get(profile_name)
    if profile is None:
        return f"unknown profile {profile_name!r}"
    if verdict.get("ok") is not True:
        return "ok is not True"
    if verdict.get("profile") != profile_name:
        return f"profile {_describe(verdict.get('profile'))} is not {profile_name!r}"
    if verdict.get("generator") != GENERATOR_NAME:
        return (
            f"generator {_describe(verdict.get('generator'))} is not {GENERATOR_NAME!r}"
        )
    if verdict.get("failure") != "":
        return "failure is not the empty string on a success verdict"
    gt_path = verdict.get("gt_path_used")
    oracle = verdict.get("oracle")
    if gt_path not in ("numpy", "pure"):
        return f"gt_path_used {_describe(gt_path)} is neither 'numpy' nor 'pure'"
    if gt_path == "numpy":
        if not profile.oracle.startswith("numpy"):
            return "a pure-oracle profile cannot claim the numpy GT path"
        if oracle != profile.oracle:
            return (
                f"oracle {_describe(oracle)} does not match the profile's declaration"
            )
    if gt_path == "pure":
        if profile.oracle.startswith("numpy"):
            return "a numpy-oracle profile cannot have taken the pure GT path"
        if oracle != "pure-fsum":
            return f"oracle {_describe(oracle)} does not match the pure GT path"
    numpy_field = verdict.get("numpy")
    if gt_path == "numpy":
        if (
            not isinstance(numpy_field, str)
            or not numpy_field
            or numpy_field == "absent"
        ):
            return "the numpy GT path requires a real numpy version string"
    elif numpy_field != "absent":
        return "the pure GT path must record numpy as 'absent'"
    for field, expected in (
        ("k", profile.k),
        ("queries", profile.queries),
        ("corpus_size", profile.corpus_size),
        ("dimension", profile.dimension),
    ):
        value = verdict.get(field)
        # type(value) is int: float equality (10.0 == 10) is a MUTATION, not the profile.
        if isinstance(value, bool) or type(value) is not int or value != expected:
            return f"{field} {_describe(value)} is not the profile's exact {expected}"
    hashes = verdict.get("hashes")
    declared_hashes = {
        "corpus_sha256_f64",
        "corpus_sha256_f32",
        "query_sha256_f64",
        "query_sha256_f32",
    }
    if not isinstance(hashes, dict) or set(hashes) != declared_hashes:
        return "hashes are not exactly the four declared digests"
    for key, digest in hashes.items():
        if not (
            isinstance(digest, str)
            and len(digest) == 64
            and all(ch in "0123456789abcdef" for ch in digest)
        ):
            return f"{key} is not a 64-character lowercase hex digest"
    if hashes != _expected_hashes(profile):
        return "hashes do not equal the profile's recomputed corpus/query digests"
    hnsw = verdict.get("hnsw")
    if not isinstance(hnsw, dict) or set(hnsw) != set(HNSW_FROZEN):
        return "hnsw does not carry exactly the frozen parameter names"
    for key, frozen in HNSW_FROZEN.items():
        value = hnsw[key]
        # Type-exact: dict equality accepts 16.0 == 16, and a float that merely equals
        # the frozen integer is a mutation the section must never launder.
        if type(value) is not type(frozen) or value != frozen:
            return f"hnsw.{key} {_describe(value)} is not exactly the frozen {frozen!r}"
    observed = verdict.get("observed")
    declared_observed = {
        "mean_recall_at_k",
        "min_recall_at_k",
        "queries_below_perfect",
        "dtype_check",
    }
    if not isinstance(observed, dict) or set(observed) != declared_observed:
        return "observed is not exactly its declared keys"
    mean = observed["mean_recall_at_k"]
    minimum = observed["min_recall_at_k"]
    for label, value in (("mean_recall_at_k", mean), ("min_recall_at_k", minimum)):
        if (
            isinstance(value, bool)
            or type(value) is not float
            or not math.isfinite(value)
        ):
            return f"observed.{label} {_describe(value)} is not a finite float"
        if not 0.0 <= value <= 1.0:
            return f"observed.{label} {_describe(value)} is outside [0, 1]"
        if value == 0.0 and math.copysign(1.0, value) < 0.0:
            return f"observed.{label} is negative zero, which the worker never produces"
    if minimum > mean:
        return "observed.min_recall_at_k exceeds the mean"
    below = observed["queries_below_perfect"]
    if (
        isinstance(below, bool)
        or type(below) is not int
        or not 0 <= below <= profile.queries
    ):
        return (
            f"observed.queries_below_perfect {_describe(below)} is not an int "
            "within the query count"
        )
    if (below == 0) != (minimum >= 1.0):
        return "queries_below_perfect and min_recall_at_k contradict each other"
    if (below == 0) != (mean >= 1.0):
        return "queries_below_perfect and mean_recall_at_k contradict each other"
    # Round 3 HIGH-1: recalls@k live on the grid {m/k}, so mean, min and below must be
    # JOINTLY realizable for this profile's (queries, k). min=.75 with below=1 and
    # mean=.99 at q=8,k=4 is arithmetic fiction -- seven perfect queries and one at .75
    # can only average 31/32. The comparisons work in integer grid units, where float
    # division noise is orders of magnitude below half a step, so no tolerance wide
    # enough to accept an impossible combination exists here.
    m_min = next(
        (m for m in range(profile.k + 1) if minimum.hex() == (m / profile.k).hex()),
        None,
    )
    if m_min is None:
        return (
            f"observed.min_recall_at_k {_describe(minimum)} is not on the recall@k "
            f"grid for k={profile.k}"
        )
    grid_units = mean * profile.queries * profile.k
    total = round(grid_units)
    # This rounding only PINS the candidate integer total; the decisive test is the
    # exact fsum-reachable interval below, which collapses to EQUALITY for dyadic k
    # and spans the single ulps a real histogram can reach otherwise. Half a grid
    # unit of slack here cannot admit fiction past that exact check.
    if abs(grid_units - total) > 0.75:
        return (
            f"observed.mean_recall_at_k {_describe(mean)} is not on the recall@k "
            f"grid for queries={profile.queries}, k={profile.k}"
        )
    perfect = profile.queries - below
    if below == 0:
        realizable = total == perfect * profile.k
    else:
        # Every below-perfect query scores in [m_min, k-1], and at least one of them
        # ATTAINS the minimum -- both ends of the sum are exact integers.
        lowest_total = perfect * profile.k + below * m_min
        highest_total = perfect * profile.k + m_min + (below - 1) * (profile.k - 1)
        realizable = lowest_total <= total <= highest_total
    if not realizable:
        return (
            "observed mean/min/queries_below_perfect are not jointly realizable for "
            f"queries={profile.queries}, k={profile.k} (grid total {total})"
        )
    # Round-3 final refinement: a NEIGHBOURHOOD band still crystallized fiction -- at
    # k=4 the whole pipeline is EXACT, so even nextafter(31/32) is impossible, and at
    # k=10 only the means some REAL histogram produces are legitimate. The decisive
    # test is therefore the exact reachable SET (the auditor's benchmarked bitset DP),
    # not any interval around it.
    if mean.hex() not in {
        candidate.hex()
        for candidate in _reachable_means(
            profile.queries, profile.k, below, m_min, total
        )
    }:
        return (
            f"observed.mean_recall_at_k {_describe(mean)} is outside the exact "
            f"fsum-reachable set on the recall@k grid for total {total} at "
            f"queries={profile.queries}, k={profile.k}"
        )
    dtype = observed["dtype_check"]
    if not isinstance(dtype, dict) or set(dtype) != {"mean_overlap", "min_overlap"}:
        return "dtype_check is not exactly its declared keys"
    mean_overlap = dtype["mean_overlap"]
    min_overlap = dtype["min_overlap"]
    for label, value, floor in (
        ("mean_overlap", mean_overlap, DTYPE_MEAN_OVERLAP_MIN),
        ("min_overlap", min_overlap, DTYPE_PER_QUERY_OVERLAP_MIN),
    ):
        if (
            isinstance(value, bool)
            or type(value) is not float
            or not math.isfinite(value)
        ):
            return f"dtype_check.{label} {_describe(value)} is not a finite float"
        if not floor <= value <= 1.0:
            return f"dtype_check.{label} {_describe(value)} is outside [{floor}, 1]"
        if value == 0.0 and math.copysign(1.0, value) < 0.0:
            return (
                f"dtype_check.{label} is negative zero, which the worker never produces"
            )
    if min_overlap > mean_overlap:
        return "dtype_check.min_overlap exceeds the mean overlap"
    gauge = verdict.get("gauge")
    if isinstance(gauge, bool) or type(gauge) is not float or gauge.hex() != mean.hex():
        # hex() is IEEE identity: unlike ==, it distinguishes -0.0 from +0.0.
        return "gauge does not EXACTLY equal observed.mean_recall_at_k"
    blas = verdict.get("blas_environment")
    if (
        not isinstance(blas, dict)
        or set(blas) != set(_BLAS_THREAD_VARIABLES)
        or any(blas[name] != "1" for name in _BLAS_THREAD_VARIABLES)
    ):
        return "blas_environment is not exactly the thread variables pinned to '1'"
    duration = verdict.get("duration_seconds")
    if (
        isinstance(duration, bool)
        or type(duration) is not float
        or not math.isfinite(duration)
        or duration < 0.0
        or (duration == 0.0 and math.copysign(1.0, duration) < 0.0)
    ):
        return (
            f"duration_seconds {_describe(duration)} is not a finite non-negative float"
        )
    exit_code = verdict.get("exit_code")
    if isinstance(exit_code, bool) or type(exit_code) is not int or exit_code != 0:
        return f"exit_code {_describe(exit_code)} is not exactly 0"
    return None


def build_section(
    verdict: dict[str, object], *, target: float = DEFAULT_TARGET
) -> dict[str, object]:
    """Shape one worker verdict into the ``vector_recall`` calibration section (v4 §2).

    ``frozen`` and ``observed.<family>`` are the deterministic projection two same-machine
    runs must reproduce identically; ``provenance.<family>`` holds every volatile value —
    duration, versions, the oracle path actually taken — outside every hash and comparison.

    Schema note, recorded pre-freeze: the observed counter is ``queries_below_perfect``
    (renamed from ``queries_below_target`` before any calibration was ever frozen — the
    count is of NON-PERFECT queries, ``recall < 1.0``, and the old name lied).
    """
    home = family()
    hashes = dict(verdict["hashes"])  # type: ignore[arg-type]
    observed = dict(verdict["observed"])  # type: ignore[arg-type]
    return {
        "schema_version": 1,
        "frozen": {
            "target": target,
            "gate_required": True,
            "k": verdict["k"],
            "queries": verdict["queries"],
            "metric": "cosine",
            "storage_dtype": "float32",
            "dimension": verdict["dimension"],
            "regime_threshold": REGIME_THRESHOLD,
            "corpus": {
                "generator": verdict["generator"],
                "size": verdict["corpus_size"],
                "seed": CORPUS_SEED,
                "sha256_f64": hashes["corpus_sha256_f64"],
                "sha256_f32": hashes["corpus_sha256_f32"],
            },
            "query_set": {
                "held_out": True,
                "seed": QUERY_SEED,
                "sha256_f64": hashes["query_sha256_f64"],
                "sha256_f32": hashes["query_sha256_f32"],
            },
            "hnsw": dict(HNSW_FROZEN),
            "ties": "generous: GT admits every record at or under the k-th distance",
            "gt": {
                "canonical": "pure-python math.fsum",
                "oracle": verdict["oracle"],
                "differential": {
                    "queries": DIFFERENTIAL_QUERIES,
                    "corpus_slice": DIFFERENTIAL_SLICE,
                    "selection": "deterministic by seed",
                },
                "accel_equivalence_eps_rel": ACCEL_EPS_REL,
            },
            "dtype_check": {
                "mean_overlap_min": DTYPE_MEAN_OVERLAP_MIN,
                "per_query_overlap_min": DTYPE_PER_QUERY_OVERLAP_MIN,
            },
        },
        "observed": {home: observed},
        "provenance": {
            home: {
                "profile": verdict["profile"],
                "gt_path_used": verdict["gt_path_used"],
                "numpy": verdict.get("numpy", "absent"),
                "python": sys.version.split()[0],
                "duration_seconds": verdict["duration_seconds"],
                "blas_environment": verdict.get("blas_environment", {}),
            }
        },
    }


def deterministic_projection(section: dict[str, object]) -> str:
    """Serialize ``frozen`` plus ``observed`` canonically — the equality two runs must hold."""
    projection = {"frozen": section["frozen"], "observed": section["observed"]}
    # Boundary review (5): this return value IS the equality two runs are compared on,
    # so a str subclass here decides a freeze comparison with code of its own.
    return _exact_str(
        json.dumps(
            projection, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ),
        "the deterministic projection",
    )
