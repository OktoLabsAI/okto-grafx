"""The logging event adapter (CONTRACT.md section 4.7, section 11 items 5 and 7; G1).

Three properties are worth a test each: the payload reaches the record as structured fields, the
content that must not reach a log file does not, and nothing the caller does can turn an event
into an exception. The last one matters most: an event is emitted after an operation has already
decided its outcome, so a failure here would corrupt a result that was already correct.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping

import pytest

from okto_grafx.adapters.events_logging import (
    DEFAULT_LOGGER_NAME,
    MAX_KEY_LENGTH,
    MAX_VALUE_LENGTH,
    REDACTED_MARKER,
    REDACTED_PAYLOAD_KEYS,
    LoggingEventSink,
)
from okto_grafx.domain.ports.events import EventSink


@pytest.fixture
def logger() -> Iterator[logging.Logger]:
    instance = logging.getLogger("okto_grafx.tests.events")
    instance.setLevel(logging.DEBUG)
    instance.propagate = False
    yield instance
    for handler in list(instance.handlers):
        instance.removeHandler(handler)


class _Capture(logging.Handler):
    """A handler that keeps the records instead of formatting them."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def capture(logger: logging.Logger) -> _Capture:
    handler = _Capture()
    logger.addHandler(handler)
    return handler


def test_the_sink_satisfies_the_port() -> None:
    assert isinstance(LoggingEventSink(), EventSink)


def test_the_default_logger_is_the_documented_one() -> None:
    assert LoggingEventSink().logger.name == DEFAULT_LOGGER_NAME
    assert LoggingEventSink().level == logging.INFO


def test_an_event_reaches_the_record_as_structured_fields(
    logger: logging.Logger, capture: _Capture
) -> None:
    sink = LoggingEventSink(logger)
    sink.emit("recovery.completed", {"outcome": "truncated", "records_discarded": 3, "lsn": 91})
    assert len(capture.records) == 1
    record = capture.records[0]
    assert record.levelno == logging.INFO
    assert record.okto_event == "recovery.completed"
    assert record.okto_payload == {
        "outcome": "truncated",
        "records_discarded": 3,
        "lsn": 91,
    }
    assert record.getMessage().startswith("recovery.completed ")
    assert "outcome='truncated'" in record.getMessage()


def test_the_message_is_sorted_so_two_identical_events_read_the_same(
    logger: logging.Logger, capture: _Capture
) -> None:
    sink = LoggingEventSink(logger)
    sink.emit("wal.recycled", {"segments": 2, "deferred": 1})
    sink.emit("wal.recycled", {"deferred": 1, "segments": 2})
    assert capture.records[0].getMessage() == capture.records[1].getMessage()
    assert capture.records[0].getMessage() == "wal.recycled deferred=1 segments=2"


def test_an_event_with_no_payload_still_logs_its_name(
    logger: logging.Logger, capture: _Capture
) -> None:
    LoggingEventSink(logger).emit("database.opened", {})
    assert capture.records[0].getMessage() == "database.opened"
    assert capture.records[0].okto_payload == {}


EXPECTED_REDACTED_KEYS: frozenset[str] = frozenset(
    {
        "credential",
        "email",
        "embedding",
        "key",
        "message",
        "password",
        "path",
        "payload",
        "query",
        "secret",
        "text",
        "token",
        "user",
        "username",
        "value",
        "values",
        "vector",
    }
)
"""The redaction set, pinned by hand (A56/A68).

Parametrising over the constant alone made the suite complicit: dropping "password" from it
removed the case that would have caught the drop, and the only visible symptom was the test
count falling by one. A count nobody asserts is not a signal, so the set is written out here and
compared, and the parametrised cases are driven from this copy rather than from the constant.
"""


def test_the_redaction_set_is_exactly_the_pinned_one() -> None:
    assert REDACTED_PAYLOAD_KEYS == EXPECTED_REDACTED_KEYS, {
        "dropped": sorted(EXPECTED_REDACTED_KEYS - REDACTED_PAYLOAD_KEYS),
        "added": sorted(REDACTED_PAYLOAD_KEYS - EXPECTED_REDACTED_KEYS),
    }
    assert len(REDACTED_PAYLOAD_KEYS) == 17


@pytest.mark.parametrize("key", sorted(EXPECTED_REDACTED_KEYS))
def test_every_declared_key_is_redacted(
    logger: logging.Logger, capture: _Capture, key: str
) -> None:
    LoggingEventSink(logger).emit("probe", {key: "sensitive-content"})
    assert capture.records[0].okto_payload == {key: REDACTED_MARKER}
    assert "sensitive-content" not in capture.records[0].getMessage()


def test_operational_identity_is_not_redacted(logger: logging.Logger, capture: _Capture) -> None:
    # The rule is about content, not about cardinality: an LSN or an epoch is engine state and
    # belongs in the narrative, even though it may not be a metric label.
    LoggingEventSink(logger).emit(
        "ledger.appended", {"lsn": 12, "epoch": 3, "origin_class": "forensic", "reason_code": 2}
    )
    assert capture.records[0].okto_payload == {
        "lsn": 12,
        "epoch": 3,
        "origin_class": "forensic",
        "reason_code": 2,
    }


def test_the_redaction_set_can_be_chosen_by_the_host(
    logger: logging.Logger, capture: _Capture
) -> None:
    sink = LoggingEventSink(logger, redacted_keys=frozenset({"owner_id"}))
    sink.emit("lease.taken", {"owner_id": "host-a", "path": "/var/data"})
    assert capture.records[0].okto_payload == {"owner_id": REDACTED_MARKER, "path": "/var/data"}
    assert sink.redacted_keys == frozenset({"owner_id"})


def test_the_value_and_key_bounds_are_the_pinned_ones() -> None:
    # A68: the numbers that decide how much of a payload reaches a log file are the contract,
    # and widening either of them is a change nobody would otherwise see.
    assert MAX_VALUE_LENGTH == 256
    assert MAX_KEY_LENGTH == 64


def test_a_key_longer_than_the_bound_is_truncated(
    logger: logging.Logger, capture: _Capture
) -> None:
    LoggingEventSink(logger).emit("probe", {"k" * 5_000: 1})
    key = next(iter(capture.records[0].okto_payload))
    assert len(key) == MAX_KEY_LENGTH
    assert key.endswith("...")


def test_a_long_string_is_truncated(logger: logging.Logger, capture: _Capture) -> None:
    LoggingEventSink(logger).emit("probe", {"detail": "x" * 5_000})
    detail = capture.records[0].okto_payload["detail"]
    assert isinstance(detail, str)
    assert len(detail) == MAX_VALUE_LENGTH
    assert detail.endswith("...")


def test_a_newline_cannot_forge_a_second_log_line(
    logger: logging.Logger, capture: _Capture
) -> None:
    LoggingEventSink(logger).emit(
        "probe\ninjected", {"detail": "first\nINFO forged second line", "key\nwith break": 1}
    )
    record = capture.records[0]
    assert "\n" not in record.getMessage()
    assert record.okto_event == "probe injected"
    assert record.okto_payload["detail"] == "first INFO forged second line"


def test_a_value_that_is_not_a_primitive_becomes_its_type_name(
    logger: logging.Logger, capture: _Capture
) -> None:
    LoggingEventSink(logger).emit(
        "probe", {"report": object(), "rows": [1, 2, 3], "flag": True, "ratio": 0.5, "none": None}
    )
    assert capture.records[0].okto_payload == {
        "report": "<object>",
        "rows": "<list>",
        "flag": True,
        "ratio": 0.5,
        "none": None,
    }


def test_the_level_is_respected(logger: logging.Logger, capture: _Capture) -> None:
    LoggingEventSink(logger, level=logging.WARNING).emit("probe", {"a": 1})
    assert capture.records[0].levelno == logging.WARNING


class _CountingPayload(Mapping[str, object]):
    """A payload that records every time it is read.

    Asserting that no record was emitted proves nothing about the guard: Logger.log performs its
    own level check, so a sink with the guard removed emits nothing either. The property the
    guard exists for is that the payload is never *read* when nobody is listening, and only the
    payload can report that.
    """

    def __init__(self, contents: dict[str, object]) -> None:
        self._contents = contents
        self.reads = 0

    def __getitem__(self, key: str) -> object:
        self.reads += 1
        return self._contents[key]

    def __iter__(self) -> Iterator[str]:
        self.reads += 1
        return iter(self._contents)

    def __len__(self) -> int:
        return len(self._contents)

    def items(self):  # type: ignore[override]
        self.reads += 1
        return self._contents.items()


def test_nothing_is_built_when_the_level_is_disabled(
    logger: logging.Logger, capture: _Capture
) -> None:
    logger.setLevel(logging.CRITICAL)
    payload = _CountingPayload({"a": 1, "detail": "x" * 4000})
    LoggingEventSink(logger, level=logging.DEBUG).emit("probe", payload)
    assert capture.records == []
    assert payload.reads == 0, (
        f"the payload was read {payload.reads} time(s) for an event nobody is listening to"
    )


def test_the_payload_is_read_when_somebody_is_listening(
    logger: logging.Logger, capture: _Capture
) -> None:
    # The control: the same payload, a level that is enabled, and the read does happen.
    payload = _CountingPayload({"a": 1})
    LoggingEventSink(logger, level=logging.INFO).emit("probe", payload)
    assert payload.reads >= 1
    assert capture.records[0].okto_payload == {"a": 1}


def test_a_payload_whose_values_explode_is_still_an_event(
    logger: logging.Logger, capture: _Capture
) -> None:
    class _Hostile:
        def __str__(self) -> str:
            raise RuntimeError("no string for you")

        def __repr__(self) -> str:
            raise RuntimeError("no repr either")

    LoggingEventSink(logger).emit("probe", {"safe": 1, _Hostile(): "x", "hostile": _Hostile()})
    payload = capture.records[0].okto_payload
    assert payload["safe"] == 1
    assert payload["hostile"] == "<_Hostile>"


def test_a_payload_that_is_not_a_mapping_is_absorbed(
    logger: logging.Logger, capture: _Capture
) -> None:
    LoggingEventSink(logger).emit("probe", ["not", "a", "mapping"])  # type: ignore[arg-type]
    assert capture.records[0].okto_payload == {"detail": "<unreadable payload>"}


def test_a_broken_logger_never_reaches_the_caller() -> None:
    class _BrokenLogger:
        def isEnabledFor(self, level: int) -> bool:
            return True

        def log(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("the logging stack is broken")

    sink = LoggingEventSink(_BrokenLogger())  # type: ignore[arg-type]
    assert sink.emit("probe", {"a": 1}) is None


def test_a_logger_that_cannot_even_answer_is_absorbed() -> None:
    class _Unusable:
        def isEnabledFor(self, level: int) -> bool:
            raise RuntimeError("not even a level")

    sink = LoggingEventSink(_Unusable())  # type: ignore[arg-type]
    assert sink.emit("probe", {"a": 1}) is None


def test_a_failing_handler_never_reaches_the_caller(logger: logging.Logger) -> None:
    class _Explosive(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            raise RuntimeError("handler failure")

        def handleError(self, record: logging.LogRecord) -> None:
            raise RuntimeError("even the error handler fails")

    logger.addHandler(_Explosive())
    assert LoggingEventSink(logger).emit("probe", {"a": 1}) is None
