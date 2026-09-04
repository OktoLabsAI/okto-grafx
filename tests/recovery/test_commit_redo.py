"""The shared committed-effect dispatcher covers pages and both logical index records."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxRecoveryRefused
from okto_grafx.domain.ids import RecordRef
from okto_grafx.domain.index import IndexChange, IndexOperation, wal_record_for
from okto_grafx.domain.page.layout import PageType
from okto_grafx.domain.page.slotted import Page
from okto_grafx.domain.recovery.decision import CommittedReplay
from okto_grafx.domain.txn.records import encode_page_write, encode_page_write_record
from okto_grafx.domain.wal.record import WalRecord, WalRecordType
from okto_grafx.engine import commit_redo as commit_redo_module
from okto_grafx.engine.buffer_pool import MAX_REDO_GAP_PAGES
from okto_grafx.engine.commit_redo import CommitRedo, is_redoable_page_file

from .conftest import DESCRIPTOR, build_stack, make_page_image


@dataclass(frozen=True)
class _IndexDefinitionDouble:
    versioned: bool = False


@dataclass(frozen=True)
class _NamedIndex:
    file: str
    definition: _IndexDefinitionDouble = _IndexDefinitionDouble()
    max_key_bytes: int = 400


class _IndexManagerDouble:
    """Record dispatches and expose the physical file the named index owns."""

    def __init__(self, events: list[tuple[str, int]]) -> None:
        self.events = events
        self.named = _NamedIndex("index-by_name.dat")

    def apply(self, record: WalRecord) -> bool:
        self.events.append(("index", record.record_type))
        return True

    def index(self, name: str) -> _NamedIndex:
        assert name == "by_name"
        return self.named


class _PoolDouble:
    """Expose codec/storage preflight and the explicit flush door used by CommitRedo."""

    def __init__(self) -> None:
        self.flush_calls: list[str] = []
        self.codec = _CodecDouble()
        self.storage = _StorageDouble()

    def flush(self, file: str) -> int:
        self.flush_calls.append(file)
        return {"heap.dat": 1, "index-by_name.dat": 2}[file]


class _CodecDouble:
    """Recognise the one synthetic page image used by the dispatcher-order tests."""

    def __init__(self) -> None:
        self.decode_calls = 0

    def decode_page(self, image: bytes, *, verify: bool = True) -> Page:
        self.decode_calls += 1
        assert verify is True
        if image != b"page-image":
            raise GrafxCorruptionDetected(
                "The synthetic page image is damaged.", field="page_image"
            )
        return Page(int(PageType.HEAP), page_size=512)


class _StorageDouble:
    """Present a one-page file without changing it during validation."""

    def exists(self, file: str) -> bool:
        assert file == "heap.dat"
        return True

    def page_count(self, file: str) -> int:
        assert file == "heap.dat"
        return 1


def _page_record(lsn: int = 1) -> WalRecord:
    return WalRecord(
        record_type=int(WalRecordType.WRITE_PAGE),
        payload=encode_page_write("heap.dat", 0, b"page-image"),
        descriptor=DESCRIPTOR,
        epoch=1,
        txn_id=7,
        lsn=lsn,
    )


def _encoded_page_record(
    stack: object,
    *,
    record_lsn: int,
    page_index: int,
    page_lsn: int,
    payload: bytes,
    compress: bool = False,
) -> WalRecord:
    codec = stack.codec  # type: ignore[attr-defined]
    image = make_page_image(
        codec,
        (payload,),
        page_index=page_index,
        page_lsn=page_lsn,
    )
    encoded = encode_page_write_record(
        "heap.dat", page_index, image, compress=compress
    )
    return WalRecord(
        record_type=int(WalRecordType.WRITE_PAGE),
        payload=encoded.payload,
        descriptor=DESCRIPTOR,
        epoch=1,
        txn_id=7,
        lsn=record_lsn,
        format_version=encoded.format_version,
        flags=encoded.flags,
    )


def _index_record(
    operation: IndexOperation,
    lsn: int,
    *,
    versioned: bool = False,
) -> WalRecord:
    change = IndexChange(
        index="by_name",
        operation=operation,
        key=b"Ada",
        ref=RecordRef(3, 4),
        csn=11 if operation is IndexOperation.REMOVE or versioned else 0,
        versioned=versioned,
    )
    return wal_record_for(change, epoch=1, txn_id=7, descriptor=DESCRIPTOR).with_lsn(
        lsn
    )


def _commit_record(lsn: int = 4) -> WalRecord:
    return WalRecord(
        record_type=int(WalRecordType.COMMIT),
        descriptor=DESCRIPTOR,
        epoch=1,
        txn_id=7,
        lsn=lsn,
    )


@pytest.mark.parametrize(
    "file",
    ["heap.dat", "catalog.dat", "index/by_name.idx", "index/Vector_1.idx"],
)
def test_redo_accepts_only_canonical_paged_data_names(file: str) -> None:
    assert is_redoable_page_file(file) is True


@pytest.mark.parametrize(
    "file",
    [
        None,
        "",
        "/heap.dat",
        "control/commit.state",
        "index\\by_name.idx",
        "index/../control/commit.state",
        "index//by_name.idx",
        "index/nested/by_name.idx",
        "index/not-an-identifier.idx",
        "index/by_name.dat",
    ],
)
def test_redo_refuses_noncanonical_or_nondata_names(file: object) -> None:
    assert is_redoable_page_file(file) is False


def test_replay_preserves_effect_order_dispatches_both_index_types_and_flushes_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, int]] = []
    pool = _PoolDouble()
    manager = _IndexManagerDouble(events)

    def apply_page(_pool: object, file: str, page: int, image: bytes) -> bool:
        assert (file, page, image) == ("heap.dat", 0, b"page-image")
        events.append(("page", int(WalRecordType.WRITE_PAGE)))
        return True

    monkeypatch.setattr(commit_redo_module, "apply_page_image", apply_page)
    redo = CommitRedo(pool, manager)  # type: ignore[arg-type]

    report = redo.replay(
        (
            _page_record(1),
            _index_record(IndexOperation.INSERT, 2),
            _index_record(IndexOperation.REMOVE, 3),
            _commit_record(4),
        )
    )

    assert events == [
        ("page", int(WalRecordType.WRITE_PAGE)),
        ("index", int(WalRecordType.INDEX_WRITE)),
        ("index", int(WalRecordType.INDEX_RECONCILE)),
    ]
    assert report.effects_replayed == 3
    assert report.page_effects_replayed == 1
    assert report.page_images_applied == 1
    assert report.index_effects_replayed == 2
    assert report.index_effects_dispatched == 2
    assert report.last_committed_lsn == 4
    assert report.touched_files == ("heap.dat", "index-by_name.dat")
    assert pool.flush_calls == []

    assert redo.flush(report) == 3
    assert pool.flush_calls == ["heap.dat", "index-by_name.dat"]


def test_a_required_logical_effect_without_an_index_manager_refuses_before_pages_move(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page_calls: list[str] = []

    def apply_page(_pool: object, file: str, page: int, image: bytes) -> bool:
        page_calls.append(file)
        return True

    monkeypatch.setattr(commit_redo_module, "apply_page_image", apply_page)
    redo = CommitRedo(_PoolDouble())  # type: ignore[arg-type]

    with pytest.raises(GrafxRecoveryRefused) as refused:
        redo.replay(
            (
                _page_record(1),
                _index_record(IndexOperation.INSERT, 2),
                _commit_record(3),
            )
        )

    assert refused.value.details == {"field": "index_manager", "lsn": 2}
    assert page_calls == []


def test_multiple_required_logical_effects_without_an_index_manager_refuse_typed() -> (
    None
):
    """A later logical effect cannot turn the preflight refusal into an assertion failure."""
    redo = CommitRedo(_PoolDouble())  # type: ignore[arg-type]

    with pytest.raises(GrafxRecoveryRefused) as refused:
        redo.replay(
            (
                _index_record(IndexOperation.INSERT, 1),
                _index_record(IndexOperation.REMOVE, 2),
                _commit_record(3),
            )
        )

    assert refused.value.details == {"field": "index_manager", "lsn": 1}


def test_an_unexpected_effect_type_refuses_before_an_earlier_page_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page_calls: list[str] = []

    def apply_page(_pool: object, file: str, page: int, image: bytes) -> bool:
        page_calls.append(file)
        return True

    monkeypatch.setattr(commit_redo_module, "apply_page_image", apply_page)
    replay = CommittedReplay(
        effects=(_page_record(1), _commit_record(2)), last_committed_lsn=2
    )

    with pytest.raises(GrafxRecoveryRefused) as refused:
        CommitRedo(_PoolDouble()).apply(replay)  # type: ignore[arg-type]

    assert refused.value.details["field"] == "record_type"
    assert refused.value.details["value"] == int(WalRecordType.COMMIT)
    assert page_calls == []


def test_payload_corruption_stops_the_pass_instead_of_skipping_to_the_next_effect() -> (
    None
):
    events: list[tuple[str, int]] = []
    manager = _IndexManagerDouble(events)
    damaged = WalRecord(
        record_type=int(WalRecordType.WRITE_PAGE),
        payload=b"",
        descriptor=DESCRIPTOR,
        epoch=1,
        txn_id=7,
        lsn=1,
    )
    replay = CommittedReplay(
        effects=(damaged, _index_record(IndexOperation.INSERT, 2)),
        last_committed_lsn=3,
    )

    with pytest.raises(GrafxCorruptionDetected):
        CommitRedo(_PoolDouble(), manager).apply(replay)  # type: ignore[arg-type]

    assert events == []


def test_a_late_damaged_page_image_refuses_before_an_earlier_page_moves(
    memory_device: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = build_stack(memory_device)
    valid = WalRecord(
        record_type=int(WalRecordType.WRITE_PAGE),
        payload=encode_page_write(
            "heap.dat",
            3,
            make_page_image(stack.codec, (b"valid-prefix",), page_index=3),
        ),
        descriptor=DESCRIPTOR,
        epoch=1,
        txn_id=7,
        lsn=1,
    )
    damaged = WalRecord(
        record_type=int(WalRecordType.WRITE_PAGE),
        payload=encode_page_write("heap.dat", 4, b"not-a-page"),
        descriptor=DESCRIPTOR,
        epoch=1,
        txn_id=7,
        lsn=2,
    )
    replay = CommittedReplay(
        effects=(valid, damaged),
        last_committed_lsn=3,
    )
    page_calls: list[int] = []

    def apply_page(_pool: object, _file: str, page_index: int, _image: bytes) -> bool:
        page_calls.append(page_index)
        return True

    monkeypatch.setattr(commit_redo_module, "apply_page_image", apply_page)

    with pytest.raises(GrafxCorruptionDetected) as refused:
        CommitRedo(stack.pool).apply(replay)

    assert refused.value.details["file"] == "heap.dat"
    assert refused.value.details["page"] == 4
    assert page_calls == []


def test_a_late_index_visibility_mismatch_refuses_before_a_page_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page_calls: list[int] = []
    manager = _IndexManagerDouble([])
    replay = CommittedReplay(
        effects=(
            _page_record(1),
            _index_record(IndexOperation.INSERT, 2, versioned=True),
        ),
        last_committed_lsn=3,
    )

    def apply_page(_pool: object, _file: str, page_index: int, _image: bytes) -> bool:
        page_calls.append(page_index)
        return True

    monkeypatch.setattr(commit_redo_module, "apply_page_image", apply_page)

    with pytest.raises(GrafxCorruptionDetected) as refused:
        CommitRedo(_PoolDouble(), manager).apply(replay)  # type: ignore[arg-type]

    assert refused.value.details["field"] == "versioned"
    assert refused.value.details["index"] == "by_name"
    assert page_calls == []


def test_a_late_impossible_redo_gap_refuses_before_an_earlier_page_moves(
    memory_device: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = build_stack(memory_device)
    present = stack.storage.page_count("heap.dat")  # type: ignore[attr-defined]
    first_index = present
    impossible_index = first_index + 1 + MAX_REDO_GAP_PAGES
    first = WalRecord(
        record_type=int(WalRecordType.WRITE_PAGE),
        payload=encode_page_write(
            "heap.dat",
            first_index,
            make_page_image(stack.codec, (b"prefix",), page_index=first_index),
        ),
        descriptor=DESCRIPTOR,
        epoch=1,
        txn_id=7,
        lsn=1,
    )
    impossible = WalRecord(
        record_type=int(WalRecordType.WRITE_PAGE),
        payload=encode_page_write(
            "heap.dat",
            impossible_index,
            make_page_image(
                stack.codec,
                (b"too-far",),
                page_index=impossible_index,
            ),
        ),
        descriptor=DESCRIPTOR,
        epoch=1,
        txn_id=7,
        lsn=2,
    )
    page_calls: list[int] = []

    def apply_page(_pool: object, _file: str, page_index: int, _image: bytes) -> bool:
        page_calls.append(page_index)
        return True

    monkeypatch.setattr(commit_redo_module, "apply_page_image", apply_page)

    with pytest.raises(GrafxCorruptionDetected) as refused:
        CommitRedo(stack.pool).apply(
            CommittedReplay(
                effects=(first, impossible),
                last_committed_lsn=3,
            )
        )

    assert refused.value.details["field"] == "page_index"
    assert refused.value.details["page"] == impossible_index
    assert page_calls == []


def test_reapplying_a_page_effect_is_an_idempotent_no_op(memory_device: object) -> None:
    stack = build_stack(memory_device)
    image = make_page_image(stack.codec, (b"durable",), page_index=0, page_lsn=7)
    effect = WalRecord(
        record_type=int(WalRecordType.WRITE_PAGE),
        payload=encode_page_write("heap.dat", 0, image),
        descriptor=DESCRIPTOR,
        epoch=1,
        txn_id=8,
        lsn=7,
    )
    replay = CommittedReplay(effects=(effect,), last_committed_lsn=8)
    redo = CommitRedo(stack.pool)

    first = redo.apply(replay)
    second = redo.apply(replay)

    assert first.page_images_applied == 1
    assert first.touched_files == ("heap.dat",)
    assert second.effects_replayed == 1
    assert second.page_images_applied == 0
    assert second.touched_files == ("heap.dat",)
    assert redo.flush(first) == 1
    persisted = stack.codec.decode_page(
        stack.storage.read_page("heap.dat", 0),
        page_index=0,  # type: ignore[attr-defined]
    )
    assert persisted.page_lsn == 7
    assert persisted.read_slot(0) == b"durable"


def test_reapplying_a_compressed_page_effect_is_an_idempotent_no_op(
    memory_device: object,
) -> None:
    stack = build_stack(memory_device)
    image = make_page_image(stack.codec, (b"compressed",), page_index=0, page_lsn=7)
    encoded = encode_page_write_record("heap.dat", 0, image, compress=True)
    assert encoded.compressed is True
    effect = WalRecord(
        record_type=int(WalRecordType.WRITE_PAGE),
        payload=encoded.payload,
        descriptor=DESCRIPTOR,
        epoch=1,
        txn_id=8,
        lsn=7,
        format_version=encoded.format_version,
        flags=encoded.flags,
    )
    replay = CommittedReplay(effects=(effect,), last_committed_lsn=8)
    redo = CommitRedo(stack.pool)

    first = redo.apply(replay)
    second = redo.apply(replay)

    assert first.page_images_applied == 1
    assert second.page_images_applied == 0
    assert redo.flush(first) == 1
    persisted = stack.codec.decode_page(
        stack.storage.read_page("heap.dat", 0),
        page_index=0,  # type: ignore[attr-defined]
    )
    assert persisted.page_lsn == 7
    assert persisted.read_slot(0) == b"compressed"


def test_page_only_redo_coalesces_repeated_locations_at_their_first_position(
    memory_device: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = build_stack(memory_device)
    first_a = _encoded_page_record(
        stack, record_lsn=1, page_index=0, page_lsn=4, payload=b"old-a"
    )
    only_b = _encoded_page_record(
        stack, record_lsn=2, page_index=1, page_lsn=5, payload=b"only-b"
    )
    final_a = _encoded_page_record(
        stack,
        record_lsn=3,
        page_index=0,
        page_lsn=6,
        payload=b"final-a",
        compress=True,
    )
    calls: list[tuple[int, bytes]] = []

    def apply_page(_pool: object, _file: str, page: int, image: bytes) -> bool:
        decoded = stack.codec.decode_page(image, verify=True)
        calls.append((page, decoded.read_slot(0)))
        return True

    monkeypatch.setattr(commit_redo_module, "apply_page_image", apply_page)

    report = CommitRedo(stack.pool).apply(
        CommittedReplay(
            effects=(first_a, only_b, final_a),
            last_committed_lsn=7,
        )
    )

    assert calls == [(0, b"final-a"), (1, b"only-b")]
    assert report.effects_replayed == 3
    assert report.page_effects_replayed == 3
    assert report.page_images_applied == 2
    assert report.touched_files == ("heap.dat",)


def test_a_passage_bound_preflight_reuses_the_prepared_page_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _PoolDouble()
    redo = CommitRedo(pool)  # type: ignore[arg-type]
    replay = CommittedReplay(effects=(_page_record(),), last_committed_lsn=1)
    passage = object()
    proof = redo.preflight(replay, _passage=passage)
    calls: list[int] = []
    monkeypatch.setattr(
        commit_redo_module,
        "apply_page_image",
        lambda _pool, _file, page, _image: calls.append(page) or True,
    )

    result = redo.apply(replay, _preflighted=proof, _passage=passage)

    assert pool.codec.decode_calls == 1
    assert calls == [0]
    assert result.page_images_applied == 1


@pytest.mark.parametrize(
    "mismatch", ["replay", "passage", "mode", "forged", "mutated"]
)
def test_an_incompatible_or_forged_preflight_revalidates_normally(
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    pool = _PoolDouble()
    redo = CommitRedo(pool)  # type: ignore[arg-type]
    first = CommittedReplay(effects=(_page_record(1),), last_committed_lsn=1)
    second = CommittedReplay(effects=(_page_record(2),), last_committed_lsn=2)
    passage = object()
    proof = redo.preflight(
        first,
        allow_unregistered_indexes=mismatch == "mode",
        _passage=passage,
    )
    monkeypatch.setattr(commit_redo_module, "apply_page_image", lambda *_args: True)
    replay = second if mismatch == "replay" else first
    offered_passage = object() if mismatch == "passage" else passage
    offered_proof = object() if mismatch == "forged" else proof
    if mismatch == "mutated":
        object.__setattr__(
            first.effects[0],
            "payload",
            encode_page_write("heap.dat", 0, bytes(bytearray(b"page-image"))),
        )

    redo.apply(
        replay,
        _preflighted=offered_proof,
        _passage=offered_passage,
    )

    assert pool.codec.decode_calls == 2


def test_a_page_projection_from_a_mixed_replay_preserves_sequential_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _PoolDouble()
    manager = _IndexManagerDouble([])
    redo = CommitRedo(pool, manager)  # type: ignore[arg-type]
    first = _page_record(1)
    final = _page_record(3)
    mixed = CommittedReplay(
        effects=(first, _index_record(IndexOperation.INSERT, 2), final),
        last_committed_lsn=3,
    )
    pages = CommittedReplay(effects=(first, final), last_committed_lsn=3)
    passage = object()
    full = redo.preflight(mixed, _passage=passage)
    projected = redo._project_page_preflight(
        mixed,
        pages,
        full,
        allow_unregistered_indexes=False,
        passage=passage,
    )
    calls: list[int] = []
    monkeypatch.setattr(
        commit_redo_module,
        "apply_page_image",
        lambda _pool, _file, page, _image: calls.append(page) or True,
    )

    assert projected is not None
    redo.apply(pages, _preflighted=projected, _passage=passage)

    assert pool.codec.decode_calls == 2
    assert calls == [0, 0]


@pytest.mark.parametrize(
    ("first_lsn", "second_lsn", "first_payload", "second_payload"),
    (
        (9, 8, b"newer", b"regressed"),
        (9, 9, b"first-bytes", b"different-bytes"),
    ),
    ids=("page-lsn-regression", "equal-lsn-different-image"),
)
def test_page_only_redo_keeps_an_ambiguous_location_sequential(
    memory_device: object,
    monkeypatch: pytest.MonkeyPatch,
    first_lsn: int,
    second_lsn: int,
    first_payload: bytes,
    second_payload: bytes,
) -> None:
    stack = build_stack(memory_device)
    first = _encoded_page_record(
        stack,
        record_lsn=1,
        page_index=0,
        page_lsn=first_lsn,
        payload=first_payload,
    )
    second = _encoded_page_record(
        stack,
        record_lsn=2,
        page_index=0,
        page_lsn=second_lsn,
        payload=second_payload,
    )
    calls: list[bytes] = []

    def apply_page(_pool: object, _file: str, _page: int, image: bytes) -> bool:
        calls.append(stack.codec.decode_page(image, verify=True).read_slot(0))
        return True

    monkeypatch.setattr(commit_redo_module, "apply_page_image", apply_page)

    report = CommitRedo(stack.pool).apply(
        CommittedReplay(effects=(first, second), last_committed_lsn=3)
    )

    assert calls == [first_payload, second_payload]
    assert report.page_images_applied == 2


def test_mixed_page_and_index_redo_does_not_coalesce_page_effects(
    memory_device: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = build_stack(memory_device)
    first = _encoded_page_record(
        stack, record_lsn=1, page_index=0, page_lsn=4, payload=b"first"
    )
    final = _encoded_page_record(
        stack, record_lsn=3, page_index=0, page_lsn=6, payload=b"final"
    )
    events: list[tuple[str, object]] = []
    manager = _IndexManagerDouble(events)  # type: ignore[arg-type]

    def apply_page(_pool: object, _file: str, _page: int, image: bytes) -> bool:
        events.append(("page", stack.codec.decode_page(image).read_slot(0)))
        return True

    monkeypatch.setattr(commit_redo_module, "apply_page_image", apply_page)

    report = CommitRedo(stack.pool, manager).apply(
        CommittedReplay(
            effects=(first, _index_record(IndexOperation.INSERT, 2), final),
            last_committed_lsn=4,
        )
    )

    assert events == [
        ("page", b"first"),
        ("index", int(WalRecordType.INDEX_WRITE)),
        ("page", b"final"),
    ]
    assert report.page_images_applied == 2
    assert report.index_effects_dispatched == 1


def test_a_corrupt_superseded_image_still_refuses_before_coalesced_redo_mutates(
    memory_device: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = build_stack(memory_device)
    first = _encoded_page_record(
        stack, record_lsn=1, page_index=0, page_lsn=4, payload=b"first"
    )
    damaged = WalRecord(
        record_type=int(WalRecordType.WRITE_PAGE),
        payload=encode_page_write("heap.dat", 0, b"damaged-superseded-image"),
        descriptor=DESCRIPTOR,
        epoch=1,
        txn_id=7,
        lsn=2,
    )
    final = _encoded_page_record(
        stack, record_lsn=3, page_index=0, page_lsn=6, payload=b"final"
    )
    calls: list[int] = []

    def apply_page(_pool: object, _file: str, page: int, _image: bytes) -> bool:
        calls.append(page)
        return True

    monkeypatch.setattr(commit_redo_module, "apply_page_image", apply_page)

    with pytest.raises(GrafxCorruptionDetected):
        CommitRedo(stack.pool).apply(
            CommittedReplay(effects=(first, damaged, final), last_committed_lsn=4)
        )

    assert calls == []


def test_corrupted_compressed_body_refuses_before_any_page_mutation(
    memory_device: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = build_stack(memory_device)
    image = make_page_image(stack.codec, (b"valid",), page_index=0, page_lsn=7)
    encoded = encode_page_write_record("heap.dat", 0, image, compress=True)
    damaged = encoded.payload[:-3] + b"bad"
    effect = WalRecord(
        record_type=int(WalRecordType.WRITE_PAGE),
        payload=damaged,
        descriptor=DESCRIPTOR,
        epoch=1,
        txn_id=8,
        lsn=7,
        format_version=encoded.format_version,
        flags=encoded.flags,
    )
    calls: list[int] = []

    def apply_page(_pool: object, _file: str, page_index: int, _image: bytes) -> bool:
        calls.append(page_index)
        return True

    monkeypatch.setattr(commit_redo_module, "apply_page_image", apply_page)

    with pytest.raises(GrafxCorruptionDetected):
        CommitRedo(stack.pool).apply(
            CommittedReplay(effects=(effect,), last_committed_lsn=8)
        )

    assert calls == []
