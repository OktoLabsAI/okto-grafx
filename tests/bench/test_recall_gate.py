"""The gate's recall floor has one precedence and one voice: flag > artifact > built-in (C13).

What these tests pin:

* an omitted ``--recall-target`` with a ``--calibration`` file takes the FROZEN target — the
  old masking default (flag default == built-in value) is gone, so the artifact actually
  governs;
* an explicit flag beats the artifact; nothing at all means the built-in default; every
  resolution prints its ORIGIN;
* an EXPLICITLY NAMED calibration file that is unreadable or holds no usable target
  REFUSES (UNMEASURED, exit 2) — the built-in default applies only when no source was given;
* the ROUND7 §3b anti-disconnection regression: a published gauge below the frozen target
  makes the gate exit non-zero — the knob is verifiably connected;
* the legacy exit codes survive: met is 0, EXCEEDED is 1, UNMEASURED (a required gauge
  absent included) is 2, an unreadable metrics document is 2.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import bench.harness.gate as gate_module
from bench.harness.gate import (
    DEFAULT_RECALL_TARGET,
    STATUS_UNMEASURED,
    _resolve_recall_target,
    check,
    main,
    read_multiples,
)

RECALL_METRIC = "oktografx_vector_recall_ratio"


def _metrics_document(path: Path, *, recall: float | None) -> Path:
    """Write a minimal published metrics document, optionally carrying the recall gauge."""
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
    document = path / "metrics.json"
    document.write_text(json.dumps({"metrics": entries}), encoding="utf-8")
    return document


def _calibration_document(path: Path, *, target: float) -> Path:
    """Write a calibration document whose vector_recall section freezes one target."""
    document = path / "calibration.json"
    document.write_text(
        json.dumps({"vector_recall": {"frozen": {"target": target}}}), encoding="utf-8"
    )
    return document


def test_resolution_precedence_is_flag_then_artifact_then_builtin(
    tmp_path: Path,
) -> None:
    """The three sources resolve in order, each naming its origin."""
    calibration = _calibration_document(tmp_path, target=0.95)
    explicit = _resolve_recall_target(0.88, str(calibration))
    assert explicit == (0.88, "explicit flag")
    frozen = _resolve_recall_target(None, str(calibration))
    assert frozen[0] == 0.95
    assert "frozen in" in frozen[1]
    built_in = _resolve_recall_target(None, None)
    assert built_in == (DEFAULT_RECALL_TARGET, "built-in default")


def test_an_explicitly_named_but_unusable_artifact_refuses(
    tmp_path: Path,
) -> None:
    """(b): the caller asked for THAT artifact to govern; a silent 0.90 is a floor nobody
    chose. Unreadable and target-less files raise; only calibration=None means built-in."""
    with pytest.raises(ValueError, match="unreadable"):
        _resolve_recall_target(None, str(tmp_path / "absent.json"))
    hollow = tmp_path / "hollow.json"
    hollow.write_text(json.dumps({"ceilings": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="no usable frozen"):
        _resolve_recall_target(None, str(hollow))
    assert _resolve_recall_target(None, None) == (
        DEFAULT_RECALL_TARGET,
        "built-in default",
    )


def test_the_frozen_target_governs_the_verdict_anti_disconnection(
    tmp_path: Path,
) -> None:
    """ROUND7 §3b: a gauge below the frozen target exits non-zero; at or above passes."""
    calibration = _calibration_document(tmp_path, target=0.95)
    below = _metrics_document(tmp_path, recall=0.92)
    assert (
        main(
            [
                "--metrics",
                str(below),
                "--calibration",
                str(calibration),
                "--require-recall",
            ]
        )
        == 1
    )
    above = _metrics_document(tmp_path, recall=0.96)
    assert (
        main(
            [
                "--metrics",
                str(above),
                "--calibration",
                str(calibration),
                "--require-recall",
            ]
        )
        == 0
    )


def test_an_explicit_flag_beats_the_artifact_for_the_forced_violation(
    tmp_path: Path,
) -> None:
    """The VTS-10 forced violation uses the flag override, artifact present or not."""
    calibration = _calibration_document(tmp_path, target=0.90)
    document = _metrics_document(tmp_path, recall=0.95)
    assert (
        main(
            [
                "--metrics",
                str(document),
                "--calibration",
                str(calibration),
                "--recall-target",
                "0.999",
                "--require-recall",
            ]
        )
        == 1
    )


def test_legacy_exit_codes_survive(tmp_path: Path) -> None:
    """Met 0; UNMEASURED-with-require 2; unreadable metrics document 2; no-require passes."""
    document = _metrics_document(tmp_path, recall=None)
    assert main(["--metrics", str(document)]) == 0
    assert main(["--metrics", str(document), "--require-recall"]) == 2
    assert main(["--metrics", str(tmp_path / "missing.json")]) == 2


def _document_with_recall_entries(path: Path, entries: list[dict[str, object]]) -> Path:
    """A metrics document with all three ceilings met plus the given raw recall entries."""
    payload: list[dict[str, object]] = [
        {
            "name": "oktografx_baseline_ceiling_multiple",
            "samples": [
                {"value": 1.0, "labels": {"ceiling": "durable_commit"}},
                {"value": 1.0, "labels": {"ceiling": "point_read"}},
                {"value": 1.0, "labels": {"ceiling": "open_replay"}},
            ],
        }
    ]
    payload.extend(entries)
    document = path / "metrics.json"
    document.write_text(json.dumps({"metrics": payload}), encoding="utf-8")
    return document


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "0", "1.5", "-0.5"])
def test_an_invalid_explicit_target_is_refused_as_unmeasured(
    tmp_path: Path, bad: str
) -> None:
    """A --recall-target outside (0,1] or non-finite cannot gate anything: exit 2."""
    document = _metrics_document(tmp_path, recall=0.95)
    # The = form is deliberate: argparse reads a bare "-inf" as an option, which is a
    # usage error (also exit 2) rather than the validation path this test pins.
    assert main(["--metrics", str(document), f"--recall-target={bad}"]) == 2


@pytest.mark.parametrize(
    "value", [True, False, float("nan"), float("inf"), -0.5, 1.5, "0.9", None]
)
def test_a_present_but_invalid_gauge_value_is_unmeasured_even_unrequired(
    tmp_path: Path, value: object
) -> None:
    """(c): bool, NaN, Inf, out-of-range and non-numeric are NOT measurements. A corrupt
    publication refuses even without --require-recall -- only ABSENCE is tolerable there."""
    document = _document_with_recall_entries(
        tmp_path,
        [
            {
                "name": RECALL_METRIC,
                "kind": "gauge",
                "unit": "ratio",
                "samples": [{"value": value}],
            }
        ],
    )
    assert main(["--metrics", str(document)]) == 2


def test_duplicate_gauge_entries_or_samples_are_unmeasured(tmp_path: Path) -> None:
    """(c): exactly ONE measurement; two entries or two samples mean none is THE one."""
    twice = _document_with_recall_entries(
        tmp_path,
        [
            {"name": RECALL_METRIC, "samples": [{"value": 0.99}]},
            {"name": RECALL_METRIC, "samples": [{"value": 0.99}]},
        ],
    )
    assert main(["--metrics", str(twice)]) == 2
    multi = _document_with_recall_entries(
        tmp_path,
        [{"name": RECALL_METRIC, "samples": [{"value": 0.99}, {"value": 0.98}]}],
    )
    assert main(["--metrics", str(multi)]) == 2
    hollow = _document_with_recall_entries(
        tmp_path, [{"name": RECALL_METRIC, "samples": []}]
    )
    assert main(["--metrics", str(hollow)]) == 2


def test_zero_recall_is_a_measurement_and_fails_as_exceeded(tmp_path: Path) -> None:
    """0.0 is a legitimate (terrible) ratio: MEASURED, below every target -- exit 1."""
    document = _metrics_document(tmp_path, recall=0.0)
    assert main(["--metrics", str(document)]) == 1


def test_an_unusable_named_calibration_fails_the_cli_as_unmeasured(
    tmp_path: Path,
) -> None:
    """(b) end to end: a named-but-unusable --calibration exits 2; omitting it gates on
    the built-in default."""
    document = _metrics_document(tmp_path, recall=0.95)
    hollow = tmp_path / "hollow.json"
    hollow.write_text(json.dumps({"ceilings": []}), encoding="utf-8")
    assert main(["--metrics", str(document), "--calibration", str(hollow)]) == 2
    absent = tmp_path / "absent.json"
    assert main(["--metrics", str(document), "--calibration", str(absent)]) == 2
    assert main(["--metrics", str(document)]) == 0


def _shaped(samples: list[dict[str, object]], **overrides: object) -> dict[str, object]:
    """One recall entry in the declared shape, with deliberate deviations on demand."""
    entry: dict[str, object] = {
        "name": RECALL_METRIC,
        "kind": "gauge",
        "unit": "ratio",
        "samples": samples,
    }
    entry.update(overrides)
    return entry


def test_a_junk_extra_sample_is_unmeasured_even_beside_a_valid_one(
    tmp_path: Path,
) -> None:
    """The reaudit probe: [{"value":0.99},{}] passed as MET; now it is UNMEASURED."""
    document = _document_with_recall_entries(tmp_path, [_shaped([{"value": 0.99}, {}])])
    assert main(["--metrics", str(document)]) == 2


def test_wrong_kind_or_unit_or_extra_fields_are_unmeasured(tmp_path: Path) -> None:
    """The declared shape is exact: kind=gauge, unit=ratio, no extra keys anywhere."""
    wrong_kind = _document_with_recall_entries(
        tmp_path, [_shaped([{"value": 0.99}], kind="counter")]
    )
    assert main(["--metrics", str(wrong_kind)]) == 2
    wrong_unit = _document_with_recall_entries(
        tmp_path, [_shaped([{"value": 0.99}], unit="seconds")]
    )
    assert main(["--metrics", str(wrong_unit)]) == 2
    extra_entry_field = _document_with_recall_entries(
        tmp_path, [_shaped([{"value": 0.99}], labels=[])]
    )
    assert main(["--metrics", str(extra_entry_field)]) == 2
    extra_sample_field = _document_with_recall_entries(
        tmp_path, [_shaped([{"value": 0.99, "labels": {}}])]
    )
    assert main(["--metrics", str(extra_sample_field)]) == 2
    bare_entry = _document_with_recall_entries(
        tmp_path, [{"name": RECALL_METRIC, "samples": [{"value": 0.99}]}]
    )
    assert main(["--metrics", str(bare_entry)]) == 2


@pytest.mark.parametrize(
    "target",
    [float("nan"), float("inf"), 0.0, 1.5, -1.0, True, "0.9", 10**10000, -(10**10000)],
    # Explicit ids: pytest's own id generation calls str() on parameters, and
    # str(10**10000) exceeds CPython's digit limit -- the collection itself crashed.
    ids=[
        "nan",
        "inf",
        "zero",
        "above-one",
        "negative",
        "bool",
        "string",
        "huge-int",
        "huge-negative-int",
    ],
)
def test_check_called_directly_with_an_invalid_target_is_unmeasured(
    tmp_path: Path, target: object
) -> None:
    """(2): the library boundary defends itself -- UNMEASURED, never a raise or verdict."""
    from bench.harness.gate import STATUS_UNMEASURED, check

    document = _metrics_document(tmp_path, recall=0.95)
    result = check(
        document.read_text(encoding="utf-8"),
        recall_target=target,  # type: ignore[arg-type]
    )
    assert result.status == STATUS_UNMEASURED


def test_a_deeply_nested_document_is_unmeasured_not_a_recursion_error(
    tmp_path: Path,
) -> None:
    """MEDIUM-3: json.loads overflows the parser's recursion at ~5000 levels, and
    'never raises' includes that shape -- library check() and the CLI both."""
    from bench.harness.gate import STATUS_UNMEASURED, check

    deep = "[" * 5000 + "]" * 5000
    assert check(deep).status == STATUS_UNMEASURED
    document = tmp_path / "metrics.json"
    document.write_text(deep, encoding="utf-8")
    assert main(["--metrics", str(document)]) == 2


class _EvilRepr:
    """An object whose repr raises -- the hostile shape MEDIUM-4 names."""

    def __repr__(self) -> str:
        raise RuntimeError("hostile repr")


class _HostileMeta(type):
    """Round-3 C: even type(value).__name__ can execute a metaclass property."""

    @property
    def __name__(cls) -> str:  # noqa: N804
        raise RuntimeError("hostile metaclass")


class _HostileValue(metaclass=_HostileMeta):
    def __repr__(self) -> str:
        raise RuntimeError("hostile repr")


def test_a_hostile_metaclass_cannot_escape_the_refusal_paths(tmp_path: Path) -> None:
    """Round-3 C: the describer's fallback is a CONSTANT -- the refusal path touches
    the offender zero more times, so check() and _resolve stay typed."""
    from bench.harness.gate import STATUS_UNMEASURED, check

    document = _metrics_document(tmp_path, recall=0.95).read_text(encoding="utf-8")
    result = check(document, recall_target=_HostileValue())  # type: ignore[arg-type]
    assert result.status == STATUS_UNMEASURED
    with pytest.raises(ValueError, match="finite number"):
        _resolve_recall_target(_HostileValue(), None)  # type: ignore[arg-type]


def test_a_hostile_repr_cannot_escape_check_or_resolve(tmp_path: Path) -> None:
    """MEDIUM-4: the refusal path formats the offender through the guarded describer,
    so a repr raising RuntimeError still yields UNMEASURED / the typed ValueError."""
    from bench.harness.gate import STATUS_UNMEASURED, check

    document = _metrics_document(tmp_path, recall=0.95).read_text(encoding="utf-8")
    result = check(document, recall_target=_EvilRepr())  # type: ignore[arg-type]
    assert result.status == STATUS_UNMEASURED
    with pytest.raises(ValueError, match="finite number"):
        _resolve_recall_target(_EvilRepr(), None)  # type: ignore[arg-type]


def test_a_non_utf8_metrics_file_is_unmeasured_not_a_traceback(tmp_path: Path) -> None:
    """(6): invalid bytes used to escape as ValueError -> exit 1 (false EXCEEDED)."""
    document = tmp_path / "metrics.json"
    document.write_bytes(b"\xff\xfe\x00garbage")
    assert main(["--metrics", str(document)]) == 2


def test_a_huge_integer_gauge_value_is_refused_not_an_overflow(tmp_path: Path) -> None:
    """The reaudit's OverflowError probe, gauge side: float(10**400) raises BEFORE
    isfinite unless the conversion is guarded. 10**400 is used because it still parses
    as JSON (CPython refuses integer literals past its digit limit) yet overflows float.
    """
    document = _document_with_recall_entries(tmp_path, [_shaped([{"value": 10**400}])])
    assert main(["--metrics", str(document)]) == 2


def test_a_huge_integer_ceiling_sample_is_skipped_not_an_overflow(
    tmp_path: Path,
) -> None:
    """Third-order: read_multiples also coerced with bare float() -- a huge integer in a
    LEGACY ceiling sample crashed the gate. Skipped now, so the ceiling is UNMEASURED."""
    document = tmp_path / "metrics.json"
    document.write_text(
        json.dumps(
            {
                "metrics": [
                    {
                        "name": "oktografx_baseline_ceiling_multiple",
                        "samples": [
                            {"value": 10**400, "labels": {"ceiling": "durable_commit"}},
                            {"value": 1.0, "labels": {"ceiling": "point_read"}},
                            {"value": 1.0, "labels": {"ceiling": "open_replay"}},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert main(["--metrics", str(document)]) == 2


@pytest.mark.parametrize(
    "bad_samples", [None, 3, "list", {}], ids=["null", "int", "string", "dict"]
)
def test_non_list_samples_are_skipped_not_a_typeerror(
    tmp_path: Path, bad_samples: object
) -> None:
    """read_multiples iterated entry['samples'] blind: null/int raised TypeError through
    the never-raise promise. A non-list contributes nothing; the ceiling is UNMEASURED."""
    document = tmp_path / "metrics.json"
    document.write_text(
        json.dumps(
            {
                "metrics": [
                    {
                        "name": "oktografx_baseline_ceiling_multiple",
                        "samples": bad_samples,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert main(["--metrics", str(document)]) == 2


def test_a_huge_integer_explicit_target_raises_the_typed_error_not_overflow() -> None:
    """_resolve refuses programmatic 10**10000 with its DOCUMENTED ValueError."""
    with pytest.raises(ValueError, match="finite number"):
        _resolve_recall_target(10**10000, None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="finite number"):
        _resolve_recall_target(-(10**10000), None)  # type: ignore[arg-type]


# =====================================================================================
# Round-5 blocker 3: nothing after the parse escapes the never-raise boundaries
# =====================================================================================


class _HostileMapping(dict):
    """A Mapping that passes isinstance and then raises at ONE chosen access.

    The shape a monkeypatched ``json.loads`` can hand back, and the shape a dict
    SUBCLASS in a real document can have: isinstance(payload, Mapping) says yes, and
    then get/__iter__/keys/__getitem__ run the object's own code.
    """

    def __init__(self, mapping: dict[str, object], failing: str) -> None:
        super().__init__(mapping)
        self.failing = failing

    def get(self, key: object, default: object = None) -> object:
        if self.failing == "get":
            raise RuntimeError("hostile get")
        return dict.get(self, key, default)

    def __iter__(self):
        if self.failing == "iter":
            raise RuntimeError("hostile iter")
        return dict.__iter__(self)

    def keys(self):
        if self.failing == "keys":
            raise RuntimeError("hostile keys")
        return dict.keys(self)

    def __getitem__(self, key: object) -> object:
        if self.failing == "getitem":
            raise RuntimeError("hostile getitem")
        return dict.__getitem__(self, key)


def _hostile_document(where: str, failing: str) -> _HostileMapping:
    """A published document carrying the hostile mapping in one of the two positions.

    ``where="payload"`` makes the document itself hostile (the object a patched
    json.loads hands back); ``where="entry"`` makes the recall ENTRY hostile (the shape
    a dict subclass inside a real document can have).
    """
    body: dict[str, object] = {
        "name": RECALL_METRIC,
        "kind": "gauge",
        "unit": "ratio",
        "samples": [{"value": 0.95}],
    }
    if where == "entry":
        return _HostileMapping({"metrics": [_HostileMapping(body, failing)]}, "never")
    return _HostileMapping({"metrics": [dict(body)]}, failing)


@pytest.mark.parametrize(
    ("where", "failing"),
    [("payload", "get"), ("entry", "iter"), ("entry", "getitem")],
    ids=["payload-get", "entry-iter", "entry-getitem"],
)
def test_a_hostile_mapping_is_a_malformed_publication_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, where: str, failing: str
) -> None:
    """Round-5 blocker 3: the boundary began AFTER payload.get("metrics") and ended
    after the enumeration, so the .get and the whole shape validation ran the offender's
    own code unguarded. These are the three accesses this reader actually performs on a
    document -- payload.get("metrics"), set(entry)/sorted(map(str, entry)) through
    __iter__, and entry["kind"]/entry["samples"] through __getitem__ -- and each one
    used to be reachable outside every guard. A publication that raises while being
    inspected is MALFORMED, which is a verdict the gate can act on; a traceback is
    exit 1, which means CEILING EXCEEDED."""
    payload = _hostile_document(where, failing)
    monkeypatch.setattr(gate_module.json, "loads", lambda *a, **kw: payload)
    state, _ = gate_module._recall_measurement("{}")
    assert state == "malformed", "an inspection that raises is never a measurement"
    verdict = check("{}", require_recall=True)
    assert verdict.status == STATUS_UNMEASURED
    assert verdict.exit_code == 2, "UNMEASURED is exit 2, never the exceeded 1"


@pytest.mark.parametrize(
    ("where", "failing"),
    [("payload", "get"), ("entry", "get")],
    ids=["payload-get", "entry-get"],
)
def test_a_hostile_mapping_reads_as_unmeasured_in_the_ceiling_reader(
    monkeypatch: pytest.MonkeyPatch, where: str, failing: str
) -> None:
    """Round-5 blocker 3: read_multiples' guard also started one line too late -- after
    payload.get("metrics"). This reader calls .get on the payload AND on every entry,
    and both could raise straight out of a function whose docstring promises it never
    raises."""
    payload = _hostile_document(where, failing)
    monkeypatch.setattr(gate_module.json, "loads", lambda *a, **kw: payload)
    multiples, gauges, reason = read_multiples("{}")
    assert (multiples, gauges) == ({}, {})
    assert reason, "an unreadable document must say so, not raise"


@pytest.mark.parametrize("failing", ["get", "iter", "keys", "getitem"])
@pytest.mark.parametrize("where", ["payload", "entry"])
def test_no_hostile_access_shape_escapes_either_reader(
    monkeypatch: pytest.MonkeyPatch, where: str, failing: str
) -> None:
    """The auditor's four access shapes against both readers, in both positions.

    Not every combination is on a reader's path: nothing in this module calls keys(),
    and read_multiples never iterates an entry. Those cells are pinned as READ NORMALLY
    rather than dressed up as refusals -- a probe that claimed a refusal the code does
    not make would be fiction. What the matrix proves is the absolute property: whatever
    the shape and wherever it sits, NEITHER reader raises, and every verdict stays one
    of the three each is allowed to return. A cell that stops reading normally because
    some future edit put keys() or an entry iteration on a path is a change this probe
    forces someone to look at."""
    payload = _hostile_document(where, failing)
    monkeypatch.setattr(gate_module.json, "loads", lambda *a, **kw: payload)
    state, _ = gate_module._recall_measurement("{}")
    assert state in ("absent", "malformed", "value")
    multiples, gauges, _ = read_multiples("{}")
    assert isinstance(multiples, dict)
    assert isinstance(gauges, dict)
    assert check("{}", require_recall=True).exit_code in (0, 1, 2)


def test_a_hostile_repr_in_the_document_is_described_not_executed_raw(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Round-5 blocker 3: the malformed reasons formatted document values with a raw
    {!r}, so a hostile __repr__ crashed the refusal it was part of. The guarded
    describer keeps the specific reason and cannot crash producing it."""

    class _HostileRepr:
        def __repr__(self) -> str:
            raise RuntimeError("hostile repr")

    document = json.dumps({"metrics": []})
    payload = {
        "metrics": [
            {
                "name": RECALL_METRIC,
                "kind": _HostileRepr(),
                "unit": "ratio",
                "samples": [{"value": 0.95}],
            }
        ]
    }
    monkeypatch.setattr(gate_module.json, "loads", lambda *a, **kw: payload)
    state, reason = gate_module._recall_measurement(document)
    assert state == "malformed"
    # Round-8: the rejection now happens EARLIER, at the canonical rebuild, so the
    # hostile __repr__ is never reached rather than being safely described. The probe
    # still pins what matters -- present-but-uninspectable is MALFORMED, never absent
    # and never a traceback -- and the describer keeps its own dedicated probes.
    assert "not plain JSON-native data" in str(reason)
    assert check(document, require_recall=True).status == STATUS_UNMEASURED


def test_a_parse_failure_with_a_hostile_str_cannot_escape_either_reader(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Round-5 blocker 3: both readers interpolated the parse error RAW, so an EvilError
    whose __str__ raises turned the refusal itself into a traceback -- exit 1, the code
    that means CEILING EXCEEDED, out of a file that simply could not be read."""

    class EvilError(ValueError):
        def __str__(self) -> str:
            raise RuntimeError("hostile str")

        def __repr__(self) -> str:
            raise RuntimeError("hostile repr")

    def evil_loads(*args: object, **kwargs: object) -> object:
        raise EvilError()

    monkeypatch.setattr(gate_module.json, "loads", evil_loads)
    multiples, gauges, reason = read_multiples("{}")
    assert (multiples, gauges) == ({}, {})
    assert "not readable JSON" in reason
    assert gate_module._recall_measurement("{}") == ("absent", None)
    assert check("{}", require_recall=True).status == STATUS_UNMEASURED
    metrics = tmp_path / "metrics.json"
    metrics.write_text("{}", encoding="utf-8")
    assert main(["--metrics", str(metrics), "--require-recall"]) == 2
    assert "UNMEASURED" in capsys.readouterr().out


def test_an_unguarded_metaclass_cannot_escape_the_ceiling_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-6, the fourth site (same class as the auditor's three, found while mapping
    them): read_multiples formats type(payload).__name__ in the not-an-object branch,
    which sits BEFORE its guard opens. A metaclass whose __name__ is a property that
    raises escaped a function whose docstring promises it never raises -- and an escape
    from this reader means exit 1, which is this gate's code for CEILING EXCEEDED."""

    class _ExitingMeta(type):
        @property
        def __name__(cls) -> str:  # noqa: N805
            raise SystemExit(74)

    class _Hostile(metaclass=_ExitingMeta):
        pass

    monkeypatch.setattr(gate_module.json, "loads", lambda *a, **kw: _Hostile())
    multiples, gauges, reason = read_multiples("{}")
    assert (multiples, gauges) == ({}, {})
    # Round-8: the rebuild refuses this object before the not-an-object branch can even
    # format it, so the metaclass property is never touched at all.
    assert "not plain JSON-native data" in reason
    assert check("{}", require_recall=True).status == STATUS_UNMEASURED


def test_any_unreadable_metrics_file_is_unmeasured_not_exceeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Round-6 item 6: the read clause listed (OSError, ValueError), so any other
    ordinary shape escaped as a traceback -- and this gate's exit 1 means CEILING
    EXCEEDED, so a broken file produced a false regression verdict. Whatever the reason,
    a file that cannot be read is UNMEASURED."""
    metrics = tmp_path / "metrics.json"
    metrics.write_text("{}", encoding="utf-8")
    real_read_text = Path.read_text

    def hostile_read(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == "metrics.json":
            raise RuntimeError("this file cannot be read")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", hostile_read)
    assert main(["--metrics", str(metrics)]) == 2
    monkeypatch.undo()
    assert "UNMEASURED" in capsys.readouterr().out


def test_a_hostile_instance_check_cannot_escape_either_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-6 item 6: both readers evaluated isinstance(payload, Mapping) OUTSIDE the
    guard. An ABC check runs __instancecheck__/__subclasshook__, which a metaclass
    controls, so the very line deciding "this is not a document" could raise out of a
    function that promises it never raises -- exit 1, which this gate reads as CEILING
    EXCEEDED."""

    class _ExitingMeta(type):
        def __instancecheck__(cls, instance: object) -> bool:
            raise SystemExit(75)

        def __subclasshook__(cls, subclass: type) -> bool:
            raise SystemExit(75)

    class _Hostile(metaclass=_ExitingMeta):
        @property
        def __class__(self) -> type:  # noqa: D105
            raise SystemExit(75)

    monkeypatch.setattr(gate_module.json, "loads", lambda *a, **kw: _Hostile())
    multiples, gauges, reason = read_multiples("{}")
    assert (multiples, gauges) == ({}, {})
    assert reason, "an unreadable document must say so, not raise"
    assert gate_module._recall_measurement("{}")[0] in ("absent", "malformed")
    assert check("{}", require_recall=True).status == STATUS_UNMEASURED


class _ExitingGetMapping(dict):
    """A parsed object whose every .get raises -- the round-7 (B)/(E) shape."""

    def get(self, *args: object, **kwargs: object) -> object:
        raise SystemExit(92)


def test_a_payload_whose_get_exits_is_a_verdict_not_an_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-7 (2, codes 92/93): the post-parse boundaries caught Exception while
    everything inside them runs the OBJECT's code, so a payload fabricating SystemExit
    walked out of functions that promise a verdict. Inspection of an already-parsed
    object absorbs every shape; the parse itself does not."""
    monkeypatch.setattr(
        gate_module.json, "loads", lambda *a, **kw: _ExitingGetMapping({"metrics": []})
    )
    multiples, gauges, reason = read_multiples("{}")
    assert (multiples, gauges) == ({}, {})
    assert reason
    # Round-8: refused at the rebuild, before any .get is reached.
    assert gate_module._recall_measurement("{}") == (
        "malformed",
        "the document is not plain JSON-native data",
    )
    assert check("{}", require_recall=True).exit_code == 2


def test_a_calibration_payload_whose_get_exits_reads_as_unmeasured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Round-7 (5, code 101): three chained .get calls on the parsed calibration payload,
    all of them the object's code, escaped main's Exception clause and ended the process
    from inside a gate. A payload that cannot be inspected carries no frozen target,
    which is UNMEASURED -- never exit 1, which means CEILING EXCEEDED."""
    calibration = tmp_path / "calibration.json"
    calibration.write_text("{}", encoding="utf-8")
    metrics = tmp_path / "metrics.json"
    metrics.write_text(json.dumps({"metrics": []}), encoding="utf-8")

    class _ExitingChain(dict):
        def get(self, *args: object, **kwargs: object) -> object:
            raise SystemExit(101)

    real_loads = json.loads

    def hostile_loads(text: str, *args: object, **kwargs: object) -> object:
        parsed = real_loads(text, *args, **kwargs)
        return _ExitingChain(parsed) if isinstance(parsed, dict) else parsed

    monkeypatch.setattr(gate_module.json, "loads", hostile_loads)
    assert main(["--metrics", str(metrics), "--calibration", str(calibration)]) == 2
    monkeypatch.undo()
    assert "UNMEASURED" in capsys.readouterr().out


def test_a_str_subclass_key_never_reaches_the_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-8 (B, code 161): the reader exported keys taken straight from the document,
    so a str SUBCLASS became a ceiling or metric name and answered the CONSUMER's __eq__
    however it liked, long after this function returned. The rebuild refuses a document
    whose keys are not exact strings, so nothing of the sort can leave."""

    class _LyingKey(str):
        def __eq__(self, other: object) -> bool:
            raise SystemExit(161)

        def __hash__(self) -> int:
            return hash(str(self))

    payload = {
        "metrics": [
            {
                "name": "oktografx_baseline_ceiling_multiple",
                "samples": [
                    {"value": 1.0, "labels": {_LyingKey("ceiling"): "point_read"}}
                ],
            }
        ]
    }
    monkeypatch.setattr(gate_module.json, "loads", lambda *a, **kw: payload)
    multiples, gauges, reason = read_multiples("{}")
    monkeypatch.undo()
    assert (multiples, gauges) == ({}, {})
    assert "not plain JSON-native data" in reason
    for key in list(multiples) + list(gauges):
        assert type(key) is str


def _deep_document(depth: int) -> str:
    """A type-exact JSON tree deep enough to exhaust the rebuild's recursion."""
    return "[" * depth + "1" + "]" * depth


def test_a_deeply_nested_metrics_document_is_unmeasured_at_every_reader(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Round-9 (2): a type-exact tree deeper than the interpreter's limit raises
    RecursionError from inside the RECURSIVE rebuild. Each public boundary must answer
    with its own verdict -- UNMEASURED, exit 2 -- and never with the exit 1 that this
    gate reads as CEILING EXCEEDED."""
    document = '{"metrics": ' + _deep_document(400) + "}"
    metrics = tmp_path / "metrics.json"
    metrics.write_text(document, encoding="utf-8")
    calibration = tmp_path / "calibration.json"
    calibration.write_text(
        '{"vector_recall": ' + _deep_document(400) + "}", encoding="utf-8"
    )
    original_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(200)
    try:
        multiples, gauges, reason = read_multiples(document)
        assert (multiples, gauges) == ({}, {})
        assert reason
        state, _ = gate_module._recall_measurement(document)
        assert state in ("absent", "malformed")
        assert check(document, require_recall=True).exit_code == 2
        assert main(["--metrics", str(metrics), "--require-recall"]) == 2
        assert main(["--metrics", str(metrics), "--calibration", str(calibration)]) == 2
    finally:
        sys.setrecursionlimit(original_limit)
    assert "UNMEASURED" in capsys.readouterr().out


def test_a_rebuild_failure_is_malformed_and_never_a_tolerated_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-9 (3), the fail-open: the parse and the rebuild shared one except clause, so
    a document that PARSED and then failed to rebuild was reported ABSENT -- and absent
    is TOLERATED without --require-recall, so `check` answered ceilings_met and exit 0
    over a publication nothing could vouch for.

    The tree here is handed back ALREADY PARSED, which is what makes this a probe of the
    REBUILD rather than of json.loads. An earlier version of this test fed the depth as
    text, so the RecursionError came from the parse and the rebuild was never exercised
    at all -- it passed against the bug, and the reverse mutation is what caught it."""
    deep: object = 1
    for _ in range(300):
        deep = [deep]
    document = json.dumps({"metrics": []})
    monkeypatch.setattr(gate_module.json, "loads", lambda *a, **kw: {"metrics": deep})
    original_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(150)
    try:
        state, reason = gate_module._recall_measurement(document)
        assert state == "malformed", "present-and-untrustworthy is never absent"
        multiples, gauges, read_reason = read_multiples(document)
        assert (multiples, gauges) == ({}, {})
        assert read_reason
        verdict = check(document)
        assert verdict.exit_code == 2, "UNMEASURED, not the tolerated exit 0"
        assert verdict.status == STATUS_UNMEASURED
    finally:
        sys.setrecursionlimit(original_limit)
    monkeypatch.undo()


def test_check_parses_the_document_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-10 (A): `check` handed the same TEXT to two readers and each parsed it
    again, so a json.loads answering differently the second time let the ceilings be
    judged from one document and the recall from another. Both reported honestly about
    the tree they saw, and the combination was a lie: ceilings_met with the gauge "not
    published", exit 0. Counting the parses is the property; the verdict below is the
    consequence."""
    ceilings = {
        "metrics": [
            {
                "name": "oktografx_baseline_ceiling_multiple",
                "samples": [
                    {"value": 1.0, "labels": {"ceiling": "durable_commit"}},
                    {"value": 1.0, "labels": {"ceiling": "point_read"}},
                    {"value": 1.0, "labels": {"ceiling": "open_replay"}},
                ],
            }
        ]
    }
    calls: list[int] = []

    def counted(*args: object, **kwargs: object) -> object:
        calls.append(1)
        return ceilings if len(calls) == 1 else []

    monkeypatch.setattr(gate_module.json, "loads", counted)
    verdict = check("{}")
    monkeypatch.undo()
    assert len(calls) == 1, "one parse per decision; a second is a second document"
    assert verdict.status != "ceilings_met" or verdict.exit_code == 0
    # With a single snapshot the two readers agree by construction: the ceilings are met
    # AND the recall is genuinely absent in that same tree, which without --require-recall
    # is exit 0 honestly. The lie was reaching that verdict from two different documents.
    assert verdict.exit_code == 0


@pytest.mark.parametrize(
    "second", [[], {}, {"metrics": []}], ids=["list", "empty-dict", "empty-metrics"]
)
def test_a_divergent_second_parse_cannot_produce_a_passing_gate(
    monkeypatch: pytest.MonkeyPatch, second: object
) -> None:
    """Round-10 (A): whatever the second read would have said, it is never consulted --
    so no combination of two documents can be assembled into a verdict."""
    document = json.dumps(
        {
            "metrics": [
                {
                    "name": RECALL_METRIC,
                    "kind": "gauge",
                    "unit": "ratio",
                    "samples": [{"value": 0.10}],
                }
            ]
        }
    )
    calls: list[int] = []
    real_loads = json.loads

    def counted(text: str, *args: object, **kwargs: object) -> object:
        calls.append(1)
        return real_loads(text, *args, **kwargs) if len(calls) == 1 else second

    monkeypatch.setattr(gate_module.json, "loads", counted)
    verdict = check(document, require_recall=True)
    monkeypatch.undo()
    assert len(calls) == 1
    # 0.10 is below the 0.90 floor in the ONE snapshot, so the gate refuses.
    assert verdict.exit_code != 0
