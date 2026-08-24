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
    _canonical_verdict,
    _describe,
    _validate_verdict,
    build_section,
    run_recall,
)


def _replace_json(path: Path, mutate: Callable[[dict[str, object]], None]) -> None:
    """Read a JSON document, apply one mutation, and atomically replace the file.

    The scratch file is UNIQUE per call (round-3 B): the old deterministic ``.c13.tmp``
    name let two concurrent stages clobber each other's half-written scratch before the
    replace. A failure unlinks the orphan scratch best-effort and re-raises.
    """
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document)
    descriptor, scratch_name = tempfile.mkstemp(
        prefix=path.name + ".c13-", suffix=".tmp", dir=str(path.parent)
    )
    payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    closed = False
    try:
        written = 0
        while written < len(payload):
            progress = os.write(descriptor, payload[written:])
            if not isinstance(progress, int) or progress <= 0:
                raise RuntimeError(
                    "the scratch write made no progress; refusing to replace a "
                    "valid document with a torn one"
                )
            written += progress
        closed = True
        os.close(descriptor)
        os.replace(scratch_name, path)
    except BaseException:
        # Ownership is explicit: the raw descriptor is ours until the single close
        # attempt above. Cleanup is best-effort and can never replace the PRIMARY
        # exception; ordinary shapes then die at the stage boundary as exit 3.
        if not closed:
            try:
                os.close(descriptor)
            except Exception:  # noqa: BLE001
                pass
        try:
            os.unlink(scratch_name)
        except Exception:  # noqa: BLE001
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
    document = json.loads(metrics.read_text(encoding="utf-8"))
    entries = document.get("metrics") if isinstance(document, dict) else None
    if not isinstance(entries, list) or not any(
        isinstance(entry, dict) and entry.get("name") == RECALL_METRIC
        for entry in entries
    ):
        return

    def mutate(document: dict[str, object]) -> None:
        stale = document.get("metrics")
        if isinstance(stale, list):
            document["metrics"] = [
                entry
                for entry in stale
                if not (isinstance(entry, dict) and entry.get("name") == RECALL_METRIC)
            ]

    _replace_json(metrics, mutate)


def _append_section(out: Path, section: dict[str, object]) -> None:
    """Write step (1): the calibration document gains its ``vector_recall`` section."""

    def mutate(document: dict[str, object]) -> None:
        document["vector_recall"] = section

    _replace_json(out, mutate)


def _append_gauge(metrics: Path, value: float) -> None:
    """Write step (2), LAST: the metrics document gains the recall gauge the gate reads."""

    def mutate(document: dict[str, object]) -> None:
        entries = document.setdefault("metrics", [])
        if not isinstance(entries, list):
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

    _replace_json(metrics, mutate)


def _release_publication_locks(held: list[Path]) -> list[str]:
    """Unlink held locks in REVERSE order; return diagnostics for any residue."""
    residue: list[str] = []
    for lock_path in reversed(held):
        try:
            os.unlink(str(lock_path))
        except Exception as failure:  # noqa: BLE001 -- round-4: cleanup CONTINUES
            # A hostile unlink (RuntimeError, not only OSError) must not abort the
            # reverse cleanup of the REMAINING locks; it becomes residue diagnosis.
            residue.append(f"{lock_path} ({failure})")
    return residue


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
    """
    ordered = sorted({str(document) for document in documents})
    held: list[Path] = []
    for target in ordered:
        lock_path = Path(target + ".c13.lock")
        descriptor: int | None = None
        try:
            descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            held.append(lock_path)
            os.write(descriptor, str(os.getpid()).encode("ascii"))
        except FileExistsError:
            residue = _release_publication_locks(held)
            reason = (
                f"{lock_path} exists, so another stage holds (or died holding) "
                "the publication lock; refusing to interleave. Remove the file "
                "only after confirming no stage runs."
            )
            if residue:
                reason += f" (release residue: {residue})"
            return [], reason
        except Exception as failure:  # noqa: BLE001 -- round-4: any ordinary shape
            # RuntimeError from a hostile os.open/os.write is as ordinary as OSError:
            # close best-effort, unwind, refuse typed. If the best-effort close ALSO
            # fails, this lock's descriptor state is uncertain and its file is left
            # in place (never unlink over an uncertain descriptor); only the certain
            # locks are released, and the leftover is named in the reason.
            close_ok = True
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except Exception:  # noqa: BLE001 -- best-effort, never replaces
                    close_ok = False
            uncertain = None
            if not close_ok and held and held[-1] == lock_path:
                uncertain = held.pop()
            residue = _release_publication_locks(held)
            reason = (
                f"the publication lock {lock_path} could not be taken or "
                f"written ({failure})"
            )
            if uncertain is not None:
                reason += (
                    f"; {uncertain} is left in place because the descriptor state "
                    "is uncertain"
                )
            if residue:
                reason += f" (release residue: {residue})"
            return [], reason
        except BaseException:
            # KeyboardInterrupt/SystemExit: unwind the same way, then propagate the
            # PRIMARY -- every cleanup below is best-effort and cannot replace it;
            # a lock whose close failed stays in place for the same uncertainty rule.
            close_ok = True
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except Exception:  # noqa: BLE001
                    close_ok = False
            if not close_ok and held and held[-1] == lock_path:
                held.pop()
            _release_publication_locks(held)
            raise
        try:
            os.close(descriptor)
        except Exception as failure:  # noqa: BLE001 -- round-4: close is ordinary too
            # ONE close attempt, never a retry: a close that closed and THEN raised
            # would make a second close reach a possibly-reused descriptor number.
            # And because the descriptor state is now UNCERTAIN, this lock file is
            # deliberately LEFT IN PLACE -- unlinking it could let another process
            # acquire while our descriptor possibly lives. The certain locks are
            # released, the refusal is typed, and the leftover is named as residue.
            # The OS closes the descriptor when the process ends.
            uncertain = held.pop()
            residue = _release_publication_locks(held)
            reason = (
                f"the lock descriptor for {lock_path} could not be closed "
                f"({failure}); {uncertain} is left in place because the descriptor "
                "state is uncertain"
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
    workspace: Path,
    timeout_seconds: float | None,
) -> int:
    """The measurement and publication flow, entered ONLY with the lock held."""
    try:
        if metrics is not None:
            _strip_stale_gauge(metrics)
        verdict = run_recall(
            profile,
            gt_mode=gt_mode,
            scratch=workspace / "recall",
            timeout_seconds=timeout_seconds,
        )
    except RecallStageError as failure:
        print(f"vector recall: FAIL-CLOSED -- {failure}")
        return 3
    except Exception as failure:  # noqa: BLE001 -- any ordinary failure is exit 3 typed
        # An ORDINARY exception from the strip or the worker is still a stage failure:
        # typed line, exit 3, no traceback -- and because the strip precedes the spawn,
        # a strip failure aborts before any measurement begins. Exception, not
        # BaseException: KeyboardInterrupt and SystemExit still propagate.
        print(f"vector recall: stage failed before publication -- {_describe(failure)}")
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
        print(
            "vector recall: FAIL-CLOSED -- verdict validation itself failed: "
            f"{_describe(failure)}"
        )
        return 3
    if reason is not None:
        print(f"vector recall: FAIL-CLOSED -- incoherent verdict: {reason}")
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
        print(
            "vector recall: publication failed after measurement -- "
            f"{_describe(failure)}"
        )
        return 3
    home = next(iter(section["observed"]))  # type: ignore[call-overload]
    print(
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
        print(
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
    resolved: dict[str, Path] = {}
    for alias_label, alias_path in (("--out", out), ("--metrics", metrics)):
        if alias_path is None:
            continue
        try:
            canonical = alias_path.resolve(strict=True)
            link_count = os.stat(canonical).st_nlink
        except Exception as failure:  # noqa: BLE001 -- absolute boundary; KI/SE pass
            print(
                f"vector recall stage: REFUSED -- {alias_label} {alias_path} could "
                f"not be resolved to a physical document ({failure}); nothing was run."
            )
            return 3
        if link_count > 1:
            print(
                f"vector recall stage: REFUSED -- {alias_label} {alias_path} is "
                f"hardlinked (st_nlink={link_count}); os.replace would break the "
                "aliases, so no coherent atomic publication exists. Nothing was run."
            )
            return 3
        resolved[alias_label] = canonical
    if (
        "--out" in resolved
        and "--metrics" in resolved
        and resolved["--out"] == resolved["--metrics"]
    ):
        print(
            "vector recall stage: REFUSED -- --out and --metrics resolve to the SAME "
            "physical document; the section and the gauge need distinct files."
        )
        return 3
    out = resolved.get("--out", out)
    metrics = resolved.get("--metrics", metrics)
    for label, path in (("--out", out), ("--metrics", metrics)):
        if path is None:
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except Exception as failure:  # noqa: BLE001 -- absolute boundary; KI/SE propagate
            print(
                f"vector recall stage: REFUSED -- {label} {path} is not a readable JSON "
                f"document ({failure}); nothing was run and nothing was written."
            )
            return 3
        if not isinstance(document, dict):
            print(
                f"vector recall stage: REFUSED -- {label} {path} holds "
                f"{type(document).__name__}, not an object; nothing was run."
            )
            return 3
        if (
            label == "--metrics"
            and "metrics" in document
            and not isinstance(document["metrics"], list)
        ):
            print(
                f"vector recall stage: REFUSED -- {label} {path} carries a 'metrics' key "
                "that is not a list; a gauge could never land there. Nothing was run."
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
    held_locks, lock_refusal = _acquire_publication_locks(
        [path for path in (out, metrics) if path is not None]
    )
    if lock_refusal is not None:
        print(f"vector recall stage: REFUSED -- {lock_refusal}")
        return 3
    residue: list[str] = []
    try:
        outcome = _publish_documents(
            profile=profile,
            gt_mode=gt_mode,
            out=out,
            metrics=metrics,
            workspace=workspace,
            timeout_seconds=timeout_seconds,
        )
    finally:
        residue = _release_publication_locks(held_locks)
        for residue_line in residue:
            print(
                "vector recall stage: WARNING -- lock residue left behind: "
                f"{residue_line}"
            )
    if outcome == 0 and residue:
        # Round-4: exit 0 with persisting locks would tell the next stage the field
        # is clear while the files say otherwise. Success is demoted to the typed
        # failure; the diagnostics above name every leftover.
        print(
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
        out=Path(arguments.out) if arguments.out else None,
        metrics=Path(arguments.metrics) if arguments.metrics else None,
        workspace=Path(arguments.workspace),
        timeout_seconds=arguments.timeout_seconds,
    )


if __name__ == "__main__":
    import sys

    sys.exit(main())
