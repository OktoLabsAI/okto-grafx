"""The GET /metrics endpoint of the default install (SPEC-M1 FR-14, OR-6, IR-3; api_c5e17ca5).

The frozen API contract states three answers and this file proves all three against a real
socket: 200 with the exposition body, 404 for anything else, and 503 when the publisher holds no
recording sink. The port is always ephemeral and the interface is always loopback, so the tests
never touch the network and never collide with another process.
"""

from __future__ import annotations

import contextlib
import selectors
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator

import pytest

from okto_grafx.adapters.metrics_noop import NoOpMetricsSink
from okto_grafx.adapters.metrics_openmetrics import (
    CONTENT_TYPE,
    DEFAULT_CONNECTION_TIMEOUT_SECONDS,
    _MAX_HEAD_BYTES,
    _POLL_INTERVAL_SECONDS,
    _head_is_complete,
    DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
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


def sink_with_catalog() -> OpenMetricsSink:
    """Return a recording sink with the whole catalog registered and one value set."""
    instance = OpenMetricsSink()
    register_catalog(instance)
    instance.set_gauge("oktografx_ledger_depth", 0, {"origin_class": "forensic"})
    return instance


def _bare_server(events: object | None) -> _MetricsServer:
    """Build a server object without binding a socket, for the error paths that need no port."""
    server = _MetricsServer.__new__(_MetricsServer)
    server.sink = None
    server.events = events
    server._stopping = False
    server._registry_lock = threading.Lock()
    server._connections = set()
    server._handlers = set()
    return server


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


class _ResettingSocket:
    """A socket that yields part of an answer and is then reset, as Windows does.

    The endpoint refuses an over-long head and closes while the client is still writing, and a
    close with unread bytes queued is answered with RST. The bytes already delivered are still
    the server's answer; the read simply ends by raising instead of returning empty. Driving
    that from a live socket is a race -- it reproduced twice in ten full-suite runs -- so the
    contract of the reader is asserted here directly, where it is deterministic.
    """

    def __init__(self, chunks: list[bytes], failure: BaseException) -> None:
        self._chunks = list(chunks)
        self._failure = failure
        self.timeout: float | None = None

    def settimeout(self, value: float) -> None:
        self.timeout = value

    def recv(self, size: int) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        raise self._failure


@pytest.mark.parametrize(
    "failure",
    [
        ConnectionResetError(10054, "An existing connection was forcibly closed"),
        ConnectionAbortedError(10053, "An established connection was aborted"),
        OSError(10038, "socket operation on non-socket"),
        OSError("a plain unmapped failure"),
        TimeoutError("the peer stopped talking"),
    ],
    ids=["reset", "aborted", "not-a-socket", "unmapped", "timeout"],
)
def test_the_reader_returns_the_answer_that_arrived_before_a_reset(
    failure: BaseException,
) -> None:
    body = (
        b"HTTP/1.1 414 URI Too Long" + CRLF + b"Content-Length: 9" + CRLF + CRLF
        + b"Refused." + BARE_LF
    )
    client = _ResettingSocket([body[:20], body[20:]], failure)
    answer = _drain(client)  # type: ignore[arg-type]
    assert answer == body.decode("utf-8")
    assert client.timeout == TIMEOUT


def test_the_reader_returns_what_it_has_even_if_the_reset_comes_first() -> None:
    client = _ResettingSocket([], ConnectionResetError(10054, "reset"))
    assert _drain(client) == ""  # type: ignore[arg-type]


def test_the_publisher_binds_an_ephemeral_loopback_port(publisher: OpenMetricsPublisher) -> None:
    assert publisher.running is True
    assert publisher.host == "127.0.0.1"
    assert publisher.port > 0
    assert publisher.url == f"http://127.0.0.1:{publisher.port}/metrics"


def test_the_publisher_binds_and_serves_ipv6_loopback(
    recording_sink: OpenMetricsSink,
) -> None:
    if not socket.has_ipv6:
        pytest.skip("this host has no IPv6 socket support")
    probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        probe.bind(("::1", 0))
    except OSError as failure:
        pytest.skip(f"this host cannot bind IPv6 loopback: {failure}")
    finally:
        probe.close()

    endpoint = OpenMetricsPublisher(recording_sink, host="::1", port=0)
    endpoint.start()
    try:
        assert endpoint.host == "::1"
        assert endpoint.url == f"http://[::1]:{endpoint.port}/metrics"
        answer = _raw(
            endpoint,
            b"GET /metrics HTTP/1.1\r\nHost: [::1]\r\nConnection: close\r\n\r\n",
        )
        assert "HTTP/1.1 200 OK" in answer
        assert "oktografx_ledger_depth" in answer
    finally:
        endpoint.stop()


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


UNBINDABLE_HOST: str = "192.0.2.1"
"""TEST-NET-1 of RFC 5737: routable nowhere, assigned to no interface, on either family."""


def test_a_port_another_socket_owns_is_refused(recording_sink: OpenMetricsSink) -> None:
    """Amendment A30: the publisher never reports success on an address it does not own.

    This used to be a comment explaining that the two families disagreed. They do disagree about
    the option, not about the guarantee: POSIX needs SO_REUSEADDR for the TIME_WAIT rebind, and
    Windows needs SO_EXCLUSIVEADDRUSE because SO_REUSEADDR there lets a second socket take a
    live address. Documenting the divergence was the wrong move; both families refuse now.
    """
    impostor = socket.socket()
    impostor.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    impostor.bind(("127.0.0.1", 0))
    impostor.listen(5)
    port = impostor.getsockname()[1]
    try:
        endpoint = OpenMetricsPublisher(recording_sink, port=port)
        with pytest.raises(GrafxConfigurationError, match="could not bind"):
            endpoint.start()
        assert endpoint.running is False
    finally:
        impostor.close()


def test_a_second_publisher_cannot_take_the_port_of_the_first(
    recording_sink: OpenMetricsSink,
) -> None:
    first = OpenMetricsPublisher(recording_sink, port=0)
    port = first.start()
    try:
        second = OpenMetricsPublisher(recording_sink, port=port)
        with pytest.raises(GrafxConfigurationError, match="could not bind"):
            second.start()
        assert second.running is False
        # The one that does own the port is unaffected by the attempt.
        assert _fetch(first.url)[0] == 200
    finally:
        first.stop()


def test_the_address_option_matches_the_family(recording_sink: OpenMetricsSink) -> None:
    # The guarantee is one; the option that delivers it is not. Asserting which one is in force
    # keeps a future edit from restoring the flag that made Windows report a false success.
    endpoint = OpenMetricsPublisher(recording_sink, port=0)
    endpoint.start()
    try:
        server = endpoint._server
        assert server is not None
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            assert server.allow_reuse_address is False
            assert server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE) != 0
        else:
            assert server.allow_reuse_address is True
            assert server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) != 0
    finally:
        endpoint.stop()


def test_binding_an_impossible_address_reports_a_configuration_error(
    recording_sink: OpenMetricsSink,
) -> None:
    baseline = _thread_baseline()
    endpoint = OpenMetricsPublisher(recording_sink, host=UNBINDABLE_HOST, port=0)
    with pytest.raises(GrafxConfigurationError, match="could not bind"):
        endpoint.start()
    assert endpoint.running is False
    # live_handlers reads a constant while no server exists, so it would pass either way; the
    # property worth asserting is that the failed start began nothing at all.
    assert_no_thread_left_behind(baseline)
    # A refused bind leaves the publisher usable rather than half-built.
    usable = OpenMetricsPublisher(recording_sink, port=0)
    usable.start()
    try:
        assert _fetch(usable.url)[0] == 200
    finally:
        usable.stop()


def test_a_port_the_operating_system_refuses_is_a_configuration_error(
    recording_sink: OpenMetricsSink,
) -> None:
    # The publisher refuses an out-of-range port before the socket layer can raise anything of
    # its own, so the caller always sees a Grafx error rather than an OverflowError.
    with pytest.raises(GrafxConfigurationError, match="between 0 and 65535"):
        OpenMetricsPublisher(recording_sink, port=99999)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("port", -1),
        ("port", 65536),
        ("port", "8080"),
        ("port", None),
        ("port", True),
        ("host", None),
        ("host", ""),
        ("connection_timeout", "x"),
        ("connection_timeout", -1.0),
        ("connection_timeout", 0.0),
        ("connection_timeout", None),
        ("connection_timeout", float("inf")),
        ("shutdown_timeout", 0),
        ("shutdown_timeout", "soon"),
    ],
    ids=lambda item: repr(item),
)
def test_an_unusable_publisher_setting_is_refused_at_construction(
    field: str, value: object
) -> None:
    # A deadline of zero or a port that is a string does not fail by accident at bind time: it
    # starts cleanly and then blacks out every scrape. That belongs to the build.
    with pytest.raises(GrafxConfigurationError) as failure:
        OpenMetricsPublisher(None, **{field: value})
    assert failure.value.details["field"] == field


def test_the_publisher_deadlines_are_the_pinned_ones() -> None:
    # A68: these two decide how long an idle connection lives and how long a stop waits, and
    # widening either is a change to behaviour that nothing else would report.
    assert DEFAULT_CONNECTION_TIMEOUT_SECONDS == 10.0
    assert DEFAULT_SHUTDOWN_TIMEOUT_SECONDS == 5.0
    assert _POLL_INTERVAL_SECONDS == 0.05
    assert _MAX_HEAD_BYTES == 65536 + 16384
    # And the defaults really are what an unconfigured publisher uses.
    endpoint = OpenMetricsPublisher(None, port=0)
    assert endpoint._connection_timeout == DEFAULT_CONNECTION_TIMEOUT_SECONDS
    assert endpoint._shutdown_timeout == DEFAULT_SHUTDOWN_TIMEOUT_SECONDS


def test_a_valid_publisher_setting_is_accepted() -> None:
    endpoint = OpenMetricsPublisher(None, host="127.0.0.1", port=0, connection_timeout=0.5)
    assert endpoint.host == "127.0.0.1"
    assert endpoint.port == 0


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
    server = _bare_server(events)
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
    server = _bare_server(events)
    capfd.readouterr()
    try:
        raise ValueError("a genuine internal fault")
    except ValueError:
        server.handle_error(None, ("127.0.0.1", 0))
    assert capfd.readouterr().err == ""
    assert events.events == [("metrics.publisher_request_failed", {"error": "ValueError"})]


def test_a_non_socket_fault_while_stopping_is_not_reported_as_one(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Tearing a connection down mid-request does not always raise an OSError.

    Reading a file object whose socket was just closed raises ValueError, not OSError, so the
    teardown tuple alone would report a shutdown artefact as an internal fault and wake somebody
    for it. While the server is stopping, every fault is a shutdown artefact.
    """
    events = _RecordingEvents()
    server = _bare_server(events)
    server._stopping = True
    capfd.readouterr()
    try:
        raise ValueError("I/O operation on closed file.")
    except ValueError:
        server.handle_error(None, ("127.0.0.1", 0))
    assert events.events == [], "a fault during shutdown was reported as an internal one"
    assert capfd.readouterr().err == ""


def test_the_same_fault_is_reported_when_the_server_is_not_stopping(
    capfd: pytest.CaptureFixture[str],
) -> None:
    # The control: the stopping flag is what silences it, not the type of the error.
    events = _RecordingEvents()
    server = _bare_server(events)
    server._stopping = False
    capfd.readouterr()
    try:
        raise ValueError("I/O operation on closed file.")
    except ValueError:
        server.handle_error(None, ("127.0.0.1", 0))
    assert events.events == [("metrics.publisher_request_failed", {"error": "ValueError"})]
    assert capfd.readouterr().err == ""


def test_a_publisher_without_an_event_sink_still_stays_silent(
    capfd: pytest.CaptureFixture[str],
) -> None:
    server = _bare_server(None)
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

    server = _bare_server(_BrokenEvents())
    capfd.readouterr()
    try:
        raise ValueError("a genuine internal fault")
    except ValueError:
        assert server.handle_error(None, ("127.0.0.1", 0)) is None
    assert capfd.readouterr().err == ""


def test_a_host_that_stops_the_publisher_from_inside_a_handler_gets_no_exception() -> None:
    """An EventSink may react to a fault by taking the endpoint down. That runs on the handler.

    report() is called from the serving thread, so a host that answers metrics.render_failed by
    calling stop() has the publisher joining the very thread it is running on. That raises a bare
    RuntimeError out of a method whose docstring promises never to raise out of a shutdown path,
    and leaves the endpoint half stopped.
    """
    baseline = _thread_baseline()

    class _Unrenderable(OpenMetricsSink):
        def render(self) -> str:
            raise RuntimeError("the aggregator is corrupt")

    class _StopsFromTheHandler:
        def __init__(self) -> None:
            self.publisher: OpenMetricsPublisher | None = None
            self.events: list[str] = []
            self.escaped: BaseException | None = None

        def emit(self, event: str, payload: dict[str, object]) -> None:
            self.events.append(event)
            if event == "metrics.render_failed" and self.publisher is not None:
                try:
                    self.publisher.stop()
                except BaseException as failure:  # noqa: BLE001 - the point is that none escapes
                    self.escaped = failure

    sink = _Unrenderable()
    register_catalog(sink)
    host = _StopsFromTheHandler()
    port = _free_port()
    endpoint = OpenMetricsPublisher(sink, port=port, events=host, shutdown_timeout=1.0)
    host.publisher = endpoint
    endpoint.start()
    try:
        # The in-flight answer may or may not survive: stop() closes the socket the handler was
        # about to write to, which is what the host asked for. What must not happen is an
        # exception leaving stop(), or a publisher left half shut.
        with contextlib.suppress(Exception):
            _fetch(endpoint.url)
        assert _wait_until(lambda: "metrics.render_failed" in host.events)
        assert host.escaped is None, f"stop() raised out of a handler: {host.escaped!r}"
        assert _wait_until(lambda: endpoint.running is False)
        # Whether publisher_stop_incomplete also fires depends on whether another handler was
        # still inside host code at that instant, and either answer is an honest report rather
        # than a defect. Asserting its absence pinned the trigger instead of the safety, and
        # went red one run in five for a publisher behaving correctly.
        assert set(host.events) <= {"metrics.render_failed", "metrics.publisher_stop_incomplete"}
        _assert_port_released(port, "after the host stopped it")
    finally:
        endpoint.stop()
    # The serving thread deregisters as soon as its own request ends.
    assert _wait_until(lambda: endpoint.live_handlers == 0)
    assert_no_thread_left_behind(baseline)


def test_stopping_from_the_accept_loop_returns_instead_of_deadlocking() -> None:
    """The accept loop can be the caller, and shutdown() waits for the accept loop.

    serve_forever runs service_actions on its own thread, and it is the same thread a failed
    handler spawn reports on, so a host EventSink that answers a fault by stopping the endpoint
    is running here. shutdown() blocks until this very loop finishes, so it deadlocks -- and it
    was the first statement, which is why the self-join guard after it could never be reached.
    """
    baseline = _thread_baseline()
    sink = sink_with_catalog()
    endpoint = OpenMetricsPublisher(sink, port=0, shutdown_timeout=1.0)
    endpoint.start()
    server = endpoint._server
    assert server is not None

    called = threading.Event()
    returned = threading.Event()
    escaped: list[BaseException] = []

    def service_actions() -> None:
        if called.is_set():
            return
        called.set()
        try:
            endpoint.stop()
        except BaseException as failure:  # noqa: BLE001 - the point is that none escapes
            escaped.append(failure)
        finally:
            returned.set()

    original = type(server).service_actions
    type(server).service_actions = staticmethod(service_actions)
    try:
        assert returned.wait(timeout=TIMEOUT), "stop() never returned from the accept loop"
        assert escaped == [], f"stop() raised from the accept loop: {escaped!r}"
        assert endpoint.running is False
        # The loop really is finishing, not merely reported as finished.
        assert _wait_until(lambda: not any(
            thread.name == "oktografx-metrics" and thread not in baseline
            for thread in threading.enumerate()
        ), timeout=TIMEOUT)
    finally:
        type(server).service_actions = original
        # Only if the first stop() came back. A stop that never returned still holds the
        # lifecycle lock, so calling it again here would block this test forever instead of
        # failing it -- and a test that hangs reports nothing at all.
        if returned.is_set() and not escaped:
            endpoint.stop()
    assert _wait_until(lambda: endpoint.live_handlers == 0)
    # The teardown runs on a helper thread precisely because the caller could not wait for it;
    # so the leak check waits for that helper rather than reading before it has finished.
    assert _wait_until(lambda: not _threads_left_behind(baseline), timeout=TIMEOUT), (
        f"threads left behind: {_threads_left_behind(baseline)}"
    )


def test_closing_a_server_releases_its_port_while_a_reference_is_still_held() -> None:
    """The release must not depend on the garbage collector noticing the object.

    This is why start() closes the server itself when the serving thread cannot be created.
    Deleting that call leaves the suite green on CPython, because refcounting closes the socket
    the moment the failed start's local goes out of scope -- so the mutation is unobservable
    there rather than harmless. A reference is held here deliberately, which is the only way to
    ask whether the close was explicit or incidental.
    """
    port = _free_port()
    server = _MetricsServer(("127.0.0.1", port), None)
    try:
        taken = socket.socket()
        try:
            with pytest.raises(OSError):
                taken.bind(("127.0.0.1", port))
        finally:
            taken.close()

        server.server_close()

        # The object is still alive and referenced; only the explicit close can have freed this.
        assert server is not None
        _assert_port_released(port)
    finally:
        with contextlib.suppress(Exception):
            server.server_close()


def test_a_publisher_that_cannot_start_its_thread_leaks_nothing() -> None:
    """A thread that cannot be created must not leave a bound listener nobody can close.

    The fields that make a publisher stoppable were assigned before the thread was started, and
    the start was outside the guard that turns a failure into a Grafx error, so the failure left
    a listener bound, running reporting True, and no way to shut it down.
    """
    baseline = _thread_baseline()
    # A fixed port, chosen by binding and releasing one, so the replacement below can ask for
    # the very port the failed start bound. With port=0 the replacement would take a different
    # ephemeral port and pass whether or not the listener was ever released.
    port = _free_port()
    endpoint = OpenMetricsPublisher(sink_with_catalog(), port=port)
    real_start = threading.Thread.start

    def refuse(self: threading.Thread) -> None:
        if self.name == "oktografx-metrics":
            raise RuntimeError("cannot start new thread")
        real_start(self)

    threading.Thread.start = refuse  # type: ignore[method-assign]
    try:
        with pytest.raises(GrafxConfigurationError, match="could not start serving"):
            endpoint.start()
    finally:
        threading.Thread.start = real_start  # type: ignore[method-assign]

    assert endpoint.running is False
    assert_no_thread_left_behind(baseline)
    # The listener really was released: the replacement asks for that exact port.
    replacement = OpenMetricsPublisher(sink_with_catalog(), port=port)
    replacement.start()
    try:
        assert replacement.port == port
        assert _fetch(replacement.url)[0] == 200
    finally:
        replacement.stop()


class _StopsOnEveryEvent:
    """A host that answers every event by stopping the publisher, which is its right to do.

    Nothing about this is exotic. Taking the endpoint down when it reports a fault is the
    obvious thing for a host to do, and the publisher emits from inside its own shutdown, so
    the callback re-enters stop() on the thread that is already inside it.
    """

    def __init__(self) -> None:
        self.publisher: OpenMetricsPublisher | None = None
        self.events: list[str] = []
        self.entered: list[str] = []
        self.returned: list[str] = []

    def emit(self, event: str, payload: dict[str, object]) -> None:
        self.events.append(event)
        self.entered.append(event)
        if self.publisher is not None:
            with contextlib.suppress(Exception):
                self.publisher.stop()
        self.returned.append(event)


def test_a_host_may_stop_the_publisher_from_the_event_the_stop_itself_emits() -> None:
    """A91: no host code is called while the lifecycle lock is held.

    Both halves of this already shipped as passing tests -- a host that stops from a handler,
    and a handler that outlives the shutdown window. Composed, the second emits an event from
    inside stop(), the host answers it by calling stop() again on that same thread, and a
    non-reentrant lock held across the notification never lets it back in.

    The fix is not another guarded path. Three rounds guarded three paths and a fourth arrived
    each time, because what a host callback may do is not a set this component can enumerate.
    The lock is released before anything host-supplied is called at all.
    """
    entered, release = threading.Event(), threading.Event()

    class _Blocking(OpenMetricsSink):
        def render(self) -> str:
            entered.set()
            release.wait(timeout=25.0)
            return super().render()

    sink = _Blocking()
    register_catalog(sink)
    host = _StopsOnEveryEvent()
    endpoint = OpenMetricsPublisher(sink, port=0, events=host, shutdown_timeout=0.5)
    host.publisher = endpoint
    endpoint.start()
    port = endpoint.port

    scrape = _scrape_in_background(endpoint.url)
    try:
        assert entered.wait(timeout=TIMEOUT), "no handler reached render()"

        finished = threading.Event()

        def stop_it() -> None:
            endpoint.stop()
            finished.set()

        threading.Thread(target=stop_it, name="stopper", daemon=True).start()
        assert finished.wait(timeout=TIMEOUT), (
            f"stop() never returned; entered {host.entered}, returned {host.returned}"
        )
        assert host.entered == host.returned, (
            f"{len(host.entered) - len(host.returned)} re-entrant stop() call(s) never returned"
        )
        assert "metrics.publisher_stop_incomplete" in host.events
        # The socket is released even though a handler is still inside render().
        _assert_port_released(port)
    finally:
        release.set()
        scrape.join(timeout=TIMEOUT)


def test_stopping_from_the_accept_loop_releases_the_port_before_it_returns() -> None:
    """MAJOR 3: returning is not stopping if the socket is still bound when it returns.

    The teardown used to be handed to a helper thread, so stop() came back while the listener
    was still open and a host doing what start()'s own error message instructs -- stop it, then
    start it again on the same port -- was refused. It also meant the spawn itself could fail,
    for exactly the condition that puts host code on the accept loop to begin with.
    """
    port = _free_port()
    for attempt in range(3):
        baseline = _thread_baseline()
        endpoint = OpenMetricsPublisher(sink_with_catalog(), port=port, shutdown_timeout=1.0)
        endpoint.start()
        server = endpoint._server
        assert server is not None

        done = threading.Event()
        escaped: list[BaseException] = []

        def service_actions() -> None:
            if done.is_set():
                return
            try:
                endpoint.stop()
            except BaseException as failure:  # noqa: BLE001 - none may escape
                escaped.append(failure)
            finally:
                done.set()

        original = type(server).service_actions
        type(server).service_actions = staticmethod(service_actions)
        try:
            assert done.wait(timeout=TIMEOUT), f"attempt {attempt}: stop() never returned"
            assert escaped == [], f"attempt {attempt}: {escaped!r}"
            assert endpoint.running is False
            # Bound to nothing: the very next attempt takes the same port again.
            _assert_port_released(port, f"on attempt {attempt}")
        finally:
            type(server).service_actions = original
        assert _wait_until(lambda: not _threads_left_behind(baseline), timeout=TIMEOUT), (
            f"attempt {attempt}: {_threads_left_behind(baseline)}"
        )


@pytest.mark.parametrize("trial", range(3), ids=["first", "second", "third"])
def test_a_stop_survives_the_accept_loop_being_the_blocked_party(trial: int) -> None:
    """The accept loop runs host code, and a teardown waits for the accept loop.

    Every earlier guard here covered the accept loop as the thread doing the stopping. This is
    the other direction and it is the one that deadlocks: a stopper takes the lifecycle lock and
    calls shutdown(), which waits for the loop to finish; the loop is inside handle_error ->
    report() -> the host's emit(); and the host answers by touching the lifecycle. The lock is
    held by a thread waiting for the thread that wants the lock.

    L2 is the rule this encodes: while holding a lock, do not call foreign code AND do not block
    on anything that can be waiting on foreign code. _tear_down calls no host code -- its
    docstring is literally true -- and that was not enough, because shutdown() waits for a thread
    that does.
    """
    on_loop = threading.Event()
    stopper_in_flight = threading.Event()
    loop_stop_entered = threading.Event()
    loop_stop_returned = threading.Event()

    class _TouchesTheLifecycleFromTheLoop:
        def __init__(self) -> None:
            self.publisher: OpenMetricsPublisher | None = None

        def emit(self, event: str, payload: dict[str, object]) -> None:
            on_loop.set()
            stopper_in_flight.wait(timeout=3.0)
            loop_stop_entered.set()
            if self.publisher is not None:
                with contextlib.suppress(Exception):
                    self.publisher.stop()
            loop_stop_returned.set()

    port = _free_port()
    host = _TouchesTheLifecycleFromTheLoop()
    endpoint = OpenMetricsPublisher(
        sink_with_catalog(), port=port, events=host, shutdown_timeout=2.0
    )
    host.publisher = endpoint
    endpoint.start()
    server = endpoint._server
    assert server is not None

    fired = threading.Event()

    def service_actions() -> None:
        if fired.is_set():
            return
        fired.set()
        # Exactly what handle_error does on this thread: hand an event to the host.
        server.report("metrics.render_failed", "probe")

    original = type(server).service_actions
    type(server).service_actions = staticmethod(service_actions)
    outer_returned = threading.Event()
    try:
        assert on_loop.wait(timeout=TIMEOUT), "host code never ran on the accept loop"

        def stop_it() -> None:
            endpoint.stop()
            outer_returned.set()

        threading.Thread(target=stop_it, name="host-stopper", daemon=True).start()
        _pause(0.15)
        stopper_in_flight.set()

        assert loop_stop_returned.wait(timeout=TIMEOUT), (
            "the accept loop never came back from the stop() its host called"
        )
        assert outer_returned.wait(timeout=TIMEOUT), "the stopping thread never returned"

        # The consequence that made this permanent rather than momentary: the port.
        _assert_port_released(port, "by the deadlocked stop")
        assert endpoint.running is False
    finally:
        type(server).service_actions = original
        stopper_in_flight.set()


def test_a_handler_that_stops_while_another_thread_owns_the_teardown_returns_at_once() -> None:
    """The accept loop is not the only thread a teardown waits for; handlers are too.

    ``await_handlers`` joins every serving thread except the one calling it, so a handler that
    finds a teardown already under way must not queue behind it: the owner is waiting for this
    very thread. The accept loop is covered by the identity check one line above; this is the
    branch that asks the server whether the caller is one of its handlers, and without it the
    two sides wait for each other until the shutdown budget runs out.
    """
    entered = threading.Event()
    may_stop = threading.Event()
    handler_stop_seconds: list[float] = []

    class _StopsFromInsideRender(OpenMetricsSink):
        def render(self) -> str:
            entered.set()
            may_stop.wait(timeout=20.0)
            started = time.monotonic()
            with contextlib.suppress(Exception):
                publisher.stop()
            handler_stop_seconds.append(time.monotonic() - started)
            return super().render()

    sink = _StopsFromInsideRender()
    register_catalog(sink)
    budget = 3.0
    publisher = OpenMetricsPublisher(sink, port=0, shutdown_timeout=budget)
    publisher.start()
    scrape = _scrape_in_background(publisher.url)
    owner_returned = threading.Event()
    try:
        assert entered.wait(timeout=TIMEOUT), "no handler reached render()"

        def own_the_teardown() -> None:
            publisher.stop()
            owner_returned.set()

        threading.Thread(target=own_the_teardown, name="owner", daemon=True).start()
        # Let the owner reach await_handlers, which is where it starts waiting for this handler.
        _pause(0.2)
        may_stop.set()

        assert _wait_until(lambda: bool(handler_stop_seconds), timeout=TIMEOUT), (
            "the handler never came back from its own stop()"
        )
        waited = handler_stop_seconds[0]
        assert waited < budget / 2, (
            f"the handler queued behind the teardown for {waited:.2f}s while the teardown was "
            f"waiting for the handler; a serving thread must never wait for a stop it does not own"
        )
        assert owner_returned.wait(timeout=TIMEOUT), "the owning stop() never returned"
    finally:
        may_stop.set()
        scrape.join(timeout=TIMEOUT)
        publisher.stop()


def test_running_stays_true_until_the_socket_is_actually_released() -> None:
    """P7: a publisher that still holds its port has not stopped.

    running flipping first gave a host a window in which it was told the endpoint was down and
    the port free, while the listener was still bound -- and the instruction in start()'s own
    error message is to stop it and start it again.
    """
    entered, release = threading.Event(), threading.Event()

    class _Blocking(OpenMetricsSink):
        def render(self) -> str:
            entered.set()
            release.wait(timeout=25.0)
            return super().render()

    sink = _Blocking()
    register_catalog(sink)
    port = _free_port()
    endpoint = OpenMetricsPublisher(sink, port=port, shutdown_timeout=3.0)
    endpoint.start()
    scrape = _scrape_in_background(endpoint.url)
    observations: list[tuple[bool, bool]] = []
    try:
        assert entered.wait(timeout=TIMEOUT)

        watching = threading.Event()

        def watch() -> None:
            # Sample the pair (running, port-is-free) until the stop completes. running must
            # never read False while the port is still taken.
            while not watching.is_set():
                running = endpoint.running
                observations.append((running, _port_is_free(port)))

        watcher = threading.Thread(target=watch, name="watcher", daemon=True)
        watcher.start()
        endpoint.stop()
        watching.set()
        watcher.join(timeout=TIMEOUT)
    finally:
        release.set()
        scrape.join(timeout=TIMEOUT)

    assert observations, "the watcher never sampled anything"
    liars = [(running, free) for running, free in observations if not running and not free]
    assert liars == [], (
        f"{len(liars)} of {len(observations)} samples reported the publisher stopped while its "
        f"port was still bound"
    )


def test_the_accept_loop_ends_on_the_stopping_flag_alone() -> None:
    """The flag is the mechanism; a closed socket ending the loop is only a side effect.

    Both families also end the loop when the listener is closed under it, because the select
    that follows raises. Relying on that is what broke on POSIX in the first place, and it also
    masks the flag: every ordinary stop closes the socket too, so a loop that ignored the flag
    entirely would still finish and no test would notice. Here nothing is closed at all, so the
    flag is the only thing that can end it.
    """
    endpoint = OpenMetricsPublisher(sink_with_catalog(), port=0, shutdown_timeout=3.0)
    endpoint.start()
    server = endpoint._server
    assert server is not None
    thread = endpoint._thread
    assert thread is not None
    try:
        assert not server._loop_exited.is_set(), "the loop was not running to begin with"

        server.begin_stopping()          # the flag, and nothing else

        assert server._loop_exited.wait(timeout=TIMEOUT), (
            "the accept loop did not end when it was told to; only closing its socket would"
        )
        assert _wait_until(lambda: not thread.is_alive(), timeout=TIMEOUT)
        # And the socket is still open: the loop ended without anything being torn down.
        assert server.socket.fileno() != -1
    finally:
        endpoint.stop()


def test_the_wait_for_the_accept_loop_is_bounded() -> None:
    """shutdown() must not wait forever for a loop that never finishes.

    The standard library's waits without a limit, which was the one wait in the teardown that
    the publisher's shutdown budget did not cover -- measured at 3.68 s against a 0.5 s setting.
    A loop that has finished makes any bound look correct, so the loop here is never started.
    """
    port = _free_port()
    server = _MetricsServer(("127.0.0.1", port), None)
    try:
        server._loop_exited.clear()      # as if a loop were running and never finishing
        started = time.monotonic()
        server.shutdown(timeout=0.05)
        waited = time.monotonic() - started
        assert waited < 1.0, f"shutdown() waited {waited:.2f}s for a loop that never ends"
        assert server._stopping is True
    finally:
        server._loop_exited.set()
        with contextlib.suppress(Exception):
            server.server_close()


def test_stopping_from_the_accept_loop_reports_no_failure_of_its_own() -> None:
    """Closing the listener under its own loop ends that loop; it must end it quietly.

    The select that follows the close fails, and an unhandled failure on a thread is delivered
    to threading.excepthook, whose default prints a traceback into the standard error of the
    host -- the same stream an earlier round spent itself keeping clean for aborted scrapes.
    The hook is what this asserts on rather than the bytes: it is the point the failure is
    delivered to, so the observation does not depend on which stream is installed or when.
    """
    port = _free_port()
    endpoint = OpenMetricsPublisher(sink_with_catalog(), port=port, shutdown_timeout=1.0)
    endpoint.start()
    server = endpoint._server
    assert server is not None

    reported: list[str] = []
    original_hook = threading.excepthook
    threading.excepthook = lambda args: reported.append(args.exc_type.__name__)

    done = threading.Event()

    def service_actions() -> None:
        if done.is_set():
            return
        with contextlib.suppress(Exception):
            endpoint.stop()
        done.set()

    original = type(server).service_actions
    type(server).service_actions = staticmethod(service_actions)
    try:
        assert done.wait(timeout=TIMEOUT)
        # The loop needs a moment to notice the closed socket and unwind.
        _pause(0.3)
        assert reported == [], (
            f"the accept loop reported its own shutdown as a failure: {reported}"
        )
    finally:
        type(server).service_actions = original
        threading.excepthook = original_hook


def test_stopping_from_a_handler_still_releases_the_port() -> None:
    class _Unrenderable(OpenMetricsSink):
        def render(self) -> str:
            raise RuntimeError("the aggregator is corrupt")

    class _StopsFromTheHandler:
        def __init__(self) -> None:
            self.publisher: OpenMetricsPublisher | None = None

        def emit(self, event: str, payload: dict[str, object]) -> None:
            if event == "metrics.render_failed" and self.publisher is not None:
                with contextlib.suppress(Exception):
                    self.publisher.stop()

    sink = _Unrenderable()
    register_catalog(sink)
    host = _StopsFromTheHandler()
    endpoint = OpenMetricsPublisher(sink, port=0, events=host, shutdown_timeout=1.0)
    host.publisher = endpoint
    port = endpoint.start()
    try:
        with contextlib.suppress(Exception):
            _fetch(endpoint.url)
    finally:
        endpoint.stop()
    # The socket really was released: a fresh publisher can take the same port.
    assert _wait_until(lambda: endpoint.live_handlers == 0)
    replacement = OpenMetricsPublisher(sink_with_catalog(), port=port)
    replacement.start()
    try:
        assert _fetch(replacement.url)[0] == 200
    finally:
        replacement.stop()


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


# --- lifecycle: stopping means stopping -------------------------------------------------------


def _port_is_free(port: int) -> bool:
    """Return True when a publisher could bind this port, using the options the product uses."""
    probe = socket.socket()
    try:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            probe.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        else:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _assert_port_released(port: int, context: str = "") -> None:
    """Fail unless a publisher could take this port again.

    Binding the way the product binds, not the way a bare socket does. A30 gives the listener
    SO_EXCLUSIVEADDRUSE on Windows and SO_REUSEADDR on POSIX because the two families need
    opposite options for the same guarantee, and a probe that ignores that measures the platform
    instead of the release: on POSIX a connection a parked handler still holds keeps the port out
    of reach of a bare bind, while the publisher itself takes it without trouble -- verified.
    """
    probe = socket.socket()
    try:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            probe.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        else:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))
    except OSError as failure:
        pytest.fail(f"the port was not released{(' ' + context) if context else ''}: {failure}")
    finally:
        probe.close()


def _free_port() -> int:
    """Return a port nothing is listening on, so a test can ask for that exact one."""
    finder = socket.socket()
    try:
        finder.bind(("127.0.0.1", 0))
        return int(finder.getsockname()[1])
    finally:
        finder.close()


def _thread_baseline() -> set[threading.Thread]:
    """Return the threads alive right now, held by reference.

    Keeping the objects rather than their id() matters: a collected thread frees its id for
    reuse, and a leaked thread that happened to land on a recycled id would read as part of the
    baseline. Holding the reference makes reuse impossible for as long as the check runs.
    """
    return set(threading.enumerate())


def _threads_left_behind(baseline: set[threading.Thread]) -> list[str]:
    """Return the names of the threads that appeared after the baseline and are still alive."""
    return sorted(
        thread.name
        for thread in threading.enumerate()
        if thread not in baseline and thread.is_alive()
    )


def assert_no_thread_left_behind(baseline: set[threading.Thread]) -> None:
    """Fail when any thread created after the baseline is still running."""
    leaked = _threads_left_behind(baseline)
    assert leaked == [], f"threads left behind: {leaked}"


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 5.0) -> bool:
    """Poll a predicate under a bound; no single wait is longer than a tenth of the limit."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


CRLF: bytes = b"\r\n"
"""The line ending of the protocol, named so no test has to escape it inline."""

BARE_LF: bytes = b"\n"
"""The line ending a lenient client writes, which the reader also has to recognise."""

HALF_SENT_REQUEST: bytes = b"GET /metrics HTTP/1.1"
"""A request line with no line ending and no blank line: the client stopped halfway.

This is the state the shipped lifecycle tests never produced. A silent connection parks a thread
before the first read; a half-sent one parks it inside the header read, which is a different
piece of code and, on Windows, was the one that could not be interrupted.
"""


_PARK_WAIT_SECONDS: float = 30.0
"""How long a test waits for N connections to reach a serving thread on a loaded machine."""

PARKED_CONNECTION_DEADLINE: float = 120.0
"""The idle deadline of a publisher whose parked connections a test intends to count.

The default deadline is 10 s, which is right for the product and wrong for this measurement:
200 serving threads may take longer than that to all exist under load, and the ones that
arrived first are shed while the rest are still starting, so the population never reaches N and
has vanished by the time the wait gives up. Diagnosing that is the trap A78 names -- "too slow
to reach the state" and "the state expired while waiting" read identically in the failure and
have opposite remedies. Raising the deadline for these tests is the remedy; the deadline itself
stays pinned by test_a_silent_connection_dies_on_its_own_deadline, which measures it directly.
"""


def _park_connections(
    publisher: OpenMetricsPublisher, count: int, *, state: str
) -> list[socket.socket]:
    """Open connections that park a serving thread, either before or during the header read."""
    clients = [
        socket.create_connection((publisher.host, publisher.port), timeout=TIMEOUT)
        for _ in range(count)
    ]
    if state == "half-sent":
        for client in clients:
            client.sendall(HALF_SENT_REQUEST)
    server = publisher._server
    assert server is not None
    # A78: a generous wait is only safe when the state it waits for is stable. A parked
    # connection is shed by its own idle deadline, so waiting past that deadline destroys the
    # very population being waited for -- the count climbs, the deadline fires, and the
    # assertion reads zero. The publisher these tests build therefore carries a deadline longer
    # than the wait (see PARKED_CONNECTION_DEADLINE), and the wait is bounded well inside it.
    assert publisher._connection_timeout >= _PARK_WAIT_SECONDS * 2, (
        f"the connections would be shed at {publisher._connection_timeout}s, before the "
        f"{_PARK_WAIT_SECONDS}s wait for them to appear could finish"
    )
    _wait_until(lambda: server.live_handlers >= count, timeout=_PARK_WAIT_SECONDS)
    assert server.live_handlers >= 1, "no connection reached a serving thread at all"
    return clients


def _open_silent_connections(publisher: OpenMetricsPublisher, count: int) -> list[socket.socket]:
    """Open connections that never send a byte, which is what parks a serving thread."""
    return _park_connections(publisher, count, state="silent")


def test_a_connection_opened_before_stop_is_not_served_after_stop(
    recording_sink: OpenMetricsSink,
) -> None:
    # Breaking the accept loop is not stopping: a client can park a connection and harvest live
    # metric state at any later time, from a host that switched its metrics surface off.
    endpoint = OpenMetricsPublisher(recording_sink, port=0)
    endpoint.start()
    early = socket.create_connection((endpoint.host, endpoint.port), timeout=TIMEOUT)
    try:
        assert _wait_until(lambda: endpoint.live_handlers >= 1)
        endpoint.stop()
        assert endpoint.running is False

        served = b""
        with contextlib.suppress(OSError):
            early.sendall(b"GET /metrics HTTP/1.1" + CRLF + b"Host: localhost" + CRLF + CRLF)
            served = _drain(early).encode("utf-8", "replace")
        assert b"oktografx_" not in served, f"a stopped publisher served {len(served)} bytes"
    finally:
        with contextlib.suppress(OSError):
            early.close()


def test_stop_closes_every_connection_it_accepted(recording_sink: OpenMetricsSink) -> None:
    endpoint = OpenMetricsPublisher(
        recording_sink, port=0, connection_timeout=PARKED_CONNECTION_DEADLINE
    )
    endpoint.start()
    server = endpoint._server
    assert server is not None
    clients = _open_silent_connections(endpoint, 4)
    try:
        assert server.live_connections == 4
        endpoint.stop()
        assert server.live_connections == 0
        assert server.live_handlers == 0
    finally:
        for client in clients:
            with contextlib.suppress(OSError):
                client.close()


@pytest.mark.parametrize("count", [1, 40, 200], ids=["one", "forty", "two-hundred"])
@pytest.mark.parametrize("state", ["silent", "half-sent"], ids=["silent", "half-sent"])
def test_stop_reclaims_every_parked_connection(
    recording_sink: OpenMetricsSink, state: str, count: int
) -> None:
    # The connections stay open on purpose: the host asked the publisher to stop, and the
    # publisher does not get to wait for a client that may never come back.
    #
    # Every assertion below reads the registry of the real server, captured before stop(). A
    # count taken from the publisher after it cleared its own field would answer zero while the
    # threads were still running, which is the shape of a guard that cannot fail.
    baseline = _thread_baseline()
    endpoint = OpenMetricsPublisher(
        recording_sink, port=0, connection_timeout=PARKED_CONNECTION_DEADLINE
    )
    endpoint.start()
    server = endpoint._server
    assert server is not None
    clients = _park_connections(endpoint, count, state=state)
    try:
        parked = server.live_handlers
        assert parked >= 1
        assert _threads_left_behind(baseline)

        started = time.monotonic()
        endpoint.stop()
        elapsed = time.monotonic() - started

        assert server.live_handlers == 0, (
            f"{server.live_handlers} of {parked} serving threads outlived stop"
        )
        assert server.live_connections == 0
        assert elapsed < DEFAULT_SHUTDOWN_TIMEOUT_SECONDS, (
            f"stop() spent {elapsed:.3f}s, which is its whole shutdown budget"
        )
        assert_no_thread_left_behind(baseline)
    finally:
        for client in clients:
            with contextlib.suppress(OSError):
                client.close()


class _BlockingSink(OpenMetricsSink):
    """A sink whose render() parks inside the handler until a test releases it.

    This is the only way to produce a handler that outlives the shutdown window, and that state
    is what two pieces of the shutdown path exist for. Without it they are unobservable, which
    is exactly how a mutation of either one survives a whole test suite.
    """

    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        super().__init__()
        self._entered = entered
        self._release = release

    def render(self) -> str:
        self._entered.set()
        self._release.wait(timeout=30.0)
        return super().render()


def _blocking_publisher(
    entered: threading.Event, release: threading.Event, events: _RecordingEvents
) -> OpenMetricsPublisher:
    sink = _BlockingSink(entered, release)
    register_catalog(sink)
    sink.increment("oktografx_database_opens_total")
    return OpenMetricsPublisher(sink, port=0, shutdown_timeout=0.5, events=events)


def _scrape_in_background(url: str) -> threading.Thread:
    def run() -> None:
        with contextlib.suppress(Exception):
            _fetch(url)

    thread = threading.Thread(target=run, name="scrape", daemon=True)
    thread.start()
    return thread


def test_a_handler_that_outlives_the_shutdown_window_is_reported_by_the_publisher() -> None:
    # Reads the publisher's own property after stop(), which every other shutdown test reaches
    # around by keeping a local reference to the server. Clearing the field in stop() would make
    # this answer zero while a thread is demonstrably still running.
    entered, release = threading.Event(), threading.Event()
    events = _RecordingEvents()
    endpoint = _blocking_publisher(entered, release, events)
    endpoint.start()
    scrape = _scrape_in_background(endpoint.url)
    try:
        assert entered.wait(timeout=TIMEOUT), "the handler never reached render()"
        endpoint.stop()

        assert endpoint.live_handlers == 1, (
            "the publisher reported no serving thread while one was still inside render()"
        )
        assert endpoint.running is False
        assert (
            "metrics.publisher_stop_incomplete",
            {"error": "handlers_still_running"},
        ) in events.events
    finally:
        release.set()
        scrape.join(timeout=TIMEOUT)
    assert _wait_until(lambda: endpoint.live_handlers == 0)


def test_stop_closes_the_connection_of_a_handler_it_could_not_join() -> None:
    # close_live_connections() only ever matters for a handler that outlives the window: for any
    # other one the connection is closed by the ordinary shutdown_request path, which is why
    # emptying the method passed a whole suite.
    entered, release = threading.Event(), threading.Event()
    events = _RecordingEvents()
    endpoint = _blocking_publisher(entered, release, events)
    endpoint.start()
    client = socket.create_connection((endpoint.host, endpoint.port), timeout=TIMEOUT)
    try:
        client.sendall(b"GET /metrics HTTP/1.1" + CRLF + b"Host: localhost" + CRLF + CRLF)
        assert entered.wait(timeout=TIMEOUT), "the handler never reached render()"

        endpoint.stop()
        assert endpoint.live_handlers == 1, "the handler was expected to still be blocked"

        client.settimeout(2.0)
        try:
            remaining = client.recv(65536)
        except ConnectionError:
            remaining = b""
        except TimeoutError:
            pytest.fail("stop() left the connection of an unjoinable handler open")
        assert remaining == b"", "a stopped publisher answered from a handler it could not join"
    finally:
        release.set()
        with contextlib.suppress(OSError):
            client.close()


def test_a_parked_handler_notices_the_stopping_flag_on_its_own() -> None:
    """A parked handler sheds itself when the server starts stopping, before any socket closes.

    Two mechanisms end a parked connection and they overlap: stop() closes the socket, and the
    read loop watches the stopping flag. Overlapping is fine; being untestable is not. Raising
    the flag alone, with a deadline far too long to be the cause and no socket touched, is the
    one state that isolates the second mechanism from the first.
    """
    sink = OpenMetricsSink()
    register_catalog(sink)
    endpoint = OpenMetricsPublisher(sink, port=0, connection_timeout=30.0)
    endpoint.start()
    server = endpoint._server
    assert server is not None
    client = socket.create_connection((endpoint.host, endpoint.port), timeout=TIMEOUT)
    try:
        assert _wait_until(lambda: server.live_handlers >= 1)
        assert server.live_connections == 1

        server.begin_stopping()
        assert _wait_until(lambda: server.live_handlers == 0, timeout=2.0), (
            "a parked handler ignored the stopping flag and waited for its idle deadline"
        )
        # Nothing closed the socket: the handler left of its own accord.
        assert client.fileno() != -1
    finally:
        with contextlib.suppress(OSError):
            client.close()
        endpoint.stop()


def test_a_request_completed_during_shutdown_is_not_served(
    recording_sink: OpenMetricsSink,
) -> None:
    # The stopping state is read before the request arrives and again before it is answered, so
    # a head whose last bytes land during shutdown is not turned into live metric state.
    endpoint = OpenMetricsPublisher(recording_sink, port=0)
    endpoint.start()
    server = endpoint._server
    assert server is not None
    client = socket.create_connection((endpoint.host, endpoint.port), timeout=TIMEOUT)
    try:
        client.sendall(HALF_SENT_REQUEST)
        assert _wait_until(lambda: server.live_handlers >= 1)

        server.begin_stopping()
        with contextlib.suppress(OSError):
            client.sendall(CRLF + b"Host: localhost" + CRLF + CRLF)

        answer = _drain(client).encode("utf-8", "replace")
        assert b"oktografx_" not in answer, (
            f"a stopping publisher served live metric state: {len(answer)} bytes, "
            f"first line {answer.splitlines()[0][:80] if answer else b''!r}"
        )
    finally:
        with contextlib.suppress(OSError):
            client.close()
        endpoint.stop()


def test_a_silent_connection_dies_on_its_own_deadline(recording_sink: OpenMetricsSink) -> None:
    # Even with nobody stopping the publisher, an idle connection may not pin a thread and a
    # socket for as long as the host process lives.
    baseline = _thread_baseline()
    endpoint = OpenMetricsPublisher(recording_sink, port=0, connection_timeout=0.2)
    endpoint.start()
    client = socket.create_connection((endpoint.host, endpoint.port), timeout=TIMEOUT)
    try:
        assert _wait_until(lambda: endpoint.live_handlers >= 1)
        assert _wait_until(lambda: endpoint.live_handlers == 0), "the deadline never fired"
        assert endpoint.live_connections == 0
        # The endpoint is still healthy for a client that does talk.
        assert _fetch(endpoint.url)[0] == 200
    finally:
        with contextlib.suppress(OSError):
            client.close()
        endpoint.stop()
    assert_no_thread_left_behind(baseline)


def test_the_publisher_leaves_no_thread_behind(recording_sink: OpenMetricsSink) -> None:
    baseline = _thread_baseline()
    before = set(threading.enumerate())
    endpoint = OpenMetricsPublisher(recording_sink, port=0)
    endpoint.start()
    # The serve loop of THIS publisher, not whichever thread of that name happens to come first:
    # a publisher another test is still shutting down would otherwise be the one under assertion.
    serve_loop = next(
        thread
        for thread in threading.enumerate()
        if thread.name == "oktografx-metrics" and thread not in before
    )
    _fetch(endpoint.url)
    endpoint.stop()
    assert_no_thread_left_behind(baseline)
    assert serve_loop.is_alive() is False
    assert endpoint.live_handlers == 0


def test_the_leak_check_fails_on_a_leaked_thread() -> None:
    # The previous version of this guard asserted `before == after or before <= after`, which is
    # just `before <= after`: the leaked-thread condition itself. A guard that cannot fail on the
    # thing it guards is worse than no guard, so this proves the current one can.
    baseline = _thread_baseline()
    release = threading.Event()
    leaked = threading.Thread(
        target=release.wait, name="oktografx-metrics-leaked", daemon=True
    )
    leaked.start()
    try:
        assert _wait_until(leaked.is_alive)
        with pytest.raises(AssertionError, match="threads left behind"):
            assert_no_thread_left_behind(baseline)
    finally:
        release.set()
        leaked.join(timeout=TIMEOUT)
    assert_no_thread_left_behind(baseline)


# --- a broken sink is a distinguishable condition ------------------------------------------------


class _UnrenderableSink(OpenMetricsSink):
    """A recording sink whose render() fails, which is not the same as having no sink at all."""

    def render(self) -> str:
        raise RuntimeError("the aggregator is corrupt")


def test_a_sink_that_cannot_be_rendered_says_so_and_tells_the_event_sink(
    capfd: pytest.CaptureFixture[str],
) -> None:
    # Answering "the publisher holds no recording sink" while holding one tells the operator
    # something false, and the fault reached nobody at all.
    events = _RecordingEvents()
    sink = _UnrenderableSink()
    register_catalog(sink)
    endpoint = OpenMetricsPublisher(sink, port=0, events=events)
    endpoint.start()
    capfd.readouterr()
    try:
        status, content_type, body = _fetch(endpoint.url)
    finally:
        endpoint.stop()

    assert status == 503
    assert content_type.startswith("text/plain")
    assert "could not be rendered" in body
    assert "holds no recording sink" not in body
    assert events.events == [("metrics.render_failed", {"error": "RuntimeError"})]
    assert capfd.readouterr().err == ""


def test_the_two_five_hundred_and_three_reasons_are_distinguishable() -> None:
    empty = OpenMetricsPublisher(None, port=0)
    empty.start()
    broken_sink = _UnrenderableSink()
    register_catalog(broken_sink)
    broken = OpenMetricsPublisher(broken_sink, port=0)
    broken.start()
    try:
        _, _, without = _fetch(empty.url)
        _, _, unrenderable = _fetch(broken.url)
    finally:
        empty.stop()
        broken.stop()
    assert without != unrenderable
    assert "holds no recording sink" in without
    assert "could not be rendered" in unrenderable


def test_a_sink_whose_enabled_flag_explodes_is_reported_too() -> None:
    class _HostileSink(OpenMetricsSink):
        @property
        def enabled(self) -> bool:
            raise RuntimeError("even the flag is broken")

    events = _RecordingEvents()
    endpoint = OpenMetricsPublisher(_HostileSink(), port=0, events=events)
    endpoint.start()
    try:
        status, _, body = _fetch(endpoint.url)
    finally:
        endpoint.stop()
    assert status == 503
    assert "could not be rendered" in body
    assert events.events == [("metrics.render_failed", {"error": "RuntimeError"})]


# --- protocol edges answer in the same voice as everything else ----------------------------------




def _pause(seconds: float) -> None:
    """Wait, in slices no longer than the limit a single sleep may take in this suite."""
    remaining = seconds
    while remaining > 0.0:
        slice_seconds = min(remaining, 0.05)
        time.sleep(slice_seconds)
        remaining -= slice_seconds


def _drain(client: socket.socket) -> str:
    """Read one whole answer from an already-open connection, reset or not.

    A reset ends the read the same way an orderly close does: with whatever already arrived.
    The endpoint refuses an over-long head and closes while the client is still writing, and
    Windows answers a close-with-unread-bytes by sending RST, so the read raises rather than
    returning the response that is already in the buffer. Treating that as a failure of the
    test rather than as the end of the answer is what made this suite intermittently red.
    """
    client.settimeout(TIMEOUT)
    chunks: list[bytes] = []
    try:
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    except OSError:
        # One clause, because ConnectionError and TimeoutError are both OSError subclasses and
        # Windows reports a reset through several of them: 10053 and 10054 arrive already mapped
        # to the ConnectionError family, while 10038 on a socket closed under us does not. The
        # bytes already read are the answer the server sent, whichever spelling ends the read.
        pass
    return b"".join(chunks).decode("utf-8", "replace")


def _exchange(publisher: OpenMetricsPublisher, request: bytes, *, chunk: int = 8192) -> str:
    """Send a request in pieces, stopping as soon as the endpoint has answered.

    A request the server refuses part-way through is answered while the client is still
    writing. Continuing to write into a socket the server has finished with is what leaves
    unread bytes queued, and unread bytes are what turn the close into a reset that discards
    the answer. So this stops writing the moment the connection becomes readable.
    """
    client = socket.create_connection((publisher.host, publisher.port), timeout=TIMEOUT)
    try:
        with contextlib.suppress(OSError):
            with selectors.DefaultSelector() as selector:
                selector.register(client, selectors.EVENT_READ)
                for offset in range(0, len(request), chunk):
                    if selector.select(timeout=0):
                        break
                    client.sendall(request[offset : offset + chunk])
                else:
                    # Everything was sent: half-close so the peer sees a clean end of request.
                    with contextlib.suppress(OSError):
                        client.shutdown(socket.SHUT_WR)
        return _drain(client)
    finally:
        with contextlib.suppress(OSError):
            client.close()


def _raw(publisher: OpenMetricsPublisher, request: bytes) -> str:
    """Send raw bytes and return the whole answer as text.

    The send is allowed to fail. A request the server refuses early -- an over-long head is the
    case that matters -- is answered and closed while the client is still writing, so the reset
    that arrives mid-send is the refusal working, not a failure to make it. Whether the request
    reached the server at all is settled by the answer, which every caller asserts on.
    """
    client = socket.create_connection((publisher.host, publisher.port), timeout=TIMEOUT)
    try:
        with contextlib.suppress(OSError):
            client.sendall(request)
        return _drain(client)
    finally:
        with contextlib.suppress(OSError):
            client.close()


def assert_gaps_stayed_inside(realised: list[float], deadline: float) -> None:
    """Fail with a diagnosis when a scheduling stall reached the deadline being measured against.

    The realised gap is not the nominal one: under load it was measured at more than three times
    its budget. When a gap does reach the deadline the endpoint sheds the connection -- correctly
    -- and the measurement simply could not be made. Saying that is worth a great deal more than
    an unhandled socket error, which is what the test used to produce.
    """
    worst = max(realised) if realised else 0.0
    assert worst < deadline, (
        f"a scheduling stall of {worst:.2f}s reached the {deadline}s deadline, so the endpoint "
        f"shed a client that had stopped sending for longer than the deadline allows; the "
        f"measurement could not be made rather than the property being wrong"
    )


def test_the_slow_client_premise_guard_reports_a_stall_rather_than_a_socket_error() -> None:
    # The guard above only fires on a loaded machine, so it is exercised directly here: a run
    # whose gaps did cross the deadline must say so, and one whose gaps did not must pass.
    assert_gaps_stayed_inside([0.05, 0.06, 0.04], 3.0)
    with pytest.raises(AssertionError, match="scheduling stall of 3.20s"):
        assert_gaps_stayed_inside([0.05, 3.2, 0.04], 3.0)
    with pytest.raises(AssertionError, match="could not be made"):
        assert_gaps_stayed_inside([3.0], 3.0)
    assert_gaps_stayed_inside([], 3.0)


def test_a_slow_but_progressing_client_is_served(recording_sink: OpenMetricsSink) -> None:
    """The connection deadline is an idle deadline, not a total one.

    Two numbers have to hold at once and they pull in opposite directions: no single gap may
    reach the deadline, or the endpoint sheds the connection exactly as it should and the test
    goes red for the behaviour working; and the total must exceed the deadline, or a deadline
    that never resets would not fire either and the test would pass on a broken implementation.

    A78 from the sender's side: a nominal gap is a budget, and the realised gap is what actually
    happens. Under load the realised gap was measured at more than three times its nominal
    value, so the margin here is deliberately large -- sixty nominal gaps to one deadline -- and
    the realised gaps are recorded and asserted afterwards, so a scheduling stall that did cross
    the deadline says exactly that instead of surfacing as an unhandled socket error.
    """
    gap = 0.05
    deadline = 3.0
    # A padded query string, which the path match ignores, so the head is long enough to be sent
    # in enough pieces for the total to outlast the deadline without any single gap coming near.
    head = (
        b"GET /metrics?pad=" + b"p" * 180 + b" HTTP/1.1" + CRLF + b"Host: localhost" + CRLF + CRLF
    )
    pieces = [head[index : index + 2] for index in range(0, len(head), 2)]
    assert gap * 20 <= deadline, "one gap must be far short of the deadline"
    assert len(pieces) * gap > deadline * 1.4, "the total must clearly outlast the deadline"

    endpoint = OpenMetricsPublisher(recording_sink, port=0, connection_timeout=deadline)
    endpoint.start()
    client = socket.create_connection((endpoint.host, endpoint.port), timeout=TIMEOUT)
    realised: list[float] = []
    send_failure: OSError | None = None
    try:
        started = time.monotonic()
        for piece in pieces:
            before = time.monotonic()
            _pause(gap)
            realised.append(time.monotonic() - before)
            try:
                client.sendall(piece)
            except OSError as failure:
                # The endpoint shed the connection. Whether that was correct is decided below,
                # by the realised gaps, not by letting a socket error end the test.
                send_failure = failure
                break
        answer = _drain(client)
        elapsed = time.monotonic() - started
    finally:
        with contextlib.suppress(OSError):
            client.close()
        endpoint.stop()

    worst = max(realised) if realised else 0.0
    assert_gaps_stayed_inside(realised, deadline)
    assert send_failure is None, (
        f"the connection was closed mid-send after {len(realised)} gaps, worst {worst:.2f}s, "
        f"while every gap was inside the {deadline}s deadline: {send_failure!r}"
    )
    assert elapsed > deadline, (
        f"the client finished in {elapsed:.2f}s without outlasting the {deadline}s deadline, so "
        f"a deadline that never reset would have passed this test too"
    )
    assert " 200 " in answer.splitlines()[0], f"a client that kept sending was dropped: {answer!r}"
    assert "oktografx_ledger_depth" in answer


VERSION_TOKENS: tuple[tuple[str, bytes], ...] = (
    ("explicit 0.9", b" HTTP/0.9"),
    ("no version at all", b""),
    ("1.0", b" HTTP/1.0"),
)
"""Request line tails whose framing the endpoint must not vary.

``default_request_version = "HTTP/1.0"`` keeps a version-less request line framed, and that has
a test. Its sibling is the explicit ``HTTP/0.9`` token, which the parser accepts and which then
makes the standard library discard the status line and every header while still writing the
body -- an answer with no status, no content type and no length. The API contract of SPEC-M1
freezes a framed answer, so both spellings are pinned here rather than only the one.
"""


@pytest.mark.parametrize(
    ("label", "version"), VERSION_TOKENS, ids=[row[0] for row in VERSION_TOKENS]
)
@pytest.mark.parametrize(
    ("path_label", "path", "status"),
    [
        ("metrics", b"/metrics", b" 200 "),
        ("unknown path", b"/nope", b" 404 "),
    ],
    ids=["metrics", "unknown-path"],
)
def test_every_answer_is_framed_whatever_version_the_client_names(
    publisher: OpenMetricsPublisher,
    label: str,
    version: bytes,
    path_label: str,
    path: bytes,
    status: bytes,
) -> None:
    raw = _exchange(publisher, b"GET " + path + version + CRLF + CRLF).encode("utf-8", "replace")
    head, found, body = raw.partition(CRLF + CRLF)
    assert found, f"{label}/{path_label}: the answer had no header block at all"
    first = head.splitlines()[0]
    assert first.startswith(b"HTTP/1."), f"{label}/{path_label}: no status line: {first!r}"
    assert status in first, f"{label}/{path_label}: {first!r}"
    assert b"Content-Type:" in head, f"{label}/{path_label}: no content type"
    assert b"Content-Length:" in head, f"{label}/{path_label}: no explicit length"
    length = next(
        int(line.split(b":", 1)[1])
        for line in head.splitlines()
        if line.lower().startswith(b"content-length:")
    )
    assert length == len(body)


@pytest.mark.parametrize(
    ("label", "version"), VERSION_TOKENS, ids=[row[0] for row in VERSION_TOKENS]
)
def test_the_no_sink_answer_is_framed_whatever_version_the_client_names(
    label: str, version: bytes
) -> None:
    # api_c5e17ca5 freezes a 503 for this case; an unframed body is not a 503 at all.
    endpoint = OpenMetricsPublisher(None, port=0)
    endpoint.start()
    try:
        raw = _exchange(endpoint, b"GET /metrics" + version + CRLF + CRLF).encode(
            "utf-8", "replace"
        )
    finally:
        endpoint.stop()
    head, found, _ = raw.partition(CRLF + CRLF)
    assert found and b" 503 " in head.splitlines()[0], f"{label}: {head[:60]!r}"


@pytest.mark.parametrize(
    ("label", "version"), VERSION_TOKENS, ids=[row[0] for row in VERSION_TOKENS]
)
def test_head_carries_no_body_whatever_version_the_client_names(
    publisher: OpenMetricsPublisher, label: str, version: bytes
) -> None:
    raw = _exchange(publisher, b"HEAD /metrics" + version + CRLF + CRLF).encode("utf-8", "replace")
    head, found, body = raw.partition(CRLF + CRLF)
    assert found, f"{label}: no header block"
    assert body == b"", f"{label}: {len(body)} bytes of body in a response to HEAD"


def test_a_malformed_request_line_still_gets_a_status_line(
    publisher: OpenMetricsPublisher,
) -> None:
    answer = _raw(publisher, b"GARBAGE\r\n\r\n")
    assert answer.startswith("HTTP/1.0 ") or answer.startswith("HTTP/1.1 ")
    assert "<html" not in answer.lower()
    assert "text/plain" in answer


def test_an_unsupported_method_answers_in_plain_text(publisher: OpenMetricsPublisher) -> None:
    answer = _raw(publisher, b"PATCH /metrics HTTP/1.1\r\nHost: localhost\r\n\r\n")
    assert " 501 " in answer.splitlines()[0]
    assert "text/plain" in answer
    assert "<html" not in answer.lower()


def test_an_http_1_1_request_without_a_host_header_is_refused(
    publisher: OpenMetricsPublisher,
) -> None:
    answer = _raw(publisher, b"GET /metrics HTTP/1.1\r\n\r\n")
    assert " 400 " in answer.splitlines()[0]
    assert "Host header" in answer
    assert "oktografx_" not in answer


def test_an_http_1_0_request_without_a_host_header_is_served(
    publisher: OpenMetricsPublisher,
) -> None:
    # RFC 7230 requires the header of HTTP/1.1 only, and a 1.0 scraper is still a scraper.
    answer = _raw(publisher, b"GET /metrics HTTP/1.0\r\n\r\n")
    assert " 200 " in answer.splitlines()[0]
    assert "oktografx_ledger_depth" in answer


def test_a_head_terminated_by_bare_line_feeds_is_served(
    recording_sink: OpenMetricsSink,
) -> None:
    """A lenient client that writes LF instead of CRLF is answered, not left to time out.

    _BARE_BLANK_LINE carries its own docstring, which makes it a declared behaviour; without a
    test it was a declared behaviour with no coverage, and dropping it passed the whole suite.
    The deadline here is short on purpose: losing the support turns this into a hang, and a
    hanging test should fail quickly rather than sit on the default deadline.
    """
    endpoint = OpenMetricsPublisher(recording_sink, port=0, connection_timeout=0.5)
    endpoint.start()
    try:
        answer = _raw(endpoint, b"GET /metrics HTTP/1.0" + BARE_LF + BARE_LF)
    finally:
        endpoint.stop()
    assert " 200 " in answer.splitlines()[0], f"a bare line feed head was not served: {answer!r}"
    assert "oktografx_ledger_depth" in answer


@pytest.mark.parametrize(
    ("head", "complete"),
    [
        (b"GET /metrics HTTP/1.1", False),
        (b"GET /metrics HTTP/1.1" + CRLF, False),
        (b"GET /metrics HTTP/1.1" + CRLF + b"Host: x" + CRLF, False),
        (b"GET /metrics HTTP/1.1" + CRLF + CRLF, True),
        (b"GET /metrics HTTP/1.1" + CRLF + b"Host: x" + CRLF + CRLF, True),
        (b"GET /metrics HTTP/1.0" + BARE_LF + BARE_LF, True),
        (b"GET /metrics HTTP/1.0" + BARE_LF + b"Host: x" + BARE_LF + BARE_LF, True),
        (b"GET /metrics" + CRLF, True),
        (b"GET /metrics" + BARE_LF, True),
        (b"GET", False),
    ],
    ids=[
        "request-line-only",
        "request-line-crlf",
        "one-header-no-blank-line",
        "crlf-terminated",
        "crlf-with-header",
        "bare-lf-terminated",
        "bare-lf-with-header",
        "http-0-9-crlf",
        "http-0-9-bare-lf",
        "partial-token",
    ],
)
def test_the_head_terminators_the_reader_recognises(head: bytes, complete: bool) -> None:
    assert _head_is_complete(head) is complete


def test_an_unterminated_request_head_is_answered_from_the_size_bound() -> None:
    """A head that never ends is refused on its size, while the client is still sending.

    A terminated head, however long, proves nothing here: the completeness check ends it and the
    parser answers on its merits. The bound exists for the head that never terminates.

    The premise -- that the client really did offer past the bound -- is counted *before* each
    send rather than after it. Counting after was a scheduling race wearing a byte counter:
    ``5 + 4096*19 = 77829`` is under the 81920-byte bound and ``5 + 4096*20 = 81925`` is over it,
    so the endpoint answers strictly *inside* the twentieth ``sendall`` and a counter incremented
    on its return could still read 77829 when the answer arrived. Measured at 1 failure in 400
    quiet trials, always at exactly that boundary value. Counting what was offered makes the
    premise true the moment the twentieth send begins, which is at or before the moment the
    endpoint can have seen the byte that triggers it.

    The sender also stops as soon as the connection becomes readable. Continuing to write into a
    socket the endpoint has finished with is what leaves unread bytes queued, and unread bytes at
    close are what make Windows send RST and discard the answer that was already written.
    """
    offered = 0
    stop_sending = threading.Event()
    endpoint = OpenMetricsPublisher(sink_with_catalog(), port=0, connection_timeout=2.0)
    endpoint.start()
    client = socket.create_connection((endpoint.host, endpoint.port), timeout=TIMEOUT)

    def flood() -> None:
        # No blank line, ever: a request line that simply never ends.
        nonlocal offered
        chunk = b"x" * 4096
        with contextlib.suppress(OSError):
            with selectors.DefaultSelector() as selector:
                selector.register(client, selectors.EVENT_READ)
                offered += 5
                client.sendall(b"GET /")
                while not stop_sending.is_set() and offered < 4_000_000:
                    if selector.select(timeout=0):
                        # The endpoint has answered; anything more would only queue unread.
                        return
                    offered += len(chunk)
                    client.sendall(chunk)

    sender = threading.Thread(target=flood, name="flood", daemon=True)
    try:
        started = time.monotonic()
        sender.start()
        answer = _drain(client)
        elapsed = time.monotonic() - started
        stop_sending.set()

        assert answer, "an unterminated head was never answered at all"
        assert " 414 " in answer.splitlines()[0], f"unexpected answer: {answer.splitlines()[0]!r}"
        assert "oktografx_" not in answer
        # Deliberately not a timing assertion. The deadline cannot produce this answer at all:
        # when it expires the read returns nothing and the connection is closed in silence, so a
        # 414 can only have come from the size bound.
        assert offered >= _MAX_HEAD_BYTES, (
            f"only {offered} bytes were offered, below the bound of {_MAX_HEAD_BYTES}: the test "
            f"never reached the condition it is about"
        )
        assert elapsed < endpoint._connection_timeout * 10, (
            f"the answer took {elapsed:.2f}s, far past any bounded read"
        )
    finally:
        stop_sending.set()
        sender.join(timeout=TIMEOUT)
        with contextlib.suppress(OSError):
            client.close()
        endpoint.stop()


def test_a_long_but_terminated_head_is_answered_on_its_merits(
    publisher: OpenMetricsPublisher,
) -> None:
    # The companion case: a head that does end is judged by the parser, not by the bound.
    answer = _exchange(
        publisher, b"GET /" + b"x" * OVER_LONG_REQUEST_LINE + b" HTTP/1.1" + CRLF + CRLF
    )
    assert " 414 " in answer.splitlines()[0]
    assert "oktografx_" not in answer


OVER_LONG_REQUEST_LINE: int = 70_000
"""Just past the 65536-byte request line limit of the parser, and well under the head bound.

Size matters twice here. Above the parser limit the answer is 414, which is the point. Below the
read bound the endpoint consumes the whole request before answering, so its close is orderly and
nothing is left queued -- and a close with unread bytes queued is what makes Windows send RST and
discard the response that was already written. Sending three times as much proved nothing extra
and made the test intermittently red.
"""


HEAD_ONLY_REQUESTS: tuple[tuple[str, bytes], ...] = (
    ("well formed", b"HEAD /metrics HTTP/1.1" + CRLF + b"Host: localhost" + CRLF + CRLF),
    ("unknown path", b"HEAD /nope HTTP/1.1" + CRLF + b"Host: localhost" + CRLF + CRLF),
    ("unsupported version", b"HEAD /metrics HTTP/9.9" + CRLF + CRLF),
    ("unparseable version", b"HEAD /metrics HTTP/A.B" + CRLF + CRLF),
    ("over-long request line", b"HEAD /" + b"x" * OVER_LONG_REQUEST_LINE + b" HTTP/1.1" + CRLF + CRLF),
)
"""Every HEAD path that reaches a response, including the three that go through send_error."""


@pytest.mark.parametrize(
    ("label", "request_bytes"), HEAD_ONLY_REQUESTS, ids=[row[0] for row in HEAD_ONLY_REQUESTS]
)
def test_a_head_request_never_carries_a_body(
    publisher: OpenMetricsPublisher, label: str, request_bytes: bytes
) -> None:
    """RFC 7231 section 4.3.2: a response to HEAD carries no body, on every path.

    The three error paths are the interesting ones. The parser of the standard library clears
    ``command`` before it checks the version, and clears it again with ``requestline`` when the
    request line is too long, so a guard that trusts ``command`` writes a body into exactly the
    responses that may not have one.
    """
    raw = _exchange(publisher, request_bytes).encode("utf-8", "replace")
    head, separator, body = raw.partition(CRLF + CRLF)
    assert separator, f"{label}: no complete response head"
    assert body == b"", f"{label}: {len(body)} bytes of body in a response to HEAD"
    assert b"Content-Length:" in head, f"{label}: no explicit length"


@pytest.mark.parametrize(
    ("label", "request_bytes"), HEAD_ONLY_REQUESTS, ids=[row[0] for row in HEAD_ONLY_REQUESTS]
)
def test_the_get_of_the_same_request_does_carry_a_body(
    publisher: OpenMetricsPublisher, label: str, request_bytes: bytes
) -> None:
    # The mirror of the rule above: suppressing the body for HEAD must not suppress it for GET.
    raw = _exchange(publisher, request_bytes.replace(b"HEAD", b"GET", 1)).encode("utf-8", "replace")
    _, separator, body = raw.partition(CRLF + CRLF)
    assert separator
    assert body, f"{label}: a GET answered with no body at all"


REQUEST_LINE_SEPARATORS: tuple[tuple[str, bytes], ...] = (
    ("space", b" "),
    ("tab", b"	"),
    ("vertical tab", b""),
    ("form feed", b""),
    ("two spaces", b"  "),
)
"""Every separator ``requestline.split()`` accepts between the method and the target.

The parser of the standard library separates on any run of whitespace, so all of these are the
same HEAD request to it, and it dispatches them to ``do_HEAD``. Anything here that tokenised on
a literal space alone would disagree with the parser about what the request even is, and would
write a body into a HEAD response on exactly the paths where the parser has cleared ``command``.
"""


@pytest.mark.parametrize(
    ("label", "separator"), REQUEST_LINE_SEPARATORS, ids=[row[0] for row in REQUEST_LINE_SEPARATORS]
)
@pytest.mark.parametrize(
    ("path_label", "tail"),
    [
        ("ok", b"/metrics HTTP/1.1" + CRLF + b"Host: localhost" + CRLF + CRLF),
        ("unknown path", b"/nope HTTP/1.1" + CRLF + b"Host: localhost" + CRLF + CRLF),
        ("unsupported version", b"/metrics HTTP/9.9" + CRLF + CRLF),
        ("unparseable version", b"/metrics HTTP/A.B" + CRLF + CRLF),
    ],
    ids=["ok", "unknown-path", "unsupported-version", "unparseable-version"],
)
def test_head_is_recognised_on_every_separator_the_parser_accepts(
    publisher: OpenMetricsPublisher, label: str, separator: bytes, path_label: str, tail: bytes
) -> None:
    raw = _exchange(publisher, b"HEAD" + separator + tail).encode("utf-8", "replace")
    head, found, body = raw.partition(CRLF + CRLF)
    assert found, f"{label}/{path_label}: no complete response head"
    assert body == b"", (
        f"{label}/{path_label}: {len(body)} bytes of body in a response to HEAD"
    )


@pytest.mark.parametrize(
    ("label", "separator"), REQUEST_LINE_SEPARATORS, ids=[row[0] for row in REQUEST_LINE_SEPARATORS]
)
def test_the_same_separators_leave_a_get_body_alone(
    publisher: OpenMetricsPublisher, label: str, separator: bytes
) -> None:
    # The mirror: recognising HEAD more widely must not suppress the body of anything else.
    raw = _exchange(publisher, b"GET" + separator + b"/metrics HTTP/9.9" + CRLF + CRLF).encode(
        "utf-8", "replace"
    )
    _, found, body = raw.partition(CRLF + CRLF)
    assert found and body, f"{label}: a GET was answered with no body"


def test_the_method_is_read_as_case_sensitively_as_the_parser_reads_it(
    publisher: OpenMetricsPublisher,
) -> None:
    # The parser dispatches on the exact spelling, so a lowercase head is not a HEAD request and
    # its response is an ordinary one, body and all.
    raw = _exchange(
        publisher, b"head /metrics HTTP/1.1" + CRLF + b"Host: localhost" + CRLF + CRLF
    ).encode("utf-8", "replace")
    status, _, body = raw.partition(CRLF + CRLF)
    assert b" 501 " in status.splitlines()[0]
    assert body, "a request the parser did not treat as HEAD was answered without a body"


def test_the_method_derivation_survives_a_cleared_command(
    publisher: OpenMetricsPublisher,
) -> None:
    # The single derivation reads the raw request line, which the parser never clears; this is
    # the property that makes one rule enough where three tiers disagreed.
    handler = _MetricsRequestHandler.__new__(_MetricsRequestHandler)
    handler.raw_requestline = b"HEAD	/metrics HTTP/1.1" + CRLF
    handler.command = None
    handler.requestline = ""
    assert handler._request_method() == "HEAD"
    assert handler._head_requested() is True
    handler.raw_requestline = b"GET /metrics HTTP/1.1" + CRLF
    assert handler._head_requested() is False
    handler.raw_requestline = b""
    assert handler._request_method() == ""
    assert handler._head_requested() is False


def test_a_head_of_the_metrics_path_reports_the_length_of_the_body_it_withheld(
    publisher: OpenMetricsPublisher,
) -> None:
    headers = _raw(publisher, b"HEAD /metrics HTTP/1.1" + CRLF + b"Host: localhost" + CRLF + CRLF)
    length = next(
        int(line.split(":", 1)[1])
        for line in headers.splitlines()
        if line.lower().startswith("content-length:")
    )
    body = _fetch(publisher.url)[2]
    assert length == len(body.encode("utf-8"))
    assert length > 0


@pytest.mark.parametrize("method", ["PUT", "DELETE", "POST"])
def test_every_write_method_is_refused_in_plain_text(
    publisher: OpenMetricsPublisher, method: str
) -> None:
    answer = _raw(
        publisher,
        method.encode() + b" /metrics HTTP/1.1" + CRLF + b"Host: localhost" + CRLF + CRLF,
    )
    assert " 404 " in answer.splitlines()[0]
    assert "text/plain" in answer
    assert "Not found." in answer
    assert "oktografx_" not in answer


def test_every_response_states_its_length(publisher: OpenMetricsPublisher) -> None:
    # Without an explicit length a 1.1 client waits for a close that the keep-alive machinery
    # of the standard library would not necessarily send.
    for request_bytes in (
        b"GET /metrics HTTP/1.1" + CRLF + b"Host: localhost" + CRLF + CRLF,
        b"GET /nope HTTP/1.1" + CRLF + b"Host: localhost" + CRLF + CRLF,
        b"PUT /metrics HTTP/1.1" + CRLF + b"Host: localhost" + CRLF + CRLF,
    ):
        answer = _raw(publisher, request_bytes)
        header = next(
            line for line in answer.splitlines() if line.lower().startswith("content-length:")
        )
        _, _, body = answer.partition((CRLF + CRLF).decode("ascii"))
        assert int(header.split(":", 1)[1]) == len(body.encode("utf-8"))


def test_the_server_header_does_not_disclose_the_build(publisher: OpenMetricsPublisher) -> None:
    answer = _raw(publisher, b"GET /metrics HTTP/1.1\r\nHost: localhost\r\n\r\n")
    header = next(line for line in answer.splitlines() if line.lower().startswith("server:"))
    assert header == "Server: okto-grafx"
    assert "Python" not in answer.split("\r\n\r\n", 1)[0]
    assert "/1.0" not in header


def test_stopping_twice_after_parked_connections_is_still_harmless(
    recording_sink: OpenMetricsSink,
) -> None:
    endpoint = OpenMetricsPublisher(
        recording_sink, port=0, connection_timeout=PARKED_CONNECTION_DEADLINE
    )
    endpoint.start()
    clients = _open_silent_connections(endpoint, 3)
    try:
        server = endpoint._server
        assert server is not None
        endpoint.stop()
        endpoint.stop()
        assert endpoint.running is False
        assert server.live_handlers == 0
        assert server.live_connections == 0
    finally:
        for client in clients:
            with contextlib.suppress(OSError):
                client.close()
