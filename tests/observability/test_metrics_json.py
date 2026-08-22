"""The JSON metrics adapter (SPEC-M1 FR-14, OR-6: a selectable adapter with no code shortcut).

The document is the same measurement the text body carries, in a shape a batch job or a CI runner
can read without an HTTP scraper, so the round trip through ``json.loads`` is the real assertion
here. The file writer is tested against a real temporary directory because rotation is mechanism
and mechanism is only proved by running it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from okto_grafx.adapters.metrics_json import (
    DOCUMENT_FORMAT,
    DOCUMENT_VERSION,
    JsonMetricsSink,
    RotatingFileWriter,
)
from okto_grafx.adapters.metrics_openmetrics import MetricAggregator, OpenMetricsSink
from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.ports.metrics import MetricDescriptor, MetricKind, MetricsSink
from okto_grafx.engine.metrics_catalog import metric_names, register_catalog

PROBE_HISTOGRAM = MetricDescriptor(
    name="oktografx_probe_seconds",
    kind=MetricKind.HISTOGRAM,
    description="A probe histogram.",
    unit="seconds",
    buckets=(1.0, 2.0),
)


class _Collector:
    """A destination that keeps every document it was handed."""

    def __init__(self) -> None:
        self.documents: list[str] = []

    def __call__(self, document: str) -> None:
        self.documents.append(document)


def test_the_sink_satisfies_the_port() -> None:
    assert isinstance(JsonMetricsSink(_Collector()), MetricsSink)


def test_a_destination_that_is_not_callable_is_refused() -> None:
    with pytest.raises(GrafxConfigurationError, match="callable destination"):
        JsonMetricsSink("metrics.json")  # type: ignore[arg-type]


def test_publish_hands_one_json_document_to_the_destination() -> None:
    collector = _Collector()
    sink = JsonMetricsSink(collector)
    register_catalog(sink)
    sink.increment("oktografx_database_opens_total", 2.0)
    sink.set_gauge("oktografx_ledger_depth", 3.0, {"origin_class": "forensic"})

    returned = sink.publish()
    assert collector.documents == [returned]

    document = json.loads(returned)
    assert document["format"] == DOCUMENT_FORMAT
    assert document["version"] == DOCUMENT_VERSION
    assert {entry["name"] for entry in document["metrics"]} == metric_names()

    by_name = {entry["name"]: entry for entry in document["metrics"]}
    opens = by_name["oktografx_database_opens_total"]
    assert opens["kind"] == "counter"
    assert opens["samples"] == [{"labels": {}, "value": 2.0}]
    ledger = by_name["oktografx_ledger_depth"]
    assert ledger["unit"] == "entries"
    assert ledger["samples"] == [{"labels": {"origin_class": "forensic"}, "value": 3.0}]


def test_a_histogram_round_trips_with_its_cumulative_buckets() -> None:
    collector = _Collector()
    sink = JsonMetricsSink(collector)
    sink.register(PROBE_HISTOGRAM)
    for observation in (0.5, 1.5, 4.5):
        sink.observe("oktografx_probe_seconds", observation)

    entry = json.loads(sink.publish())["metrics"][0]
    assert entry["kind"] == "histogram"
    assert entry["samples"][0]["count"] == 3.0
    assert entry["samples"][0]["sum"] == 6.5
    assert entry["samples"][0]["buckets"] == [
        {"le": "1", "count": 1.0},
        {"le": "2", "count": 2.0},
        {"le": "+Inf", "count": 3.0},
    ]


def _strict(document: str) -> dict:
    """Parse with the non-JSON constants refused, the way a strict reader would."""
    def refuse(token: str) -> object:
        raise ValueError(f"not JSON: {token}")

    return json.loads(document, parse_constant=refuse)


def test_a_counter_that_overflows_still_produces_strict_json() -> None:
    """json.dumps would write the bare token Infinity, which no strict reader accepts.

    Emission refuses a non-finite value, so this can only arrive by accumulation; the text
    adapter renders the same number as +Inf and stays valid 0.0.4. Two adapters built from one
    collect() should not differ in whether their output can be read back at all.
    """
    sink = JsonMetricsSink(_Collector())
    sink.register(
        MetricDescriptor(
            name="oktografx_probe_total",
            kind=MetricKind.COUNTER,
            description="A probe counter.",
        )
    )
    for _ in range(3):
        sink.increment("oktografx_probe_total", 1e308)

    document = _strict(sink.document())
    entry = next(item for item in document["metrics"] if item["samples"])
    assert entry["samples"][0]["value"] == "+Inf"


def test_a_histogram_that_overflows_still_produces_strict_json() -> None:
    sink = JsonMetricsSink(_Collector())
    sink.register(PROBE_HISTOGRAM)
    for _ in range(3):
        sink.observe("oktografx_probe_seconds", 1e308)
    sample = _strict(sink.document())["metrics"][0]["samples"][0]
    assert sample["sum"] == "+Inf"
    assert sample["count"] == 3.0


def test_an_ordinary_document_still_carries_numbers_as_numbers() -> None:
    sink = JsonMetricsSink(_Collector())
    register_catalog(sink)
    sink.increment("oktografx_database_opens_total", 2.0)
    entry = next(
        item
        for item in _strict(sink.document())["metrics"]
        if item["name"] == "oktografx_database_opens_total"
    )
    assert entry["samples"][0]["value"] == 2.0
    assert isinstance(entry["samples"][0]["value"], float)


def test_the_document_is_stable_and_sorted() -> None:
    collector = _Collector()
    sink = JsonMetricsSink(collector)
    register_catalog(sink)
    sink.set_gauge("oktografx_vector_index_entries", 1.0, {"space": "beta"})
    sink.set_gauge("oktografx_vector_index_entries", 2.0, {"space": "alpha"})
    document = json.loads(sink.document())
    names = [entry["name"] for entry in document["metrics"]]
    assert names == sorted(names)
    entries = {entry["name"]: entry for entry in document["metrics"]}
    spaces = [sample["labels"]["space"] for sample in entries["oktografx_vector_index_entries"]["samples"]]
    assert spaces == ["alpha", "beta"]
    assert sink.document() == sink.document()


def test_the_registration_contract_is_the_same_as_the_text_adapter() -> None:
    sink = JsonMetricsSink(_Collector())
    register_catalog(sink)
    with pytest.raises(GrafxConfigurationError, match="was never registered"):
        sink.increment("oktografx_ghost_total")
    with pytest.raises(GrafxConfigurationError, match="is not one of them"):
        sink.set_gauge("oktografx_ledger_depth", 1.0, {"origin_class": "unknown"})
    with pytest.raises(GrafxConfigurationError, match="cannot be used as a gauge"):
        sink.set_gauge("oktografx_database_opens_total", 1.0)


def test_a_disabled_sink_publishes_declarations_with_no_samples() -> None:
    sink = JsonMetricsSink(_Collector(), enabled=False)
    register_catalog(sink)
    assert sink.enabled is False
    sink.increment("oktografx_database_opens_total")
    document = json.loads(sink.document())
    assert all(entry["samples"] == [] for entry in document["metrics"])


def test_the_sink_can_share_the_aggregator_of_the_text_adapter() -> None:
    aggregator = MetricAggregator()
    text = OpenMetricsSink(aggregator=aggregator)
    written = JsonMetricsSink(_Collector(), aggregator=aggregator)
    register_catalog(text)
    text.increment("oktografx_database_opens_total")
    document = json.loads(written.document())
    entry = next(item for item in document["metrics"] if item["name"] == "oktografx_database_opens_total")
    assert entry["samples"] == [{"labels": {}, "value": 1.0}]
    assert written.snapshot() == text.snapshot()


def test_the_timing_context_manager_reaches_the_document() -> None:
    sink = JsonMetricsSink(_Collector())
    sink.register(PROBE_HISTOGRAM)
    with sink.time("oktografx_probe_seconds"):
        pass
    assert sink.sample_value("oktografx_probe_seconds") == 1.0


def test_an_indented_document_is_still_valid_json() -> None:
    sink = JsonMetricsSink(_Collector(), indent=2)
    register_catalog(sink)
    document = sink.document()
    assert "\n  " in document
    assert json.loads(document)["version"] == DOCUMENT_VERSION


def test_a_destination_that_raises_is_reported_as_a_configuration_error() -> None:
    def broken(document: str) -> None:
        raise RuntimeError("the disk is on fire")

    sink = JsonMetricsSink(broken)
    with pytest.raises(GrafxConfigurationError, match="refused the document"):
        sink.publish()


def test_the_file_writer_appends_one_line_per_document(tmp_path: Path) -> None:
    destination = tmp_path / "metrics.json"
    writer = RotatingFileWriter(str(destination))
    assert writer.path == str(destination)
    sink = JsonMetricsSink(writer)
    register_catalog(sink)
    sink.increment("oktografx_database_opens_total")
    sink.publish()
    sink.increment("oktografx_database_opens_total")
    sink.publish()

    lines = destination.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first, second = (json.loads(line) for line in lines)
    assert _value(first, "oktografx_database_opens_total") == 1.0
    assert _value(second, "oktografx_database_opens_total") == 2.0


def _value(document: dict[str, object], name: str) -> float:
    entry = next(item for item in document["metrics"] if item["name"] == name)  # type: ignore[index]
    return float(entry["samples"][0]["value"])


DATABASE_FILES: tuple[tuple[str, int], ...] = (
    ("heap.dat", 1),
    ("catalog.dat", 0),
    ("grafx.meta", 1),
    ("index/person_id.idx", 1),
)
"""Files G6 says no sanctioned operation moves, renames or deletes.

The writer is not asked to recognise them. It is asked to recognise its own output and refuse
everything else, which covers these without anybody having had to list them -- the list exists
only so the test names the consequence it is preventing.
"""


@pytest.mark.parametrize(
    ("name", "backups"), DATABASE_FILES, ids=[row[0] for row in DATABASE_FILES]
)
def test_the_file_writer_refuses_a_file_it_did_not_create(
    tmp_path: Path, name: str, backups: int
) -> None:
    """G6 by construction, not by assertion: the destination is host-supplied (A8).

    Pointed at a database file, an earlier version appended JSON to it, renamed it to
    ``.1``, and with backups=0 removed it outright.
    """
    destination = tmp_path / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    original = b"OKTOGRFX" + bytes(4088)
    destination.write_bytes(original)

    writer = RotatingFileWriter(str(destination), max_bytes=16, backups=backups)
    sink = JsonMetricsSink(writer)
    register_catalog(sink)
    sink.increment("oktografx_database_opens_total")

    with pytest.raises(GrafxConfigurationError, match="not written by this writer"):
        sink.publish()

    assert destination.read_bytes() == original, "the file was written to"
    assert destination.exists(), "the file was removed"
    assert not (tmp_path / (name + ".1")).exists(), "the file was rotated away"
    # Repeating the refusal changes nothing either.
    with pytest.raises(GrafxConfigurationError):
        sink.publish()
    assert destination.read_bytes() == original


def test_the_file_writer_adopts_its_own_file_across_a_restart(tmp_path: Path) -> None:
    destination = tmp_path / "metrics.json"
    first = JsonMetricsSink(RotatingFileWriter(str(destination)))
    register_catalog(first)
    first.publish()
    first.publish()

    # A new writer, new process shape, same path: this file is recognisably ours.
    second = JsonMetricsSink(RotatingFileWriter(str(destination)))
    register_catalog(second)
    second.publish()
    assert len(destination.read_text(encoding="utf-8").splitlines()) == 3


def _try_to_swap(destination: Path, foreign: bytes) -> bool:
    """Replace the destination with a foreign file. Return False when the system refuses.

    The two families defend the same guarantee by opposite means, and the test has to accept
    both: POSIX lets the swap happen and the writer detects it, because holding the descriptor
    pins the inode so the reuse that would disguise it cannot occur; Windows refuses the unlink
    outright while the file is held, so the swap never happens at all. Asserting one mechanism
    would have made this test a statement about the platform rather than about the guarantee.
    """
    try:
        destination.unlink()
    except OSError:
        return False
    destination.write_bytes(foreign)
    return True


def test_a_file_swapped_underneath_the_writer_is_never_rotated(tmp_path: Path) -> None:
    destination = tmp_path / "metrics.json"
    writer = RotatingFileWriter(str(destination), max_bytes=100_000, backups=1)
    sink = JsonMetricsSink(writer)
    register_catalog(sink)
    sink.publish()
    ours = destination.read_bytes()

    foreign = b"OKTOGRFX" + bytes(64)
    swapped = _try_to_swap(destination, foreign)

    if swapped:
        # The writer must notice and refuse to touch what it now finds there.
        with pytest.raises(GrafxConfigurationError, match="not written by this writer"):
            sink.publish()
        assert destination.read_bytes() == foreign
    else:
        # The system refused the swap: the file is still the one the writer is holding.
        assert destination.read_bytes() == ours
        sink.publish()

    assert not (tmp_path / "metrics.json.1").exists()


def test_the_writer_checks_ownership_again_before_it_rotates(tmp_path: Path) -> None:
    """The check before the rename guards the window between writing and rotating.

    Claiming happens before each write, so within one uninterrupted call the second check cannot
    fire, which is why it is reached directly here. Between a write and the rename that follows
    it another process can put a different file at that path, and renaming that one is the thing
    G6 forbids.
    """
    destination = tmp_path / "metrics.json"
    writer = RotatingFileWriter(str(destination), max_bytes=100_000, backups=1)
    sink = JsonMetricsSink(writer)
    register_catalog(sink)
    sink.publish()
    ours = destination.read_bytes()

    foreign = b"OKTOGRFX" + bytes(64)
    swapped = _try_to_swap(destination, foreign)

    if swapped:
        with pytest.raises(GrafxConfigurationError, match="not the file this writer created"):
            writer._rotate()
        assert destination.read_bytes() == foreign
    else:
        assert destination.read_bytes() == ours

    assert not (tmp_path / "metrics.json.1").exists()


def test_the_writer_holds_the_file_it_claimed(tmp_path: Path) -> None:
    # The identity is the descriptor, not the path: whatever the platform allows to happen to
    # the name, the writer keeps hold of the file it decided was its own.
    destination = tmp_path / "metrics.json"
    writer = RotatingFileWriter(str(destination), max_bytes=100_000, backups=1)
    sink = JsonMetricsSink(writer)
    register_catalog(sink)
    sink.publish()

    assert writer._handle is not None
    assert writer._still_ours()
    writer.close()
    assert writer._handle is None
    # Closing releases it; the next write claims it again and recognises its own document.
    sink.publish()
    assert writer._handle is not None
    assert len(destination.read_text(encoding="utf-8").splitlines()) == 2


def test_an_empty_destination_is_adopted(tmp_path: Path) -> None:
    # Nothing to destroy, so nothing to refuse.
    destination = tmp_path / "metrics.json"
    destination.write_bytes(b"")
    sink = JsonMetricsSink(RotatingFileWriter(str(destination)))
    register_catalog(sink)
    sink.publish()
    assert destination.read_text(encoding="utf-8").strip()


def test_the_file_writer_rotates_when_it_passes_the_bound(tmp_path: Path) -> None:
    destination = tmp_path / "metrics.json"
    writer = RotatingFileWriter(str(destination), max_bytes=64, backups=2)
    writer("a" * 100)
    assert not destination.exists()
    assert (tmp_path / "metrics.json.1").read_text(encoding="utf-8").startswith("a")

    writer("b" * 100)
    assert (tmp_path / "metrics.json.1").read_text(encoding="utf-8").startswith("b")
    assert (tmp_path / "metrics.json.2").read_text(encoding="utf-8").startswith("a")

    writer("c" * 100)
    assert (tmp_path / "metrics.json.1").read_text(encoding="utf-8").startswith("c")
    assert (tmp_path / "metrics.json.2").read_text(encoding="utf-8").startswith("b")
    assert not (tmp_path / "metrics.json.3").exists()


def test_the_file_writer_can_keep_no_backup_at_all(tmp_path: Path) -> None:
    destination = tmp_path / "metrics.json"
    writer = RotatingFileWriter(str(destination), max_bytes=16, backups=0)
    writer("x" * 40)
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


def test_the_file_writer_adds_the_line_break_only_when_it_is_missing(tmp_path: Path) -> None:
    destination = tmp_path / "metrics.json"
    writer = RotatingFileWriter(str(destination))
    writer("{}\n")
    writer("{}")
    assert destination.read_text(encoding="utf-8") == "{}\n{}\n"


LOCALIZED_STRERROR: str = "Acesso negado: n" + chr(227) + "o autorizado"
"""What ``strerror`` looks like on a system that is not running in English.

The source gate reads the file, and in the file the message is an ASCII literal -- the localized
text only exists once the operating system fills it in at runtime. No static check can see that,
so the only way to hold the rule is to make the platform's half of the sentence non-ASCII on
purpose and look at what comes out.
"""


def _refuse(*args: object, **kwargs: object) -> object:
    """Fail the way a non-English system fails."""
    failure = OSError(13, LOCALIZED_STRERROR)
    failure.winerror = 5
    raise failure


@pytest.mark.parametrize("operation", ["create", "write", "read"])
def test_a_localized_platform_message_never_reaches_the_error_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """G1 and A7: the sentence is ours in en-US ASCII; the platform's text travels in details.

    Concatenating ``strerror`` put a string the operating system chose, in whatever language it
    was configured for, inside a Grafx message -- the one part of an error that a gate, a log
    scraper and an operator all read as ours.
    """
    import builtins
    import os as os_module

    destination = tmp_path / "metrics.json"
    if operation == "create":
        monkeypatch.setattr(os_module, "open", _refuse)
    elif operation == "write":
        monkeypatch.setattr(os_module, "write", _refuse)
    else:
        # Reading happens only when adopting a destination that already exists.
        destination.write_text("{}" + chr(10), encoding="utf-8")
        monkeypatch.setattr(builtins, "open", _refuse)

    writer = RotatingFileWriter(str(destination))
    with pytest.raises(GrafxConfigurationError) as failure:
        writer("{}")

    error = failure.value
    assert error.message.isascii(), f"the message carried non-ASCII text: {error.message!r}"
    assert str(error).isascii(), f"the rendered error carried non-ASCII text: {str(error)!r}"
    assert LOCALIZED_STRERROR not in str(error)
    # The diagnostic is kept, just not as part of the sentence.
    assert error.details["platform_message"] == LOCALIZED_STRERROR
    assert error.details["errno"] == 13
    assert error.details["winerror"] == 5
    assert error.details["value"] == str(destination)


def test_the_file_writer_refuses_an_unusable_configuration(tmp_path: Path) -> None:
    with pytest.raises(GrafxConfigurationError, match="destination path"):
        RotatingFileWriter("")
    with pytest.raises(GrafxConfigurationError, match="positive size bound"):
        RotatingFileWriter(str(tmp_path / "metrics.json"), max_bytes=0)
    with pytest.raises(GrafxConfigurationError, match="negative number of backups"):
        RotatingFileWriter(str(tmp_path / "metrics.json"), backups=-1)


def test_the_file_writer_reports_an_unreachable_destination(tmp_path: Path) -> None:
    # The failure surfaces when the writer tries to claim the destination, which is before it
    # writes anything, so the message names creation rather than writing.
    unreachable = tmp_path / "missing-directory" / "metrics.json"
    writer = RotatingFileWriter(str(unreachable))
    with pytest.raises(GrafxConfigurationError, match="could not create its destination") as fail:
        writer("{}")
    assert fail.value.details["value"] == str(unreachable)


def test_a_device_that_stores_only_part_of_a_document_never_leaves_a_truncated_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A short write is not a written document, and it must never be reported as one.

    ``os.write`` may store fewer bytes than it was handed and say so by RETURNING the count, not
    by raising -- a device that fills up mid-document is the ordinary way it happens. The writer
    ignored that count, so it appended a fragment, returned normally, and let the caller believe
    the whole document was on the device; the next document then landed straight behind the
    fragment and the file held a line that is two half documents. Measured against the old code:
    8 of 62 bytes stored, no error, and the following document concatenated onto the stump.

    The device here does exactly what a filling one does: it accepts part of the buffer once and
    then behaves normally. Either outcome is acceptable -- the whole document on the device, or a
    Grafx refusal -- and the one thing that is not is a silent fragment.
    """
    destination = tmp_path / "metrics.json"
    real_write = os.write
    shortened: list[int] = []

    def store_only_part(handle: int, data: object) -> int:
        # Only this adapter's own document is shortened. Patching os.write is a global act, and
        # a probe that also truncated pytest's own writes would be testing the harness.
        payload = bytes(data)  # type: ignore[arg-type]
        if not shortened and payload.startswith(b'{"format"'):
            shortened.append(len(payload))
            return real_write(handle, payload[:8])
        return real_write(handle, payload)

    monkeypatch.setattr(os, "write", store_only_part)
    writer = RotatingFileWriter(str(destination))
    document = json.dumps({"format": DOCUMENT_FORMAT, "version": DOCUMENT_VERSION, "metrics": []})
    try:
        writer(document)
    finally:
        monkeypatch.undo()
        writer.close()

    assert shortened, "the probe never got a buffer long enough to shorten"
    assert destination.read_text(encoding="utf-8") == document + "\n"


def test_a_destination_that_accepts_no_bytes_is_refused_rather_than_written_to_for_ever(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The completion loop is bounded: a device that accepts nothing is refused, never spun on.

    This is the other side of the rule above (A85). Writing until everything is stored is only
    safe if "stored nothing and raised nothing" ends the loop, because a hang is the one outcome
    worse than a refusal, and the refusal has to say how far it got.
    """
    destination = tmp_path / "metrics.json"
    real_write = os.write

    def accept_nothing(handle: int, data: object) -> int:
        # Same narrowing as above: only the document this test writes meets a full device.
        payload = bytes(data)  # type: ignore[arg-type]
        return 0 if payload.startswith(b"{") else real_write(handle, payload)

    writer = RotatingFileWriter(str(destination))
    monkeypatch.setattr(os, "write", accept_nothing)
    try:
        with pytest.raises(GrafxConfigurationError, match="accepted no more") as failure:
            writer("{}")
    finally:
        monkeypatch.setattr(os, "write", real_write)
        writer.close()

    assert failure.value.details["stored"] == 0
    assert failure.value.details["expected"] == len("{}\n")
    assert failure.value.details["value"] == str(destination)
