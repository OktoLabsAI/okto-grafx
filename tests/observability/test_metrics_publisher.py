"""The GET /metrics endpoint of the default install (SPEC-M1 FR-14, OR-6, IR-3; api_c5e17ca5).

The frozen API contract states three answers and this file proves all three against a real
socket: 200 with the exposition body, 404 for anything else, and 503 when the publisher holds no
recording sink. The port is always ephemeral and the interface is always loopback, so the tests
never touch the network and never collide with another process.
"""

from __future__ import annotations

import socket
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator

import pytest

from okto_grafx.adapters.metrics_noop import NoOpMetricsSink
from okto_grafx.adapters.metrics_openmetrics import (
    CONTENT_TYPE,
    OpenMetricsPublisher,
    OpenMetricsSink,
    _MetricsRequestHandler,
    _MetricsServer,
)
from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.engine.metrics_catalog import register_catalog

TIMEOUT: float = 5.0
"""Every request is bounded, so a hung server fails the test rather than the run."""


class _RecordingEvents:
    """An EventSink that keeps what it was told, so a silenced fault is still observable."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def emit(self, event: str, payload: dict[str, object]) -> None:
        self.events.append((event, dict(payload)))


def _fetch(url: str) -> tuple[int, str, str]:
    """Return status, content type and body of one request, treating an error as an answer."""
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
            return (
                response.status,
                response.headers.get("Content-Type", ""),
                response.read().decode("utf-8"),
            )
    except urllib.error.HTTPError as failure:
        with failure:
            return (
                failure.code,
                failure.headers.get("Content-Type", ""),
                failure.read().decode("utf-8"),
            )


@pytest.fixture
def recording_sink() -> OpenMetricsSink:
    sink = OpenMetricsSink()
    register_catalog(sink)
    sink.set_gauge("oktografx_ledger_depth", 0, {"origin_class": "reapplicable"})
    sink.set_gauge("oktografx_ledger_depth", 0, {"origin_class": "forensic"})
    return sink


@pytest.fixture
def publisher(recording_sink: OpenMetricsSink) -> Iterator[OpenMetricsPublisher]:
    endpoint = OpenMetricsPublisher(recording_sink, port=0)
    endpoint.start()
    try:
        yield endpoint
    finally:
        endpoint.stop()


def test_the_publisher_binds_an_ephemeral_loopback_port(publisher: OpenMetricsPublisher) -> None:
    assert publisher.running is True
    assert publisher.host == "127.0.0.1"
    assert publisher.port > 0
    assert publisher.url == f"http://127.0.0.1:{publisher.port}/metrics"


def test_get_metrics_answers_two_hundred_with_the_exposition_body(
    publisher: OpenMetricsPublisher, recording_sink: OpenMetricsSink
) -> None:
    status, content_type, body = _fetch(publisher.url)
    assert status == 200
    assert content_type == CONTENT_TYPE == "text/plain; version=0.0.4"
    assert body == recording_sink.render()
    assert (
        "# HELP oktografx_ledger_depth Number of unapplied-work ledger entries by origin class."
        in body
    )
    assert 'oktografx_ledger_depth{origin_class="forensic"} 0' in body


def test_the_body_reflects_what_was_measured_between_two_scrapes(
    publisher: OpenMetricsPublisher, recording_sink: OpenMetricsSink
) -> None:
    _, _, first = _fetch(publisher.url)
    assert "\noktografx_database_opens_total 1\n" not in first
    recording_sink.increment("oktografx_database_opens_total")
    _, _, second = _fetch(publisher.url)
    assert "\noktografx_database_opens_total 1\n" in second


@pytest.mark.parametrize("path", ["/", "/metric", "/metrics/", "/healthz", "/metricsx"])
def test_any_other_path_answers_four_hundred_and_four(
    publisher: OpenMetricsPublisher, path: str
) -> None:
    status, _, body = _fetch(f"http://{publisher.host}:{publisher.port}{path}")
    assert status == 404
    assert body == "Not found.\n"


def test_a_query_string_still_reaches_the_metrics_body(publisher: OpenMetricsPublisher) -> None:
    status, content_type, _ = _fetch(f"{publisher.url}?collect=all")
    assert status == 200
    assert content_type == CONTENT_TYPE


def test_a_write_method_is_refused(publisher: OpenMetricsPublisher) -> None:
    request = urllib.request.Request(publisher.url, data=b"", method="POST")
    with pytest.raises(urllib.error.HTTPError) as failure:
        urllib.request.urlopen(request, timeout=TIMEOUT)
    assert failure.value.code == 404


def test_a_publisher_without_a_sink_answers_five_hundred_and_three() -> None:
    endpoint = OpenMetricsPublisher(None, port=0)
    endpoint.start()
    try:
        status, content_type, body = _fetch(endpoint.url)
    finally:
        endpoint.stop()
    assert status == 503
    assert content_type.startswith("text/plain")
    assert "publisher holds no recording sink" in body


def test_a_publisher_over_the_no_op_sink_answers_five_hundred_and_three() -> None:
    endpoint = OpenMetricsPublisher(NoOpMetricsSink(), port=0)
    endpoint.start()
    try:
        status, _, _ = _fetch(endpoint.url)
        assert status == 503
        # A wrong path is still a wrong path, even with no sink behind the endpoint.
        assert _fetch(f"http://{endpoint.host}:{endpoint.port}/")[0] == 404
    finally:
        endpoint.stop()


def test_a_publisher_over_a_sink_that_is_not_recording_answers_five_hundred_and_three() -> None:
    sink = OpenMetricsSink(enabled=False)
    register_catalog(sink)
    endpoint = OpenMetricsPublisher(sink, port=0)
    endpoint.start()
    try:
        assert _fetch(endpoint.url)[0] == 503
    finally:
        endpoint.stop()


def test_starting_twice_is_refused_and_stopping_twice_is_harmless(
    recording_sink: OpenMetricsSink,
) -> None:
    endpoint = OpenMetricsPublisher(recording_sink, port=0)
    endpoint.start()
    try:
        with pytest.raises(GrafxConfigurationError, match="already running"):
            endpoint.start()
    finally:
        endpoint.stop()
    endpoint.stop()
    assert endpoint.running is False


def test_a_stopped_publisher_releases_its_port(recording_sink: OpenMetricsSink) -> None:
    endpoint = OpenMetricsPublisher(recording_sink, port=0)
    port = endpoint.start()
    assert _fetch(endpoint.url)[0] == 200
    endpoint.stop()
    assert endpoint.running is False
    # The same port can be bound again, which is what proves the socket really was closed.
    again = OpenMetricsPublisher(recording_sink, port=port)
    again.start()
    try:
        assert again.port == port
        assert _fetch(again.url)[0] == 200
    finally:
        again.stop()


def test_the_publisher_works_as_a_context_manager(recording_sink: OpenMetricsSink) -> None:
    with OpenMetricsPublisher(recording_sink, port=0) as endpoint:
        assert endpoint.running is True
        assert _fetch(endpoint.url)[0] == 200
    assert endpoint.running is False


def test_binding_a_port_that_is_taken_reports_a_configuration_error(
    recording_sink: OpenMetricsSink,
) -> None:
    first = OpenMetricsPublisher(recording_sink, port=0)
    port = first.start()
    try:
        second = OpenMetricsPublisher(recording_sink, port=port)
        with pytest.raises(GrafxConfigurationError, match="could not bind"):
            second.start()
        assert second.running is False
    finally:
        first.stop()


def test_concurrent_scrapes_are_all_served(publisher: OpenMetricsPublisher) -> None:
    results: list[int] = []
    lock = threading.Lock()

    def scrape() -> None:
        status, _, _ = _fetch(publisher.url)
        with lock:
            results.append(status)

    threads = [threading.Thread(target=scrape, name=f"scrape-{index}") for index in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)
    assert results == [200] * 6


def test_an_aborted_scrape_writes_nothing_to_the_standard_error_of_the_host(
    publisher: OpenMetricsPublisher, capfd: pytest.CaptureFixture[str]
) -> None:
    # A scraper that gives up on its own scrape_timeout closes the connection before reading the
    # response. That is ordinary operation on this endpoint, and the default handler of the
    # standard library would print a full traceback per occurrence into a stream the host of an
    # embedded database never opted into.
    capfd.readouterr()
    for _ in range(5):
        client = socket.create_connection((publisher.host, publisher.port), timeout=TIMEOUT)
        client.sendall(b"GET /metrics HTTP/1.1\r\nHost: localhost\r\n\r\n")
        client.close()
    # Five complete scrapes afterwards: they force the server to do real work on the same
    # threads, which is what gives the aborted handlers time to reach their own failure.
    for _ in range(5):
        assert _fetch(publisher.url)[0] == 200
    captured = capfd.readouterr()
    assert captured.err == "", f"an aborted scrape reached the standard error: {captured.err!r}"
    assert "Traceback" not in captured.out


def test_the_endpoint_keeps_serving_after_a_client_disappears(
    publisher: OpenMetricsPublisher,
) -> None:
    for _ in range(3):
        client = socket.create_connection((publisher.host, publisher.port), timeout=TIMEOUT)
        client.sendall(b"GET /metrics HTTP/1.1\r\nHost: localhost\r\n\r\n")
        client.close()
    status, content_type, body = _fetch(publisher.url)
    assert status == 200
    assert content_type == CONTENT_TYPE
    assert body.startswith("# HELP ")


def test_a_write_that_fails_because_the_client_left_is_absorbed() -> None:
    # The deterministic half of the proof: no socket, no thread, no timing.
    class _DepartedClient:
        def write(self, payload: bytes) -> int:
            raise ConnectionAbortedError(10053, "An established connection was aborted.")

    handler = _MetricsRequestHandler.__new__(_MetricsRequestHandler)
    handler.wfile = _DepartedClient()  # type: ignore[assignment]
    handler.requestline = "GET /metrics HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.command = "GET"
    handler.close_connection = False

    assert handler._send(200, b"body", "text/plain", with_body=True) is None
    assert handler.close_connection is True


@pytest.mark.parametrize(
    "failure",
    [
        ConnectionAbortedError(10053, "aborted"),
        ConnectionResetError(10054, "reset"),
        BrokenPipeError(32, "broken pipe"),
        TimeoutError("the client stopped reading"),
    ],
    ids=["aborted", "reset", "broken-pipe", "timeout"],
)
def test_a_connection_teardown_is_silent_and_is_not_reported_as_a_fault(
    failure: BaseException, capfd: pytest.CaptureFixture[str]
) -> None:
    events = _RecordingEvents()
    server = _MetricsServer.__new__(_MetricsServer)
    server.sink = None
    server.events = events
    capfd.readouterr()
    try:
        raise failure
    except BaseException:
        server.handle_error(None, ("127.0.0.1", 0))
    assert capfd.readouterr().err == ""
    assert events.events == []


def test_a_genuine_internal_fault_goes_to_the_event_sink_and_not_to_stderr(
    capfd: pytest.CaptureFixture[str],
) -> None:
    events = _RecordingEvents()
    server = _MetricsServer.__new__(_MetricsServer)
    server.sink = None
    server.events = events
    capfd.readouterr()
    try:
        raise ValueError("a genuine internal fault")
    except ValueError:
        server.handle_error(None, ("127.0.0.1", 0))
    assert capfd.readouterr().err == ""
    assert events.events == [("metrics.publisher_request_failed", {"error": "ValueError"})]


def test_a_publisher_without_an_event_sink_still_stays_silent(
    capfd: pytest.CaptureFixture[str],
) -> None:
    server = _MetricsServer.__new__(_MetricsServer)
    server.sink = None
    server.events = None
    capfd.readouterr()
    try:
        raise ValueError("a genuine internal fault")
    except ValueError:
        server.handle_error(None, ("127.0.0.1", 0))
    assert capfd.readouterr().err == ""


def test_a_broken_event_sink_cannot_break_the_error_path(
    capfd: pytest.CaptureFixture[str],
) -> None:
    class _BrokenEvents:
        def emit(self, event: str, payload: dict[str, object]) -> None:
            raise RuntimeError("the event sink is broken too")

    server = _MetricsServer.__new__(_MetricsServer)
    server.sink = None
    server.events = _BrokenEvents()
    capfd.readouterr()
    try:
        raise ValueError("a genuine internal fault")
    except ValueError:
        assert server.handle_error(None, ("127.0.0.1", 0)) is None
    assert capfd.readouterr().err == ""


def test_the_publisher_carries_its_event_sink_to_the_server(
    recording_sink: OpenMetricsSink,
) -> None:
    events = _RecordingEvents()
    endpoint = OpenMetricsPublisher(recording_sink, port=0, events=events)
    endpoint.start()
    try:
        assert _fetch(endpoint.url)[0] == 200
        assert events.events == []
    finally:
        endpoint.stop()


def test_the_publisher_leaves_no_thread_behind(recording_sink: OpenMetricsSink) -> None:
    before = {thread.name for thread in threading.enumerate()}
    endpoint = OpenMetricsPublisher(recording_sink, port=0)
    endpoint.start()
    assert any(thread.name == "oktografx-metrics" for thread in threading.enumerate())
    _fetch(endpoint.url)
    endpoint.stop()
    after = {thread.name for thread in threading.enumerate()}
    assert "oktografx-metrics" not in after
    assert before == after or before <= after
