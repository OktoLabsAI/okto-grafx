"""The CI ceiling gate: it reads the published METRIC, never a log (SPEC-M1 AC-14, OR-4).

``calibrate.py`` measures and publishes; this reads what was published and decides. The split is
the requirement, not a preference: OR-4 says the gate reads
``oktografx_baseline_ceiling_multiple``, so the number the build is judged on is the same number a
dashboard would show, and a harness that printed a friendly log while publishing something else
would be caught here.

Four things it decides:

* each of the three D5 multiples is within its ceiling;
* a durable-commit multiple above 10x is not merely a failure but the ``consult_jp`` state FR-15
  requires -- the pipeline stops, and the trade that would fix it changes D1, which is the user's
  decision;
* a ceiling with no sample in the document is UNMEASURED, never a pass (A75.2);
* when the document carries ``oktografx_vector_recall_ratio`` (SPEC-VEC FR-8/D8d), it must be at
  or above the frozen recall target. The vector calibration itself belongs to the vector engine's
  wave; until it publishes, the ratio is absent and the gate says so rather than inventing one.

Exit codes: ``0`` every required ceiling met, ``1`` a ceiling missed or the pipeline stopped,
``2`` UNMEASURED.
"""

from __future__ import annotations

import argparse
import math
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from bench.harness.calibrate import (
    CEILINGS,
    METRIC_NAME,
    STATUS_CONSULT_JP,
    STATUS_EXCEEDED,
    STATUS_MET,
    STATUS_UNMEASURED,
)

__all__ = ["GateResult", "RECALL_METRIC", "check", "main", "read_multiples"]

RECALL_METRIC: str = "oktografx_vector_recall_ratio"
"""The SPEC-VEC recall gauge, checked as a floor rather than as a ceiling."""

DEFAULT_RECALL_TARGET: float = 0.90
"""The recall target this build calibrates against until the vector wave freezes its own."""


@dataclass(frozen=True, slots=True)
class GateResult:
    """What the gate concluded about one published metrics document."""

    status: str
    """``ceilings_met``, ``ceiling_exceeded``, ``consult_jp`` or ``unmeasured``."""

    lines: tuple[str, ...]
    """One line per ceiling, in reading order."""

    @property
    def exit_code(self) -> int:
        """Return the process exit code this result deserves."""
        if self.status == STATUS_MET:
            return 0
        if self.status == STATUS_UNMEASURED:
            return 2
        return 1


def read_multiples(document: str) -> tuple[dict[str, float], dict[str, float], str]:
    """Return the published multiples by ceiling, the plain gauges, and any read error.

    Never raises. A document that cannot be read yields empty mappings and a reason, which the
    caller turns into UNMEASURED -- a metrics file that failed to parse and a build with no
    regressions are opposite facts and must not share an encoding.
    """
    try:
        payload = json.loads(document)
    except ValueError as error:
        return {}, {}, f"the metrics document is not readable JSON: {error}"
    if not isinstance(payload, Mapping):
        # `[]`, `null` and `3` are all VALID JSON, so the parse above accepts them and only the
        # shape refuses them. Without this line `payload.get` raises AttributeError, nothing
        # catches it, and the process dies with a traceback -- which the shell reads as exit 1,
        # the code that means CEILING EXCEEDED. A document that could not be read and a build
        # that regressed are opposite facts; A75.2 forbids them sharing an encoding, and this
        # module is the one that says so.
        return (
            {},
            {},
            "the metrics document is valid JSON but not an object "
            f"({type(payload).__name__}), so it carries no metric list: UNMEASURED, not a verdict",
        )
    metrics = payload.get("metrics")
    if not isinstance(metrics, list):
        return {}, {}, "the metrics document holds no metric list"
    multiples: dict[str, float] = {}
    gauges: dict[str, float] = {}
    for entry in metrics:
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("name")
        for sample in entry.get("samples", []):
            if not isinstance(sample, Mapping) or "value" not in sample:
                continue
            value = sample["value"]
            if not isinstance(value, (int, float)):
                continue
            labels = sample.get("labels", {})
            if name == METRIC_NAME and isinstance(labels, Mapping):
                ceiling = labels.get("ceiling")
                if isinstance(ceiling, str):
                    multiples[ceiling] = float(value)
            elif isinstance(name, str):
                gauges[name] = float(value)
    return multiples, gauges, ""


def _recall_measurement(document: str) -> tuple[str, object]:
    """The ONE recall measurement, or the exact reason there is none.

    ``read_multiples`` collapses same-name entries last-wins and coerces through ``float``,
    which suits the legacy gauges and is WRONG for a required floor: the gate reads any
    metrics file, not only wiring output, so the gauge can arrive twice, or as ``true``,
    ``NaN``, ``Infinity``, or an out-of-range ratio -- and none of those is a measurement.
    The result is a three-state verdict, because ABSENT and MALFORMED are different facts:
    ``("absent", None)`` when no entry carries the name (tolerable when the gauge is not
    required); ``("malformed", reason)`` whenever anything about the publication deviates
    from the DECLARED shape -- more than one entry, an entry whose keys are not exactly
    ``{name, kind, unit, samples}``, kind != "gauge", unit != "ratio", a samples list whose
    length is not exactly 1, or a sample that is not a Mapping of exactly ``{"value"}`` --
    and ``("value", raw)`` only for the one well-formed publication. Extra fields are
    REFUSED, not ignored: the wiring publishes exactly this shape, so anything beyond it
    did not come from the pipeline and cannot be trusted as THE measurement.
    """
    try:
        payload = json.loads(document)
    except ValueError:
        return ("absent", None)
    if not isinstance(payload, Mapping):
        return ("absent", None)
    metrics = payload.get("metrics")
    if not isinstance(metrics, list):
        return ("absent", None)
    entries = [
        entry
        for entry in metrics
        if isinstance(entry, Mapping) and entry.get("name") == RECALL_METRIC
    ]
    if not entries:
        return ("absent", None)
    if len(entries) != 1:
        return (
            "malformed",
            f"published {len(entries)} times; exactly one entry is required",
        )
    entry = entries[0]
    if set(entry) != {"name", "kind", "unit", "samples"}:
        return (
            "malformed",
            f"entry keys {sorted(map(str, entry))} differ from the declared "
            "{'kind', 'name', 'samples', 'unit'}",
        )
    if entry["kind"] != "gauge":
        return ("malformed", f"kind {entry['kind']!r} is not 'gauge'")
    if entry["unit"] != "ratio":
        return ("malformed", f"unit {entry['unit']!r} is not 'ratio'")
    samples = entry["samples"]
    if not isinstance(samples, list) or len(samples) != 1:
        described = (
            len(samples) if isinstance(samples, list) else type(samples).__name__
        )
        return (
            "malformed",
            f"samples must be a list of exactly one sample; got {described}",
        )
    sample = samples[0]
    if not isinstance(sample, Mapping) or set(sample) != {"value"}:
        return ("malformed", "the sample must be a mapping of exactly {'value'}")
    return ("value", sample["value"])


def check(
    document: str,
    *,
    required: Sequence[str] = tuple(CEILINGS),
    recall_target: float = DEFAULT_RECALL_TARGET,
    require_recall: bool = False,
) -> GateResult:
    """Apply the D5 ceilings, and the recall floor when it is published.

    ``recall_target`` is validated HERE as well as at the CLI boundary: a direct caller
    passing bool/NaN/Inf or a value outside (0, 1] gets UNMEASURED, never an exception and
    never a verdict computed against nonsense.
    """
    if (
        isinstance(recall_target, bool)
        or type(recall_target) not in (float, int)
        or not math.isfinite(float(recall_target))
        or not 0.0 < float(recall_target) <= 1.0
    ):
        return GateResult(
            status=STATUS_UNMEASURED,
            lines=(
                f"vector_recall: UNMEASURED -- recall_target {recall_target!r} is not a "
                "finite number in (0, 1]; the gate cannot be taken",
            ),
        )
    recall_target = float(recall_target)
    multiples, _, error = read_multiples(document)
    if error:
        return GateResult(status=STATUS_UNMEASURED, lines=(error,))

    lines: list[str] = []
    status = STATUS_MET
    for ceiling in required:
        limit = CEILINGS.get(ceiling)
        if limit is None:
            lines.append(f"{ceiling}: no D5 limit is defined for this label")
            status = STATUS_UNMEASURED
            continue
        measured = multiples.get(ceiling)
        if measured is None:
            lines.append(
                f"{ceiling}: UNMEASURED -- {METRIC_NAME}{{ceiling={ceiling}}} was not published"
            )
            status = STATUS_UNMEASURED
            continue
        if measured <= limit:
            lines.append(f"{ceiling}: {measured:.2f}x of {limit:g}x -- met")
            continue
        lines.append(f"{ceiling}: {measured:.2f}x of {limit:g}x -- EXCEEDED")
        if ceiling == "durable_commit":
            lines.append(
                "durable commit is above 10x, so FR-15 puts the pipeline in the 'consult JP' "
                "state: nothing proceeds, and swapping to a fair lease would change D1."
            )
            if status != STATUS_UNMEASURED:
                status = STATUS_CONSULT_JP
        elif status == STATUS_MET:
            status = STATUS_EXCEEDED

    state, value = _recall_measurement(document)
    if state == "absent":
        message = f"{RECALL_METRIC} was not published"
        if require_recall:
            lines.append(f"vector_recall: UNMEASURED -- {message}")
            status = STATUS_UNMEASURED
        else:
            lines.append(
                f"vector_recall: not calibrated yet ({message}); not required by default"
            )
    elif state == "malformed":
        # Present-but-malformed is refused even without --require-recall: an absent OPTIONAL
        # gauge is tolerable, a corrupt publication never is.
        lines.append(f"vector_recall: UNMEASURED -- {RECALL_METRIC} {value}")
        status = STATUS_UNMEASURED
    else:
        usable = (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(float(value))
            and 0.0 <= float(value) <= 1.0
        )
        if not usable:
            lines.append(
                f"vector_recall: UNMEASURED -- published value {value!r} is not a recall "
                "measurement (a ratio must be a non-bool finite number in [0, 1])"
            )
            status = STATUS_UNMEASURED
        elif float(value) >= recall_target:
            lines.append(
                f"vector_recall: {float(value):.4f} at or above the "
                f"{recall_target:g} target -- met"
            )
        else:
            lines.append(
                f"vector_recall: {float(value):.4f} BELOW the {recall_target:g} "
                "target -- EXCEEDED"
            )
            if status == STATUS_MET:
                status = STATUS_EXCEEDED
    return GateResult(status=status, lines=tuple(lines))


def _resolve_recall_target(
    explicit: float | None, calibration: str | None
) -> tuple[float, str]:
    """Return the recall floor and WHERE it came from: flag > frozen artifact > built-in.

    The flag default is None deliberately (C13): a default equal to the built-in value would
    mask the frozen artifact, because an omitted flag and an explicit 0.9 would be
    indistinguishable. An EXPLICIT flag value is validated here -- a target that is not a
    finite number in (0, 1] cannot gate anything. An EXPLICITLY NAMED calibration file that
    is unreadable or holds no usable frozen target REFUSES (the ``main`` catch-all turns the
    raise into UNMEASURED, exit 2) instead of falling through: the caller asked for that
    artifact to govern, and a silent built-in floor is one nobody chose. The built-in
    default applies only when neither source was given at all.
    """
    if explicit is not None:
        if (
            isinstance(explicit, bool)
            or type(explicit) not in (float, int)
            or not math.isfinite(float(explicit))
            or not 0.0 < float(explicit) <= 1.0
        ):
            raise ValueError(
                f"--recall-target must be a finite number in (0, 1]; got {explicit!r}"
            )
        return float(explicit), "explicit flag"
    if calibration:
        try:
            payload = json.loads(Path(calibration).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ValueError(
                f"--calibration {calibration!r} was named explicitly but is unreadable "
                f"({error}); refusing to fall back to a floor nobody chose"
            ) from error
        candidate = (
            payload.get("vector_recall", {}).get("frozen", {}).get("target")
            if isinstance(payload, dict)
            else None
        )
        if (
            isinstance(candidate, (int, float))
            and not isinstance(candidate, bool)
            and math.isfinite(float(candidate))
            and 0.0 < float(candidate) <= 1.0
        ):
            return float(candidate), f"frozen in {calibration}"
        raise ValueError(
            f"--calibration {calibration!r} holds no usable frozen vector_recall target; "
            "refusing to fall back to a floor nobody chose"
        )
    return DEFAULT_RECALL_TARGET, "built-in default"


def main(argv: Sequence[str] | None = None) -> int:
    """Read a published metrics document and apply the ceilings; no exception escapes."""
    parser = argparse.ArgumentParser(
        prog="python -m bench.harness.gate",
        description="Fail the build when a D5 ceiling is exceeded, reading the published metric.",
    )
    parser.add_argument(
        "--metrics", required=True, help="the published metrics JSON document"
    )
    parser.add_argument(
        "--require",
        action="append",
        default=[],
        help=f"ceiling that must be present; defaults to {', '.join(CEILINGS)}",
    )
    parser.add_argument(
        "--recall-target",
        type=float,
        default=None,
        help=(
            "the recall floor; omitted, it comes from --calibration's frozen target, "
            "else the built-in default"
        ),
    )
    parser.add_argument(
        "--calibration",
        default=None,
        help="calibration.json whose vector_recall.frozen.target is the frozen recall floor",
    )
    parser.add_argument(
        "--require-recall",
        action="store_true",
        help="fail when the vector recall ratio has not been published",
    )
    arguments = parser.parse_args(argv)
    try:
        document = Path(arguments.metrics).read_text(encoding="utf-8")
    except (OSError, ValueError) as error:
        # ValueError covers UnicodeDecodeError: a metrics file that is not valid UTF-8 is
        # UNREADABLE, and before this clause it escaped as a traceback whose exit status 1
        # is this gate's code for CEILING EXCEEDED -- a false verdict from a broken file.
        print(
            f"D5 ceiling gate: UNMEASURED -- the metrics file could not be read: {error}"
        )
        return 2
    try:
        recall_target, target_origin = _resolve_recall_target(
            arguments.recall_target, arguments.calibration
        )
        print(f"vector recall target {recall_target:g} ({target_origin})")
        result = check(
            document,
            required=tuple(arguments.require) or tuple(CEILINGS),
            recall_target=recall_target,
            require_recall=arguments.require_recall,
        )
    except Exception as error:  # noqa: BLE001 - a crash here must read as UNMEASURED, never as 1
        # The docstring above promises that no exception escapes, and a promise the code does not
        # keep is worse than none: every unhandled exception leaves the interpreter with status 1,
        # which is this gate's code for CEILING EXCEEDED. Whatever shape of input got here, the
        # honest answer is that the gate could not be taken.
        print(
            "D5 ceiling gate: UNMEASURED (exit 2) -- the gate itself failed: "
            f"{type(error).__name__}: {error}"
        )
        return 2
    print(f"D5 ceiling gate: {result.status.upper()} (exit {result.exit_code})")
    for line in result.lines:
        print(f"  {line}")
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
