"""Additive, fail-safe publication of the vector recall stage (C13 v4 §3, atomicity).

The harness computes and writes every legacy output — D5 ceilings, the published metrics
document, the calibration document — unconditionally, exactly as before. THEN this module runs
the recall stage and, only on complete success, appends in a FIXED order:

1. the ``vector_recall`` section into the calibration document (``--out``) — first;
2. the ``oktografx_vector_recall_ratio`` gauge into the metrics document (``--metrics``) — LAST.

The gauge is what the gate consumes, so no reachable state holds a gauge without its section:
a crash between (1) and (2) leaves section-without-gauge, which a ``--require-recall`` gate
reads as UNMEASURED and fails. Every write is read-modify-write through a temporary file and
``os.replace`` in the same directory, so a torn write can never corrupt a legacy document.
Any recall failure returns non-zero with the legacy outputs already intact on disk.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

from bench.harness.recall import (
    RECALL_METRIC,
    RecallStageError,
    _canonical_json,
    _caller_path,
    _certify_none,
    _claim_temp,
    _canonical_verdict,
    _describe,
    _exact_int,
    _exact_str,
    _emit,
    _is_a,
    _trusted_path,
    _validate_verdict,
    build_section,
    run_recall,
)


def _replace_json(path: Path, mutate: Callable[[dict[str, object]], bool]) -> None:
    """Read a JSON document, apply one mutation, and atomically replace the file.

    The scratch file is UNIQUE per call (round-3 B): the old deterministic ``.c13.tmp``
    name let two concurrent stages clobber each other's half-written scratch before the
    replace. A failure unlinks the orphan scratch best-effort and re-raises.
    """
    # Round-8 CRITICAL: this is a SECOND read of a document the caller already
    # validated, and it was mutated exactly as parsed. A json.loads returning a mapping
    # whose __setitem__ quietly does nothing made the mutation a no-op while every
    # other step succeeded: append_vector_recall returned 0 having published the gauge
    # and NOT the section -- an exit 0 that lied, and the precise inversion of the
    # invariant this module exists to hold. Guarding the call sites would not have
    # helped, because nothing raised. The document is rebuilt into exact builtins and
    # ONLY that copy is mutated and serialized, so a mutation cannot be intercepted.
    parsed = json.loads(_exact_str(path.read_text(encoding="utf-8"), "a document"))
    document = _canonical_json(parsed)
    if type(document) is not dict:
        raise RecallStageError(
            f"{path} is not plain JSON-native data, so a mutation of it cannot be "
            "trusted to land; refusing to publish."
        )
    # Round-10 (A): the mutation DECLARES whether it changed anything, and that
    # declaration is the only way not to write. A mutation that merely happened to leave
    # the tree alone must not lead to a write either, because the bytes written would
    # come from a snapshot nobody inspected -- which is the same defect one level down.
    changed = mutate(document)
    if type(changed) is not bool:
        raise RecallStageError(
            f"the mutation of {path} did not declare whether it changed anything; "
            "refusing to publish on an undeclared outcome."
        )
    if not changed:
        # A true no-op: no write, no format churn, no mtime change -- the property the
        # legacy documents are entitled to, now decided ON the snapshot itself.
        return
    # Round-5 blocker 1b: the bytes are produced BEFORE any resource exists. This line
    # used to sit between the mkstemp and the try, an unguarded gap where a mutation
    # that made the document unserializable (or a RecursionError on a deep one) leaked
    # the live descriptor AND the scratch file with nothing to clean either up. Now
    # nothing is acquired until there is something to write.
    # Round-11 (7): json.dumps is a call whose RESULT is concatenated and encoded; a
    # str subclass survives both and reaches os.write as bytes nobody vouched for.
    serialized = _exact_str(
        json.dumps(document, indent=2, sort_keys=True), "the serialized document"
    )
    payload = (serialized + "\n").encode("utf-8")
    made = tempfile.mkstemp(  # WORK: a genuine interrupt propagates.
        prefix=path.name + ".c13-", suffix=".tmp", dir=str(path.parent)
    )
    # Round-12 (B): the same ownership machine as the verdict temp. The wiring used to
    # raise a raw RuntimeError here and leave both the descriptor and the file behind.
    descriptor, scratch_name, refusal = _claim_temp(made)
    if refusal is not None:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except BaseException:  # noqa: BLE001 -- never replaces the refusal
                pass
        if scratch_name is not None:
            try:
                os.unlink(scratch_name)
            except BaseException:  # noqa: BLE001 -- never replaces the refusal
                pass
        raise RecallStageError(
            f"the scratch file could not be prepared ({refusal}); nothing was written."
        )
    closed = False
    # Round-14: set ONLY after a non-None return from os.replace, never by a type.
    keep_scratch = False
    try:
        written = 0
        while written < len(payload):
            remaining = len(payload) - written
            progress = os.write(descriptor, payload[written:])  # WORK
            # Round-5 blocker 1a: `isinstance(progress, int) and progress > 0` accepted
            # True -- bool IS an int and True > 0 -- and accepted any count LARGER than
            # what remained. Either one satisfied the loop while ZERO bytes reached the
            # file, and os.replace then swapped a VALID document for an empty one. A
            # byte count must be an exact int, positive, and no larger than what was
            # left to write; anything else is a write that cannot be trusted.
            if type(progress) is not int or not 0 < progress <= remaining:
                raise RuntimeError(
                    "the scratch write reported an impossible byte count "
                    f"({_describe(progress)} with {remaining} left); refusing to "
                    "replace a valid document with a torn or empty one"
                )
            written += progress
        closed = True
        _certify_none(os.close(descriptor), "closing the scratch file")
        # Round-12 (C): a replace answering True may not have happened, and the stage
        # reported 0 with neither the section nor the gauge in the document and the
        # scratch still on disk. An unproved replace is inconclusive, so the scratch is
        # deliberately KEPT and accounted for by the clause below rather than removed as
        # though the publication had landed.
        replaced = os.replace(scratch_name, path)
        # Round-14: the flag is set only AFTER the call has RETURNED. Round-13 signalled
        # this case with a private exception type, and a type is a channel the call
        # itself can write to: an os.replace that RAISED _InconclusiveReplace was
        # reclassified as our own marker and had its scratch preserved, when an exception
        # from the call means the replace did not happen and the scratch is just debris.
        # Local state cannot be forged by the callee -- reaching this line at all is the
        # proof, and no exception can produce it.
        if replaced is not None:
            keep_scratch = True
            raise RuntimeError(
                f"replacing {path} did not report completion, so it may or may not "
                "have happened"
            )
    except BaseException:
        # Ownership is explicit: the raw descriptor is ours until the single close
        # attempt above. Cleanup is best-effort and can never replace the PRIMARY
        # exception -- round-5 blocker 2: the clauses were Exception-only, so a KI/SE
        # raised by the close or the unlink DID replace it; every shape is swallowed
        # here. Ordinary primaries then die at the stage boundary as exit 3.
        if not closed:
            try:
                os.close(descriptor)
            except BaseException:  # noqa: BLE001 -- never replaces the primary
                pass
        if not keep_scratch:
            # An exception from the call -- including one that happens to be any type at
            # all -- means the replace did not happen, so the scratch is debris. Only a
            # non-None RETURN leaves it in place, because only then is the outcome
            # genuinely unknown and the scratch the one candidate copy.
            try:
                os.unlink(scratch_name)
            except BaseException:  # noqa: BLE001 -- never replaces the primary
                pass
        raise


def _strip_stale_gauge(metrics: Path) -> None:
    """Write step (0): remove EVERY existing recall gauge entry before anything else runs.

    A rerun over a metrics document that already carries the gauge would otherwise leave a
    STALE value satisfying the gate if this run crashed between the section append and the
    new gauge append. Stripping first turns every crash window into gauge-ABSENT -- which a
    ``--require-recall`` gate fails as UNMEASURED -- and a happy rerun ends with exactly one
    entry. Duplicates are removed wholesale; the write is the same atomic read-modify-write
    through a temporary file and ``os.replace`` as every other append. When the document
    holds NO recall entry -- the first run, and every legacy document -- this is a strict
    no-op: no rewrite, no format churn, no mtime change, so a failure before any append
    still leaves both documents byte-identical. The document is guaranteed readable by the
    caller's pre-validation, so nothing is swallowed here: any residual failure propagates
    and aborts the stage BEFORE the worker is spawned.

    Stale boundary, stated precisely: os.replace is atomic, so a failure BEFORE the replace
    leaves the OLD document -- stale gauge included -- fully intact. In that case this
    stage exits 3 and the workflow stops before the gate step ever runs, which is the
    containment this cycle relies on. A contract that ran the gate INDEPENDENTLY of the
    stage exit would need generation binding between section and gauge; that evolution is
    recorded here rather than half-built.
    """

    # Round-10 (A): there is no decision read any more. This step used to read the
    # document to decide whether a stale gauge existed and then, through _replace_json,
    # read it AGAIN to remove one -- two snapshots, so the decision and the action could
    # disagree. A second read answering `{}` made the strip skip while the file still
    # carried a stale gauge, so the run published a second one; and a second read
    # answering `{}` from INSIDE _replace_json published that empty tree over the real
    # one, destroying the legacy ceiling metrics with exit 0. One read decides and acts.
    def mutate(document: dict[str, object]) -> bool:
        entries = document.get("metrics")
        if not _is_a(entries, list):
            # Nothing here could hold a gauge, and this is not our document to rewrite.
            return False
        kept = [
            entry
            for entry in entries
            if not (_is_a(entry, dict) and entry.get("name") == RECALL_METRIC)
        ]
        if len(kept) == len(entries):
            return False
        document["metrics"] = kept
        return True

    _replace_json(metrics, mutate)


def _append_section(out: Path, section: dict[str, object]) -> None:
    """Write step (1): the calibration document gains its ``vector_recall`` section."""

    def mutate(document: dict[str, object]) -> bool:
        document["vector_recall"] = section
        return True

    _replace_json(out, mutate)


def _append_gauge(metrics: Path, value: float) -> None:
    """Write step (2), LAST: the metrics document gains the recall gauge the gate reads."""

    def mutate(document: dict[str, object]) -> bool:
        # Round-10 (A), the deepest instance: `setdefault` CREATED the list when the
        # snapshot did not have one, so a read that came back `{}` produced a document
        # holding nothing but the new gauge -- and the legacy ceiling metrics, which the
        # prevalidation had just seen, were gone. This module only ever ADDS, so a
        # snapshot with no metric list is not the document that was validated, and
        # publishing into it would be inventing a file rather than appending to one.
        entries = document.get("metrics")
        if entries is None:
            raise RecallStageError(
                "the metrics document lost its metric list between validation and "
                "publication; refusing to publish a document that would replace it."
            )
        if not _is_a(entries, list):
            # Round-3 B: the document can change between the precheck and this append.
            # Silently skipping used to return SUCCESS with section-without-gauge --
            # an exit 0 that lied. Raising turns it into the boundary's typed exit 3.
            raise RuntimeError(
                "the metrics document's 'metrics' key is no longer a list; the gauge "
                "cannot land, and success would be a lie"
            )
        entries.append(
            {
                "name": RECALL_METRIC,
                "kind": "gauge",
                "unit": "ratio",
                "samples": [{"value": value}],
            }
        )
        return True

    _replace_json(metrics, mutate)


def _release_publication_locks(
    held: list[Path],
) -> tuple[list[str], BaseException | None]:
    """Unlink held locks in REVERSE order; report residue and any real interrupt.

    Round-8 (F): absorbing every shape keeps the cleanup complete, but absorbing a
    KeyboardInterrupt or SystemExit raised by the unlink SYSCALL also erased it when
    there was nothing else to report -- a real interrupt of a successful run came back
    as exit 3. Swallowing is only justified while a primary exception is propagating,
    because then the interrupt would REPLACE something more accurate. So the first
    non-ordinary shape is remembered and handed to the caller, which knows whether a
    primary exists: the unwind paths discard it, and the success path re-raises it.
    """
    residue: list[str] = []
    interrupted: BaseException | None = None
    for lock_path in reversed(held):
        try:
            # Round-12 (C): an unlink answering True may not have removed the lock,
            # and the run reported success with the field still locked.
            _certify_none(os.unlink(str(lock_path)), "releasing the lock")
        except BaseException as failure:  # noqa: BLE001 -- cleanup CONTINUES, any shape
            # Round-5 blocker 2: an Exception-only clause let a KeyboardInterrupt or
            # SystemExit raised BY THE UNLINK abort the reverse release, leaving every
            # remaining lock on disk; and this function runs while a primary exception
            # may already be propagating, so raising out of it would replace that
            # primary with a cleanup accident. Every shape is absorbed, the loop always
            # reaches the last lock, and the failure becomes residue diagnosis. The
            # diagnosis is itself guarded -- describing a hostile failure must not be
            # what aborts the cleanup -- and falls back to a CONSTANT.
            if interrupted is None and not _is_a(failure, Exception):
                interrupted = failure
            try:
                residue.append(f"{lock_path} ({_describe(failure)})")
            except BaseException:  # noqa: BLE001 -- the diagnosis is best-effort too
                residue.append("<a lock whose failure could not be described>")
    return residue, interrupted


def _normalized_name(resolved: object) -> str:
    """Reduce what resolve() RETURNED to a builtin string; never raise a foreign shape.

    Round-9 (5): resolve() itself is work, and a genuine interrupt of it must propagate.
    The object it hands back is a different matter -- os.fspath on that result runs the
    RETURNED object's __fspath__, and sharing one clause with the call let a SystemExit
    from there escape as though the filesystem had been interrupted. The call keeps its
    Exception clause; this normalization is data, so every shape becomes the ordinary
    error the caller already turns into a typed refusal.
    """
    try:
        name = os.fspath(resolved)
    except BaseException as failure:  # noqa: BLE001 -- the RESULT chose this shape
        raise RuntimeError(
            f"resolve() returned something that is not a path ({_describe(failure)})"
        ) from None
    if type(name) is not str:
        raise RuntimeError("resolve() did not yield a filesystem path")
    return name


class _InconclusiveLock(Exception):
    """The lock may exist and may hold a descriptor nobody can name.

    Round-12 (D). os.open returning something that is not a descriptor does not mean it
    did nothing: the file may be there and a descriptor may be live. Reporting "not
    created" would be a claim the code cannot support, and releasing the pathname would
    invite a second stage onto a field that may still be held. Fail-closed: the name
    stays blocked, the possibility is recorded, and the stage refuses.
    """

    def __init__(self, lock_path: object) -> None:
        super().__init__(lock_path)
        self.lock_path = lock_path


def _unwind_lock_failure(
    descriptor: int | None, lock_path: Path, held: list[Path]
) -> tuple[Path | None, list[str]]:
    """Release what is certainly ours after ONE close attempt; never raise.

    Round-5 blocker 2. Shared by every acquisition failure path so the ordinary and the
    KeyboardInterrupt/SystemExit unwinds cannot drift apart. Exactly one close is
    attempted on the descriptor -- a second would risk a reused descriptor number -- and
    a close that did not certainly succeed leaves ITS lock file in place, because
    unlinking over a possibly-live descriptor would let another process acquire. The
    locks that are certainly released are released. Nothing here raises, whatever the
    shape: a primary exception may be propagating through this call, and a cleanup that
    replaced it would destroy the only accurate account of what went wrong.

    Returns the lock left behind as uncertain (or None) and the release residue.
    """
    close_ok = True
    if descriptor is not None:
        try:
            # Round-13: certified here too. A close answering True leaves a descriptor
            # that may still be open, and treating it as done let the unwind REMOVE a
            # lock whose descriptor may be live -- the one outcome this helper exists to
            # prevent. One attempt, as always.
            _certify_none(os.close(descriptor), "closing the lock descriptor")
        except BaseException:  # noqa: BLE001 -- cleanup NEVER replaces the primary
            close_ok = False
    uncertain = None
    if not close_ok and held and held[-1] == lock_path:
        uncertain = held.pop()
    # A primary is propagating through every caller of this helper, so an interrupt
    # raised by the cleanup itself is deliberately dropped: it would replace a more
    # accurate account of what went wrong.
    residue, _ = _release_publication_locks(held)
    return uncertain, residue


def _acquire_publication_locks(
    documents: list[Path],
) -> tuple[list[Path], str | None]:
    """Acquire one O_EXCL lock per document, sorted, transactionally.

    Returns (held, None) on success, or ([], reason) after unwinding every lock
    already held. Deterministic ordering makes opposite caller argument orders
    take the locks in the same sequence, so two stages can contend but never
    deadlock. Any failure between creation and close -- injected or real --
    closes the descriptor, releases everything, and surfaces typed; ordinary
    exceptions become the reason, KeyboardInterrupt/SystemExit propagate after
    the same cleanup.

    The three phases -- create, stamp, close -- are kept apart on purpose: only the
    CREATE can conclude that another stage holds the lock, and only the phases after it
    own a descriptor. Collapsing them is what let a FileExistsError from the stamp
    masquerade as contention.
    """
    try:
        # Round-6: str() on a caller-supplied path-like is the CALLER's code, and it ran
        # outside every guard -- a __str__ raising SystemExit escaped before a single
        # lock existed. A document that cannot even be named cannot be locked.
        ordered = sorted({str(document) for document in documents})
        # Round-8 (C): the lock NAMES are built here, before a single lock exists.
        # Building them inside the acquisition loop meant the second Path could fail
        # after the first lock was already held, and the failure path for a name that
        # does not exist yet had nothing to release it with -- an orphaned lock left on
        # disk by a stage that never ran. Nothing is acquired until every name is known.
    except BaseException as failure:  # noqa: BLE001 -- naming runs ONLY caller code
        # Round-7 (C): this used to re-raise anything that was not an ordinary
        # Exception, which handed the caller a SystemExit the DATA had fabricated --
        # str() on a caller-supplied object is the caller's code and nothing else. This
        # region converts every shape into a typed refusal. The real lock operations
        # below keep the ordinary-vs-KI/SE distinction, because they do actual work.
        return [], (
            "a document could not be named for locking "
            f"({_describe(failure)}); nothing was locked and nothing was run"
        )
    # Round-11 (1), found while probing: building the lock names is OUR work, and it sat
    # inside the region above -- which absorbs every shape precisely because `str()` on a
    # caller's object is the caller's code. So a genuine interrupt of Path() was being
    # converted into a typed refusal by a rule written for a different reason. The two
    # halves are separated the same way resolve and stat were: naming from the caller's
    # objects absorbs, constructing our own paths does not.
    try:
        lock_paths = [_trusted_path(target + ".c13.lock") for target in ordered]
    except Exception as failure:  # noqa: BLE001 -- KI/SE propagate; this is OUR work
        # An ordinary failure building our own names is a typed refusal, exactly like a
        # failure of any other call this module makes. What must NOT happen here is the
        # round-9 behaviour of absorbing every shape: a real interrupt of the
        # constructor is an interrupt of work and belongs to the caller.
        return [], (
            "the publication lock names could not be built "
            f"({_describe(failure)}); nothing was locked and nothing was run"
        )
    held: list[Path] = []
    for lock_path in lock_paths:
        # Round-5: the CREATION and the STAMPING are separate phases, because
        # FileExistsError means completely different things in each. The single clause
        # that used to span both read a FileExistsError from os.write -- raised AFTER we
        # created the lock, with OUR descriptor open -- as "another stage holds it": it
        # told the operator to delete a file this process had just made, and it left the
        # descriptor open, since the contention path has no descriptor to close.
        try:
            opened = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            if type(opened) is not int or opened < 0:
                # Round-12 (D): the call may well have CREATED the file and opened a
                # descriptor we can no longer name. There is nothing to close and
                # nothing that can honestly be called absent, so ownership is
                # inconclusive: the pathname stays blocked and accounted for, and the
                # diagnosis says so instead of claiming it was never created.
                raise _InconclusiveLock(lock_path)
            descriptor = opened
        except _InconclusiveLock as inconclusive:
            # The certain locks are released; THIS pathname is not, because it may hold
            # a descriptor we cannot close. It is named as possibly live.
            residue, _ = _release_publication_locks(held)
            reason = (
                f"the publication lock {inconclusive.lock_path} may exist and may hold "
                "an open descriptor: the open call answered with something that is not "
                "a descriptor, so neither closing nor removing it is possible. The name "
                "is left blocked deliberately; confirm no stage runs before removing it."
            )
            if residue:
                reason += f" (release residue: {residue})"
            return [], reason
        except FileExistsError:
            # ONLY the open can conclude contention, and only here does no descriptor
            # of ours exist.
            residue, _ = _release_publication_locks(held)
            reason = (
                f"{lock_path} exists, so another stage holds (or died holding) "
                "the publication lock; refusing to interleave. Remove the file "
                "only after confirming no stage runs."
            )
            if residue:
                reason += f" (release residue: {residue})"
            return [], reason
        except BaseException as failure:  # noqa: BLE001 -- ONE unwind for every shape
            # Nothing was created on this path, so there is no descriptor and no lock
            # of ours to leave behind; only the locks already held are released.
            _, residue = _unwind_lock_failure(None, lock_path, held)
            if not _is_a(failure, Exception):
                raise
            reason = (
                f"the publication lock {lock_path} could not be created "
                f"({_describe(failure)})"
            )
            if residue:
                reason += f" (release residue: {residue})"
            return [], reason
        # The lock file exists from here on: it is OURS, and every exit below must
        # account for it.
        held.append(lock_path)
        try:
            # Round-11 (2): a lock whose stamp was never written is a lock that
            # certifies nothing. The pid is proved an exact positive int, and the write
            # must report an exact count covering the whole stamp -- os.open answering
            # True and os.write answering True or 0 both produced a "held" lock with an
            # empty file behind it.
            stamp = str(_exact_int(os.getpid(), "the process id", 1)).encode("ascii")
            # Boundary review: a partial write is legal for os.write, so the contract is
            # the same progress LOOP the scratch writer uses -- each report an exact int
            # within what is left, and the loop ends only when every byte is accounted
            # for. Refusing a partial write outright would have turned a legitimate
            # short write into a failure.
            stamped = 0
            while stamped < len(stamp):
                remaining = len(stamp) - stamped
                progress = _exact_int(
                    os.write(descriptor, stamp[stamped:]), "the stamp byte count", 1
                )
                if progress > remaining:
                    raise RuntimeError("the lock stamp write reported too many bytes")
                stamped += progress
        except BaseException as failure:  # noqa: BLE001 -- ONE unwind for every shape
            # Round-5 blocker 2: the ordinary and the KI/SE paths each carried their
            # OWN copy of the unwind, and both closed the descriptor under an
            # Exception-only clause -- so a KI/SE raised by the CLEANUP replaced the
            # primary. There is now a single unwind, it absorbs every shape, and the
            # two paths differ only in what they do afterwards: an ordinary failure
            # becomes the typed refusal, KI/SE propagate untouched. A FileExistsError
            # reaching HERE is just another ordinary failure of the stamping -- never
            # a claim about who holds the lock.
            uncertain, residue = _unwind_lock_failure(descriptor, lock_path, held)
            if not _is_a(failure, Exception):
                raise
            reason = (
                f"the publication lock {lock_path} was created but could not be "
                f"stamped ({_describe(failure)})"
            )
            if uncertain is not None:
                reason += (
                    f"; {uncertain} is left in place because the descriptor state "
                    "is uncertain"
                )
            if residue:
                reason += f" (release residue: {residue})"
            return [], reason
        try:
            # Round-13: a close answering True is not a closed descriptor. It joins the
            # uncertain-close machine below, which leaves the lock file in place and
            # refuses -- never publishing behind a lock it cannot prove it released.
            _certify_none(os.close(descriptor), "closing the lock descriptor")
        except BaseException as failure:  # noqa: BLE001 -- ONE attempt, every shape
            # ONE close attempt, never a retry: a close that closed and THEN raised
            # would make a second close reach a possibly-reused descriptor number.
            # And because the descriptor state is now UNCERTAIN, this lock file is
            # deliberately LEFT IN PLACE -- unlinking it could let another process
            # acquire while our descriptor possibly lives. The certain locks are
            # released, the refusal is typed, and the leftover is named as residue.
            # The OS closes the descriptor when the process ends.
            #
            # Round-5 blocker 2: this clause was Exception-only, so a KI/SE from the
            # close of the SECOND lock left the descriptor open AND both lock files on
            # disk -- the worst residue of any path here. Now every shape releases the
            # certain locks first; only then does an ordinary failure become the typed
            # refusal, or a KI/SE propagate.
            uncertain = held.pop()
            residue, _ = _release_publication_locks(held)
            if not _is_a(failure, Exception):
                raise
            reason = (
                f"the lock descriptor for {lock_path} could not be closed "
                f"({_describe(failure)}); {uncertain} is left in place because the "
                "descriptor state is uncertain"
            )
            if residue:
                reason += f" (release residue: {residue})"
            return [], reason
    return held, None


def _publish_documents(
    *,
    profile: str,
    gt_mode: str,
    out: Path | None,
    metrics: Path | None,
    scratch_root: Path,
    timeout_seconds: float | None,
) -> int:
    """The measurement and publication flow, entered ONLY with the lock held."""
    try:
        if metrics is not None:
            _strip_stale_gauge(metrics)
        verdict = run_recall(
            profile,
            gt_mode=gt_mode,
            scratch=scratch_root,
            timeout_seconds=timeout_seconds,
        )
    except RecallStageError as failure:
        # Round-5 blocker 3: even OUR typed error prints through the guarded describer.
        # RecallStageError carries whatever text built it, and one of those texts is
        # built from a worker verdict; a str that raises here would crash the refusal.
        _emit(f"vector recall: FAIL-CLOSED -- {_describe(failure)}")
        return 3
    except Exception as failure:  # noqa: BLE001 -- any ordinary failure is exit 3 typed
        # An ORDINARY exception from the strip or the worker is still a stage failure:
        # typed line, exit 3, no traceback -- and because the strip precedes the spawn,
        # a strip failure aborts before any measurement begins. Exception, not
        # BaseException: KeyboardInterrupt and SystemExit still propagate.
        _emit(f"vector recall: stage failed before publication -- {_describe(failure)}")
        return 3
    # Reaudit HIGH-1: the WHOLE verdict is validated before the first append -- an
    # adulterated verdict (gauge 0.99 beside observed 0.01, NaN inside observed, a
    # foreign hnsw block) was published with exit 0 while build_section stamped the
    # frozen constants over it. Contradictions are refused, never normalized; the
    # documents stay exactly as the strip left them, and junk is only ever printed
    # through the guarded describer inside the validator.
    try:
        # Round 3 HIGH-2 (TOCTOU): the verdict is snapshotted ONCE into exact builtin
        # types; subclasses that could answer validation and publication differently
        # are refused outright, and the SAME canonical instance feeds both.
        canonical = _canonical_verdict(verdict)
        reason = (
            "the verdict is not canonical builtin data"
            if canonical is None
            else _validate_verdict(canonical, profile)
        )
    except Exception as failure:  # noqa: BLE001 -- a hostile mapping may raise anywhere
        # A dict SUBCLASS can pass isinstance and then raise from get/__eq__ inside the
        # validator; the boundary converts that into the same typed refusal. Exception,
        # never BaseException: KI and SystemExit still propagate.
        _emit(
            "vector recall: FAIL-CLOSED -- verdict validation itself failed: "
            f"{_describe(failure)}"
        )
        return 3
    if reason is not None:
        _emit(f"vector recall: FAIL-CLOSED -- incoherent verdict: {reason}")
        return 3
    gauge_value = float(canonical["gauge"])  # validated: exact float == observed mean
    try:
        section = build_section(canonical)
        if out is not None:
            _append_section(out, section)
        if metrics is not None:
            _append_gauge(metrics, gauge_value)
    except Exception as failure:  # noqa: BLE001 -- any ordinary failure is exit 3 typed
        # build_section over a malformed ok-verdict raises KeyError/LookupError -- shapes
        # the old four-type tuple missed. It runs INSIDE this boundary, before
        # _append_section, so a failure here leaves both documents exactly as the strip
        # left them. Exception, not BaseException: KI and SystemExit still propagate.
        _emit(
            "vector recall: publication failed after measurement -- "
            f"{_describe(failure)}"
        )
        return 3
    home = next(iter(section["observed"]))  # type: ignore[call-overload]
    _emit(
        f"vector recall: profile {profile} mean recall@k "
        f"{gauge_value:.4f} ({home}); section first, gauge last."
    )
    return 0


def append_vector_recall(
    *,
    profile: str,
    gt_mode: str,
    out: Path | None,
    metrics: Path | None,
    workspace: Path,
    timeout_seconds: float | None = None,
) -> int:
    """Run the recall stage and append its results in the fail-safe order; return exit code.

    Zero means both appends landed. Any failure — the worker refusing, a missing document, a
    torn append — returns non-zero WITHOUT touching what was already written before the
    failure point, so the legacy outputs always survive and a partial vector publication can
    only ever be section-without-gauge, never the reverse.
    """
    if metrics is not None and out is None:
        _emit(
            "vector recall stage: REFUSED -- --metrics without --out would publish the "
            "gauge with no section; the gauge must be the LAST artifact, never the only one."
        )
        return 3
    # Round-4 aliasing: locks by TEXT do not serialize physical identity -- symlinked
    # and hardlinked names of one file acquired simultaneously in two processes. Every
    # document is resolved to its canonical path (symlinks and parents), and THAT path
    # is what the locks and the publication use. Hardlinked documents are refused
    # outright: os.replace necessarily breaks the link relation, so no atomic
    # publication coherent across all names exists. Two arguments resolving to the
    # SAME file are refused for the same reason.
    resolved: dict[str, str] = {}
    for alias_label, alias_path in (("--out", out), ("--metrics", metrics)):
        if alias_path is None:
            continue
        try:
            # Round-8 (E), step one -- DATA. os.fspath runs the ARGUMENT's __fspath__,
            # so the shape raised here is the caller's object choosing it, and every
            # shape becomes a typed refusal. Round-7 got this half right but ran
            # resolve() inside the same clause, which meant a REAL interrupt of the
            # filesystem call was absorbed too. The argument is reduced to a builtin
            # string first; nothing after this line touches the caller's object.
            alias_name = os.fspath(alias_path)
            if type(alias_name) is not str:
                raise TypeError("the argument is not a filesystem path")
        except BaseException as failure:  # noqa: BLE001 -- the ARGUMENT chose the shape
            _emit(
                f"vector recall stage: REFUSED -- {alias_label} "
                f"{_describe(alias_path)} is not a usable filesystem path "
                f"({_describe(failure)}); nothing was run."
            )
            return 3
        try:
            # Step two -- WORK. resolve() walks the filesystem and Path() is ours, so a
            # genuine KeyboardInterrupt or SystemExit here is an interrupt of work and
            # keeps propagating; an ordinary failure, RuntimeError included, is a typed
            # refusal with no fallback.
            resolved_object = _trusted_path(alias_name).resolve(strict=True)
            # Round-9: this construction sat OUTSIDE every boundary. A Path replacement
            # that allows the first construction and refuses the second escaped as a raw
            # RuntimeError instead of the typed exit 3 -- and it is the same kind of
            # call as the resolve above, so it belongs under the same clause.
            canonical_name = _exact_str(
                _normalized_name(resolved_object), "the canonical name"
            )
        except Exception as failure:  # noqa: BLE001 -- KI/SE propagate; this is I/O
            _emit(
                f"vector recall stage: REFUSED -- {alias_label} "
                f"{_describe(alias_path)} could "
                "not be resolved to a physical document "
                f"({_describe(failure)}); nothing was run."
            )
            return 3
        try:
            # Real I/O, on a builtin string: a genuine KeyboardInterrupt or SystemExit
            # here is an interrupt of WORK and keeps propagating as the primary.
            stat_result = os.stat(canonical_name)
        except Exception as failure:  # noqa: BLE001 -- KI/SE propagate; this is I/O
            _emit(
                f"vector recall stage: REFUSED -- {alias_label} "
                f"{_describe(alias_path)} could not be inspected on disk "
                f"({_describe(failure)}); nothing was run."
            )
            return 3
        try:
            # Round-10 (B): the SYSCALL above is work and keeps its interrupts, but what
            # it RETURNED is not ours -- reading st_nlink and comparing it runs the
            # returned object's code, and an st_nlink whose __gt__ raises escaped with
            # no lock taken. Reading it is a data boundary, and the value must be an
            # exact int before any comparison happens.
            link_count = stat_result.st_nlink
        except BaseException as failure:  # noqa: BLE001 -- the RESULT chose the shape
            _emit(
                f"vector recall stage: REFUSED -- {alias_label} "
                f"{_describe(alias_path)} reported an unreadable link count "
                f"({_describe(failure)}); nothing was run."
            )
            return 3
        if type(link_count) is not int or link_count < 1:
            _emit(
                f"vector recall stage: REFUSED -- {alias_label} "
                f"{_describe(alias_path)} reported a link count that is not a positive "
                f"integer ({_describe(link_count)}); nothing was run."
            )
            return 3
        if link_count > 1:
            _emit(
                f"vector recall stage: REFUSED -- {alias_label} "
                f"{_describe(alias_path)} is "
                f"hardlinked (st_nlink={link_count}); os.replace would break the "
                "aliases, so no coherent atomic publication exists. Nothing was run."
            )
            return 3
        resolved[alias_label] = canonical_name
    if (
        "--out" in resolved
        and "--metrics" in resolved
        and resolved["--out"] == resolved["--metrics"]
    ):
        _emit(
            "vector recall stage: REFUSED -- --out and --metrics resolve to the SAME "
            "physical document; the section and the gauge need distinct files."
        )
        return 3
    # Round-10 (B): the Paths are built HERE, from the builtin names, each proved to
    # be the path it was asked for. Nothing constructed earlier survives to this point.
    try:
        if "--out" in resolved:
            out = _trusted_path(resolved["--out"])
        if "--metrics" in resolved:
            metrics = _trusted_path(resolved["--metrics"])
    except Exception as failure:  # noqa: BLE001 -- KI/SE propagate
        _emit(
            "vector recall stage: REFUSED -- a canonical path could not be rebuilt "
            f"({_describe(failure)}); nothing was run."
        )
        return 3
    for label, path in (("--out", out), ("--metrics", metrics)):
        if path is None:
            continue
        try:
            # Round-9: this parse was raw, so the prevalidation was inspecting an object
            # while the commit claimed every parse is canonicalized. It agrees with the
            # later reads now -- though each read still validates its OWN snapshot,
            # because agreement here is not evidence about a later one.
            document = _canonical_json(
                json.loads(_exact_str(path.read_text(encoding="utf-8"), "a document"))
            )
        except Exception as failure:  # noqa: BLE001 -- absolute boundary; KI/SE propagate
            _emit(
                f"vector recall stage: REFUSED -- {label} {_describe(path)} is not "
                f"a readable JSON document ({_describe(failure)}); nothing was run "
                "and nothing was written."
            )
            return 3
        if type(document) is not dict:
            _emit(
                f"vector recall stage: REFUSED -- {label} {_describe(path)} holds "
                f"{_describe(document)}, not an object; nothing was run."
            )
            return 3
        if label == "--metrics" and "metrics" not in document:
            # Round-11, ratified: schema v1 always carries the metric list, and this
            # module only ever ADDS. A metrics document without one is not a document
            # this stage can append to, and refusing HERE means the worker never runs,
            # no lock or scratch is created, and both files keep their bytes and mtime.
            # The late check inside the append stays as the TOCTOU guard: agreement
            # here is not evidence about the snapshot that will actually be written.
            _emit(
                f"vector recall stage: REFUSED -- {label} {_describe(path)} carries no "
                "'metrics' list; this stage only appends to one. Nothing was run."
            )
            return 3
        try:
            # Round-7 (5): `"metrics" in document` runs the OBJECT's __contains__ and
            # the indexing runs its __getitem__ -- a dict subclass reached this line
            # before any lock existed and chose its own shape. Inspecting parsed data
            # can never be what ends the process; a document that cannot answer whether
            # it carries a usable metrics list is refused as though it did not.
            metrics_key_unusable = (
                label == "--metrics"
                and "metrics" in document
                and not _is_a(document["metrics"], list)
            )
        except BaseException:  # noqa: BLE001 -- inspection of parsed data only
            metrics_key_unusable = True
        if metrics_key_unusable:
            _emit(
                f"vector recall stage: REFUSED -- {label} {_describe(path)} carries a "
                "'metrics' key that is not a list; a gauge could never land there. "
                "Nothing was run."
            )
            return 3
    # Round-3 B, hardened per the lock blockers: EVERY non-None document gets its
    # own O_EXCL lock (a single-target lock let two runs sharing out but not
    # metrics interleave on the section), acquired in deterministic sorted order
    # so opposite argument orders cannot deadlock, and released in reverse. The
    # acquisition itself is transactional: a failure while creating, writing or
    # closing any lock unwinds every lock already held before the typed refusal --
    # and KeyboardInterrupt/SystemExit unwind too, then propagate. A normal run
    # whose release leaves residue prints a diagnostic rather than staying silent.
    # Boundary review (3): the join `workspace / "recall"` ran the CALLER's object,
    # and it happened inside the publication -- after the locks were taken and with the
    # worker about to start. The workspace is reduced and proved HERE, before anything
    # is acquired, so the scratch root handed downstream is one of ours.
    try:
        # Round-12 (A): the helper holds BOTH boundaries, so this clause is Exception --
        # an interrupt of OUR construction belongs to the caller. The previous
        # BaseException here swallowed it, which is the same mistake the scratch had.
        scratch_root = _caller_path(workspace, "the workspace") / "recall"
    except Exception as failure:  # noqa: BLE001 -- KI/SE propagate; this is OUR work
        _emit(
            "vector recall stage: REFUSED -- the workspace is not a usable path "
            f"({_describe(failure)}); nothing was run."
        )
        return 3
    held_locks, lock_refusal = _acquire_publication_locks(
        [path for path in (out, metrics) if path is not None]
    )
    if lock_refusal is not None:
        _emit(f"vector recall stage: REFUSED -- {lock_refusal}")
        return 3
    try:
        outcome = _publish_documents(
            profile=profile,
            gt_mode=gt_mode,
            out=out,
            metrics=metrics,
            scratch_root=scratch_root,
            timeout_seconds=timeout_seconds,
        )
    except BaseException:
        # Round-8 (F): a primary is propagating. The locks are still released and the
        # residue still reported, but anything the cleanup itself raises is dropped --
        # replacing the primary would destroy the only accurate account of the failure.
        primary_residue, _ = _release_publication_locks(held_locks)
        for residue_line in primary_residue:
            _emit(
                "vector recall stage: WARNING -- lock residue left behind: "
                f"{residue_line}"
            )
        raise
    residue, interrupted = _release_publication_locks(held_locks)
    for residue_line in residue:
        _emit(
            f"vector recall stage: WARNING -- lock residue left behind: {residue_line}"
        )
    if interrupted is not None:
        if outcome != 0:
            # Round-9 (6): a non-zero outcome IS a primary -- the stage already decided,
            # in a typed refusal the caller is entitled to. Re-raising here replaced that
            # verdict with an interrupt raised by the cleanup of it, which is the same
            # mistake round-8 fixed in the other direction. Diagnosed and dropped; the
            # refusal stands.
            _emit(
                "vector recall stage: WARNING -- the lock release was interrupted "
                f"({_describe(interrupted)}); the stage's own failure stands."
            )
            return 3
        # Round-8 (F): the publication SUCCEEDED, so this interrupt is the only thing
        # that happened. Absorbing it turned a real KeyboardInterrupt during cleanup into
        # a quiet exit 3 -- a stage failure the operator never asked for and an interrupt
        # they did. It reappears here, after the cleanup completed and was reported.
        raise interrupted
    if outcome == 0 and residue:
        # Round-4: exit 0 with persisting locks would tell the next stage the field
        # is clear while the files say otherwise. Success is demoted to the typed
        # failure; the diagnostics above name every leftover.
        _emit(
            "vector recall stage: FAIL -- the publication succeeded but releasing "
            "the locks left residue behind; refusing to report success over a "
            "locked field."
        )
        return 3
    return outcome


def main(argv: list[str] | None = None) -> int:
    """Run the vector stage as its own CI step, fail-closed by exit code.

    The CI job runs the legacy harness first (its own step, with the legacy wheel-absence
    tolerance), then THIS entrypoint without any tolerance: a vector failure fails the job
    before the gate step ever runs, and the appends keep the fixed order — section first,
    gauge last.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="python -m bench.harness.recall_wiring")
    parser.add_argument("--profile", required=True, choices=("smoke", "full", "tiny"))
    parser.add_argument("--gt", default="auto", choices=("auto", "pure"))
    parser.add_argument(
        "--out", default=None, help="calibration document to append into"
    )
    parser.add_argument(
        "--metrics", default=None, help="metrics document to append into"
    )
    parser.add_argument(
        "--workspace", required=True, help="scratch directory for the worker"
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=None,
        help="override the per-profile wall-clock ceiling (defaults to PROFILE_TIMEOUTS)",
    )
    arguments = parser.parse_args(argv)
    return append_vector_recall(
        profile=arguments.profile,
        gt_mode=arguments.gt,
        out=_trusted_path(arguments.out) if arguments.out else None,
        metrics=_trusted_path(arguments.metrics) if arguments.metrics else None,
        workspace=_trusted_path(arguments.workspace),
        timeout_seconds=arguments.timeout_seconds,
    )


if __name__ == "__main__":
    import sys

    sys.exit(main())
