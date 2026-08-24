"""Reversible quarantine: capture, manifest, idempotence and restore (FR-10, BR-1, G6)."""

from __future__ import annotations

import hashlib

import pytest

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxPortNotConfigured,
    GrafxQuarantineError,
    GrafxStorageError,
)
from okto_grafx.domain.recovery.manifest import (
    MANIFEST_FILE_NAME,
    STAMP_DIGITS,
    QuarantineManifest,
    entry_suffix,
    sanitize_name,
    stamp_of,
)
from okto_grafx.engine.quarantine import (
    PROTECTED_FILES,
    QUARANTINE_ENTRIES,
    QuarantineStore,
    is_protected,
)

from okto_grafx.adapters.storage_fault import FaultInjectingStorageDevice, SimulatedCrash
from okto_grafx.adapters.storage_memory import MemoryStorageDevice

from .conftest import FrozenClock, RecordingMetricsSink, Stack, build_stack

ORIGIN = "wal/000000000001.wal"


def _seed(stack: Stack, name: str = ORIGIN, body: bytes = b"0123456789abcdef") -> bytes:
    """Put a file on the device with known bytes and return them."""
    device = stack.storage
    if not device.exists(name):  # type: ignore[attr-defined]
        device.create(name, exclusive=False)  # type: ignore[attr-defined]
    device.append_log(name, body)  # type: ignore[attr-defined]
    return body


# --- names and manifests ------------------------------------------------------------------------


def test_a_name_is_sanitized_into_something_both_families_accept() -> None:
    assert sanitize_name("wal/000000000001.wal") == "wal_000000000001.wal"
    assert sanitize_name("control\\writer.lease") == "control_writer.lease"
    assert sanitize_name("../../etc/passwd") == "etc_passwd"
    assert "/" not in sanitize_name("a/b/c") and "\\" not in sanitize_name("a\\b")


def test_a_name_built_from_nothing_is_a_caller_error() -> None:
    with pytest.raises(GrafxConfigurationError):
        sanitize_name("")


def test_the_stamp_orders_entries_chronologically_by_plain_name_sort() -> None:
    early = stamp_of(1_700_000_000.0)
    later = stamp_of(1_700_000_001.0)
    assert len(early) == STAMP_DIGITS and early < later


def test_the_suffix_identifies_the_range_and_not_the_moment() -> None:
    assert entry_suffix(ORIGIN, 16, 32) == entry_suffix(ORIGIN, 16, 32)
    assert entry_suffix(ORIGIN, 16, 32) != entry_suffix(ORIGIN, 17, 32)


def test_a_manifest_round_trips_through_its_text_form() -> None:
    manifest = QuarantineManifest(
        origin=ORIGIN,
        offset=16,
        length=32,
        reason="checksum_failure",
        detail="The checksum did not match.",
        captured_at_wall=1_700_000_000.5,
        digest="a" * 64,
        payload_file="quarantine/x/000000000001.wal",
        entry_name="x",
        expected_lsn=7,
    )
    assert QuarantineManifest.parse(manifest.serialize()) == manifest


def test_a_manifest_that_will_not_parse_is_damaged_evidence(memory_device: object) -> None:
    with pytest.raises(GrafxCorruptionDetected):
        QuarantineManifest.parse(b"not a manifest")
    with pytest.raises(GrafxCorruptionDetected) as caught:
        QuarantineManifest.parse(b'{"origin":"a"}')
    assert caught.value.details["field"] == "offset"


# --- capture ------------------------------------------------------------------------------------


def test_a_port_missing_a_door_the_quarantine_opens_is_refused_at_construction(
    clock: FrozenClock, metrics: RecordingMetricsSink
) -> None:
    class Half:
        """A device that answers only some of the storage port."""

        def exists(self, file: str) -> bool:
            """Answer the one door this stand-in implements."""
            return False

    with pytest.raises(GrafxPortNotConfigured):
        QuarantineStore(Half(), clock, metrics)  # type: ignore[arg-type]


def test_a_capture_copies_the_range_and_writes_a_manifest_beside_it(stack: Stack) -> None:
    body = _seed(stack)
    entry = stack.quarantine.capture(
        origin=ORIGIN, offset=4, length=8, reason="checksum_failure", detail="Damaged."
    )
    assert stack.quarantine.read(entry.name) == body[4:12]
    assert entry.manifest.digest == hashlib.sha256(body[4:12]).hexdigest()
    assert entry.manifest.origin == ORIGIN
    assert entry.manifest.offset == 4 and entry.manifest.length == 8
    assert entry.manifest_file.endswith(MANIFEST_FILE_NAME)


def test_a_capture_never_touches_the_file_it_copied_from(stack: Stack) -> None:
    body = _seed(stack)
    stack.quarantine.capture(origin=ORIGIN, offset=0, length=len(body), reason="truncated_tail")
    assert stack.storage.exists(ORIGIN)  # type: ignore[attr-defined]
    size = stack.storage.log_size(ORIGIN)  # type: ignore[attr-defined]
    assert stack.storage.read_log(ORIGIN, 0, size) == body  # type: ignore[attr-defined]


def test_capturing_the_same_range_twice_returns_the_entry_that_already_holds_it(
    stack: Stack,
) -> None:
    _seed(stack)
    first = stack.quarantine.capture(origin=ORIGIN, offset=4, length=8, reason="truncated_tail")
    stack.clock.advance(3600.0)
    second = stack.quarantine.capture(origin=ORIGIN, offset=4, length=8, reason="truncated_tail")
    assert second.name == first.name
    assert stack.quarantine.count() == 1


def test_capturing_a_range_whose_bytes_changed_refuses_rather_than_replacing_the_copy(
    stack: Stack,
) -> None:
    _seed(stack)
    first = stack.quarantine.capture(origin=ORIGIN, offset=0, length=16, reason="truncated_tail")
    stack.storage.truncate_log(ORIGIN, 0)  # type: ignore[attr-defined]
    stack.storage.append_log(ORIGIN, b"different bytes!")  # type: ignore[attr-defined]
    with pytest.raises(GrafxQuarantineError) as caught:
        stack.quarantine.capture(origin=ORIGIN, offset=0, length=16, reason="truncated_tail")
    assert caught.value.details["field"] == "digest"
    assert stack.quarantine.read(first.name) == b"0123456789abcdef"


def test_a_capture_of_a_file_that_does_not_exist_is_refused(stack: Stack) -> None:
    with pytest.raises(GrafxQuarantineError) as caught:
        stack.quarantine.capture(origin="wal/missing.wal", offset=0, length=4, reason="x")
    assert caught.value.details["field"] == "origin"


def test_a_capture_past_the_end_of_the_origin_is_refused(stack: Stack) -> None:
    _seed(stack)
    with pytest.raises(GrafxQuarantineError) as caught:
        stack.quarantine.capture(origin=ORIGIN, offset=1000, length=4, reason="x")
    assert caught.value.details["field"] == "offset"


def test_a_capture_may_carry_bytes_the_caller_already_holds(stack: Stack) -> None:
    _seed(stack)
    entry = stack.quarantine.capture(
        origin=ORIGIN, offset=0, length=5, reason="truncated_tail", payload=b"given"
    )
    assert stack.quarantine.read(entry.name) == b"given"


def test_the_entry_gauge_counts_what_is_kept(stack: Stack) -> None:
    _seed(stack)
    stack.quarantine.capture(origin=ORIGIN, offset=0, length=4, reason="a")
    assert stack.metrics.gauge(QUARANTINE_ENTRIES) == 1.0
    stack.quarantine.capture(origin=ORIGIN, offset=4, length=4, reason="a")
    assert stack.metrics.gauge(QUARANTINE_ENTRIES) == 2.0


def test_entries_are_listed_oldest_first(stack: Stack) -> None:
    _seed(stack)
    first = stack.quarantine.capture(origin=ORIGIN, offset=0, length=4, reason="a")
    stack.clock.advance(60.0)
    second = stack.quarantine.capture(origin=ORIGIN, offset=4, length=4, reason="a")
    assert [entry.name for entry in stack.quarantine.list()] == [first.name, second.name]


def test_reading_bytes_that_no_longer_match_the_manifest_is_refused(stack: Stack) -> None:
    _seed(stack)
    entry = stack.quarantine.capture(origin=ORIGIN, offset=0, length=8, reason="a")
    stack.storage.truncate_log(entry.payload_file, 0)  # type: ignore[attr-defined]
    stack.storage.append_log(entry.payload_file, b"tampered")  # type: ignore[attr-defined]
    with pytest.raises(GrafxCorruptionDetected) as caught:
        stack.quarantine.read(entry.name)
    assert caught.value.details["field"] == "digest"


def test_an_interrupted_capture_is_left_out_of_the_listing_and_its_bytes_are_kept(
    stack: Stack,
) -> None:
    _seed(stack)
    entry = stack.quarantine.capture(origin=ORIGIN, offset=0, length=8, reason="a")
    stack.storage.remove(entry.manifest_file)  # type: ignore[attr-defined]
    assert stack.quarantine.list() == ()
    assert stack.storage.exists(entry.payload_file)  # type: ignore[attr-defined]
    with pytest.raises(GrafxQuarantineError):
        stack.quarantine.inspect(entry.name)


def test_an_interruption_inside_a_capture_never_lists_an_entry_without_its_bytes(
    clock: FrozenClock, metrics: RecordingMetricsSink
) -> None:
    """The rule the write order buys: a LISTED entry always has the bytes it promises.

    A capture writes two files. Whichever lands second, an interruption between them leaves one
    of two states: bytes with no manifest, which the listing skips and the next capture
    completes, or a manifest naming bytes that are not there -- an entry an operator would find,
    trust and be unable to read. Only the first is acceptable, and the order is what decides
    which one an interruption can produce. This states that rule rather than the ordering, so it
    holds however the two writes are arranged (A71).
    """
    inner = MemoryStorageDevice(page_size=512)
    bench = FaultInjectingStorageDevice(inner, seed=20260820)
    try:
        stack = build_stack(bench, clock=clock, metrics=metrics)
        _seed(stack)
        bench.clear_trail()
        # After the first of the two files is durable, and before the second one is written.
        bench.crash_on("durable_barrier", occurrence=1, moment="after")
        with pytest.raises(SimulatedCrash):
            stack.quarantine.capture(origin=ORIGIN, offset=0, length=16, reason="a")
        bench.disarm()
        for entry in stack.quarantine.list():
            # Reaching this line at all means the interruption left a listed entry; it must be
            # readable, and its bytes must be the ones its manifest recorded.
            assert stack.quarantine.read(entry.name) == b"0123456789abcdef"
    finally:
        inner.close()


def test_an_entry_name_that_is_a_path_is_refused(stack: Stack) -> None:
    with pytest.raises(GrafxConfigurationError):
        stack.quarantine.inspect("a/b")
    with pytest.raises(GrafxConfigurationError):
        stack.quarantine.inspect("..")


def test_a_capture_whose_origin_is_named_like_the_manifest_keeps_both_files(
    stack: Stack,
) -> None:
    """B3 / LESSONS L5: the evidence must survive an origin named after the reserved manifest.

    Every content check passes when the bytes are written to the manifest's own name and then
    written over: capture returns a valid entry, the listing shows it, the digest matches what is
    there. Only the DEVICE listing can see that one file exists where two must. So that is what
    this asserts -- the namespace, independently of the operation's own answer.
    """
    origin = "wal/manifest.json"
    body = b"the bytes that must survive"
    _seed(stack, name=origin, body=body)
    entry = stack.quarantine.capture(
        origin=origin, offset=0, length=len(body), reason="truncated_tail"
    )
    files = stack.storage.list_files(f"{entry.directory}/")  # type: ignore[attr-defined]
    assert len(files) == 2, files
    assert entry.payload_file != entry.manifest_file
    assert set(files) == {entry.payload_file, entry.manifest_file}
    assert stack.quarantine.read(entry.name) == body


def test_a_reserved_name_in_another_case_is_still_reserved(stack: Stack) -> None:
    """DoD item 9 and G4: ``MANIFEST.JSON`` is a second name on POSIX and ONE file on NTFS.

    A case-sensitive comparison here passes on the family the suite happens to run on and
    destroys the evidence on the family the contract calls primary.
    """
    # Each in its own directory: the devices now refuse two stored names differing only by
    # case, exactly as NTFS does, so seeding all three side by side would collide before the
    # property under test could be reached.
    for origin in ("wal/a/MANIFEST.JSON", "wal/b/Manifest.Json", "wal/c/RESTORE-2.JSON"):
        body = b"evidence for " + origin.encode("ascii")
        _seed(stack, name=origin, body=body)
        entry = stack.quarantine.capture(
            origin=origin, offset=0, length=len(body), reason="truncated_tail"
        )
        files = stack.storage.list_files(f"{entry.directory}/")  # type: ignore[attr-defined]
        assert len(files) == 2, (origin, files)
        assert entry.payload_file.casefold() != entry.manifest_file.casefold(), origin
        assert stack.quarantine.read(entry.name) == body


def test_the_namespace_check_still_refuses_when_the_naming_rule_does_not_cover_a_case(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A93's second cell, which is what makes keeping BOTH defences honest (A67b).

    The naming rule prevents the collision, so with it in place the namespace check can never
    fire and nothing proves it works. This disables the naming rule -- the situation a future
    reserved name, or a platform rule nobody anticipated, really produces -- and asserts the
    backstop still refuses rather than reporting a capture that destroyed its own evidence.

    The full matrix: naming rule + check -> no collision; rule alone -> no collision; CHECK
    ALONE -> refused, which is this test; NEITHER -> one file on the device, digest matching,
    capture reporting success, and the evidence gone.
    """
    import okto_grafx.engine.quarantine as module

    monkeypatch.setattr(module, "_payload_file_name", lambda origin: MANIFEST_FILE_NAME)
    origin = "wal/000000000001.wal"
    _seed(stack, name=origin, body=b"evidence that must not vanish")
    with pytest.raises(GrafxQuarantineError) as caught:
        stack.quarantine.capture(origin=origin, offset=0, length=8, reason="truncated_tail")
    assert caught.value.details["field"] == "payload_file"
    assert stack.quarantine.list() == ()


def test_an_origin_named_like_a_restore_receipt_cannot_collide_with_one(stack: Stack) -> None:
    origin = "wal/restore-1.json"
    body = b"still evidence"
    _seed(stack, name=origin, body=body)
    entry = stack.quarantine.capture(
        origin=origin, offset=0, length=len(body), reason="truncated_tail"
    )
    stack.quarantine.restore(entry.name, target="wal/put-back.json")
    files = set(stack.storage.list_files(f"{entry.directory}/"))  # type: ignore[attr-defined]
    assert entry.payload_file in files
    assert stack.quarantine.read(entry.name) == body


def test_a_capture_cannot_be_handed_something_that_is_not_bytes(stack: Stack) -> None:
    """B4: ``bytes(17)`` is seventeen NUL bytes, and would be preserved as the evidence."""
    _seed(stack)
    for wrong in (17, True, 3.5, ["a"], object()):
        with pytest.raises(GrafxConfigurationError) as caught:
            stack.quarantine.capture(
                origin=ORIGIN, offset=0, length=1, reason="a", payload=wrong  # type: ignore[arg-type]
            )
        assert caught.value.details["field"] == "payload"
    assert stack.quarantine.list() == ()


# --- the files G6 protects ------------------------------------------------------------------------


def test_every_main_data_file_is_recognised_as_protected() -> None:
    for name in PROTECTED_FILES:
        assert is_protected(name) is True
    assert is_protected("bootstrap/first-open.intent") is True
    assert is_protected("index/by_name.idx") is True
    assert is_protected("wal/000000000001.wal") is False
    assert is_protected("control/writer.lease") is False


def test_a_restore_never_writes_over_a_main_data_file(stack: Stack) -> None:
    _seed(stack)
    entry = stack.quarantine.capture(origin=ORIGIN, offset=0, length=16, reason="a")
    for target in (
        "heap.dat",
        "catalog.dat",
        "bootstrap/first-open.intent",
        "index/by_name.idx",
    ):
        with pytest.raises(GrafxQuarantineError) as caught:
            stack.quarantine.restore(entry.name, target=target)
        assert caught.value.details["field"] == "target"


# --- restore -------------------------------------------------------------------------------------


def test_a_restore_puts_the_bytes_back_and_leaves_a_receipt(stack: Stack) -> None:
    body = _seed(stack)
    entry = stack.quarantine.capture(origin=ORIGIN, offset=0, length=len(body), reason="a")
    stack.storage.remove(ORIGIN)  # type: ignore[attr-defined]
    report = stack.quarantine.restore(entry.name)
    assert report.restored_to == ORIGIN and report.restored_bytes == len(body)
    size = stack.storage.log_size(ORIGIN)  # type: ignore[attr-defined]
    assert stack.storage.read_log(ORIGIN, 0, size) == body  # type: ignore[attr-defined]
    assert stack.quarantine.receipts(entry.name) == (report.receipt_file,)


def test_a_restore_keeps_the_quarantined_copy(stack: Stack) -> None:
    body = _seed(stack)
    entry = stack.quarantine.capture(origin=ORIGIN, offset=0, length=len(body), reason="a")
    stack.storage.remove(ORIGIN)  # type: ignore[attr-defined]
    stack.quarantine.restore(entry.name)
    assert stack.quarantine.read(entry.name) == body
    assert stack.quarantine.count() == 1


def test_a_restore_onto_a_name_that_exists_is_refused(stack: Stack) -> None:
    body = _seed(stack)
    entry = stack.quarantine.capture(origin=ORIGIN, offset=0, length=len(body), reason="a")
    with pytest.raises(GrafxQuarantineError) as caught:
        stack.quarantine.restore(entry.name)
    assert caught.value.details["field"] == "target"
    size = stack.storage.log_size(ORIGIN)  # type: ignore[attr-defined]
    assert stack.storage.read_log(ORIGIN, 0, size) == body  # type: ignore[attr-defined]


def test_a_restore_of_a_fragment_is_refused_because_it_would_be_a_patch(stack: Stack) -> None:
    _seed(stack)
    entry = stack.quarantine.capture(origin=ORIGIN, offset=4, length=4, reason="a")
    with pytest.raises(GrafxQuarantineError) as caught:
        stack.quarantine.restore(entry.name, target="wal/restored.wal")
    assert caught.value.details["field"] == "offset"


def test_two_restores_leave_two_receipts_and_neither_replaces_the_other(stack: Stack) -> None:
    body = _seed(stack)
    entry = stack.quarantine.capture(origin=ORIGIN, offset=0, length=len(body), reason="a")
    first = stack.quarantine.restore(entry.name, target="wal/copy-one.wal")
    second = stack.quarantine.restore(entry.name, target="wal/copy-two.wal")
    assert first.receipt_file != second.receipt_file
    assert set(stack.quarantine.receipts(entry.name)) == {
        first.receipt_file,
        second.receipt_file,
    }


def test_a_copy_rides_out_a_device_condition_its_details_call_retryable(stack: Stack) -> None:
    _seed(stack)
    device = stack.storage
    original = device.append_log  # type: ignore[attr-defined]
    attempts: list[str] = []

    def flaky(file: str, payload: bytes) -> int:
        """Refuse the first write into the quarantine with a retryable condition."""
        if file.startswith("quarantine/") and not attempts:
            attempts.append(file)
            failure = GrafxStorageError("An indexer is holding the new directory open.")
            failure.details["retryable"] = True
            raise failure
        return original(file, payload)

    device.append_log = flaky  # type: ignore[attr-defined]
    try:
        entry = stack.quarantine.capture(origin=ORIGIN, offset=0, length=8, reason="a")
    finally:
        device.append_log = original  # type: ignore[attr-defined]
    assert attempts and stack.quarantine.read(entry.name) == b"01234567"


# --- the L5 backstop, and the tests that only it can satisfy --------------------------------------


class _MisnamingDevice(MemoryStorageDevice):
    """A device that writes the right bytes under the WRONG NAME and reports success.

    LESSONS L5 in one class. C2's hand-rolled rename returned success and wrote correct content
    under a garbled filename, and every content assertion passed because each of them read back
    through the name the operation itself had returned. Everything this device is asked about a
    file -- does it exist, how big is it, read it back -- it answers about the garbled name, so
    the capture, the digest and the entry it returns all agree with each other. Only
    ``list_files`` tells the truth, because a directory listing is the one witness that does not
    follow the operation's own answer.
    """

    def __init__(self, *, mangled_basename: str) -> None:
        """Start empty, publishing this one basename under a name nobody asked for."""
        super().__init__()
        self._mangled_basename = mangled_basename

    def _published(self, file: str) -> str:
        """Return the name this device actually uses for a logical name."""
        if file.rsplit("/", 1)[-1] == self._mangled_basename:
            return f"{file}-garbled"
        return file

    def exists(self, file: str) -> bool:
        """Answer about the published name, so the caller cannot see the substitution."""
        return super().exists(self._published(file))

    def create(self, file: str, *, exclusive: bool = True) -> None:
        """Create the published name and report success for the one that was asked for."""
        super().create(self._published(file), exclusive=exclusive)

    def append_log(self, file: str, payload: bytes) -> int:
        """Append to the published name."""
        return super().append_log(self._published(file), payload)

    def read_log(self, file: str, offset: int, length: int) -> bytes:
        """Read back from the published name, so even the digest agrees."""
        return super().read_log(self._published(file), offset, length)

    def log_size(self, file: str) -> int:
        """Size the published name."""
        return super().log_size(self._published(file))

    def truncate_log(self, file: str, size: int) -> None:
        """Truncate the published name."""
        super().truncate_log(self._published(file), size)

    def durable_barrier(self, file: str | None = None) -> None:
        """Barrier the published name."""
        super().durable_barrier(None if file is None else self._published(file))


def test_a_device_that_reports_a_write_it_did_not_perform_fails_the_capture() -> None:
    """The test only ``_require_two_files`` can satisfy (A62 / A67a).

    ``test_a_capture_whose_origin_is_named_like_the_manifest_keeps_both_files`` does NOT prove
    this guard: ``_payload_file_name`` prefixes the reserved names, so the collision it describes
    never reaches here, and the two mechanisms were alibiing each other -- replacing the device
    listing in this guard with the operation's own answer left the whole suite green.

    Here the two names the capture computed are distinct and correct, the bytes are all present,
    every read-back agrees, and the DEVICE is the thing that lied. Only the listing can see it,
    and the guard must be reading the listing to refuse.
    """
    device = _MisnamingDevice(mangled_basename="000000000001.wal")
    store = QuarantineStore(device, FrozenClock(), RecordingMetricsSink())
    device.create(ORIGIN, exclusive=False)
    device.append_log(ORIGIN, b"0123456789abcdef")
    with pytest.raises(GrafxQuarantineError) as caught:
        store.capture(origin=ORIGIN, offset=0, length=16, reason="truncated_tail")
    assert caught.value.details["field"] == "payload_file"
    observed = caught.value.details["observed"]
    # The listing holds two files and neither of them is the payload name the capture computed,
    # which is the whole of what the guard can see and the operation's own answer cannot.
    assert len(observed) == 2, observed
    assert not any(name.endswith("/000000000001.wal") for name in observed), observed
    assert any(name.endswith("-garbled") for name in observed), observed
    # No half-entry is left behind: a manifest with no bytes beside it is the one thing a
    # listing must never show.
    left = device.list_files(f"{store.directory}/")
    assert not any(name.endswith(MANIFEST_FILE_NAME) for name in left), left
    assert store.list() == ()


def test_an_entry_whose_name_merely_ends_with_the_suffix_is_not_that_range(
    stack: Stack,
) -> None:
    """``endswith`` is a filter over names; the manifest is what says which range an entry holds.

    An entry name is ``<stamp>-<origin>-<offset>-<length>``, and ``sanitize_name`` turns
    ``wal/1.wal`` into ``wal_1.wal`` -- so the suffix of the range ``1.wal-0-16`` is a genuine
    string suffix of the entry name of the range ``wal_1.wal-0-16``. Without the manifest
    re-check the second capture is handed the FIRST entry, and reports a different file's bytes
    as the copy it just made.
    """
    long_body = b"the bytes of the file inside wal"[:16]
    short_body = b"the other file's"[:16]
    assert long_body != short_body
    _seed(stack, name="wal/1.wal", body=long_body)
    _seed(stack, name="1.wal", body=short_body)
    first = stack.quarantine.capture(
        origin="wal/1.wal", offset=0, length=16, reason="truncated_tail"
    )
    assert first.name.endswith(entry_suffix("1.wal", 0, 16))
    second = stack.quarantine.capture(
        origin="1.wal", offset=0, length=16, reason="truncated_tail"
    )
    assert second.name != first.name
    assert second.manifest.origin == "1.wal"
    assert stack.quarantine.read(second.name) == short_body
    assert stack.quarantine.read(first.name) == long_body


def test_the_refusal_names_the_origin_the_entry_holds_not_the_one_that_was_asked_for(
    stack: Stack,
) -> None:
    """``sanitize_name`` is lossy, so two different origins can land on one entry suffix.

    ``wal/a.wal`` and ``wal_a.wal`` are two files and one suffix. When the second capture arrives
    with different bytes the copy that exists is kept -- and the message has to say whose bytes
    those are, because the one time anybody reads it is the time the two differ.
    """
    assert entry_suffix("wal/a.wal", 0, 16) == entry_suffix("wal_a.wal", 0, 16)
    held = b"the bytes of wal/a"[:16]
    other = b"different bytes.."[:16]
    assert held != other
    _seed(stack, name="wal/a.wal", body=held)
    _seed(stack, name="wal_a.wal", body=other)
    stack.quarantine.capture(origin="wal/a.wal", offset=0, length=16, reason="truncated_tail")
    with pytest.raises(GrafxQuarantineError) as caught:
        stack.quarantine.capture(
            origin="wal_a.wal", offset=0, length=16, reason="truncated_tail"
        )
    assert caught.value.details["field"] == "digest"
    assert caught.value.details["file"] == "wal/a.wal"
    assert caught.value.details["requested"] == "wal_a.wal"
    assert "'wal/a.wal'" in caught.value.message
