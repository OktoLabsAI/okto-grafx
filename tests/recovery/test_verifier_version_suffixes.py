"""Full verification must not rewalk every suffix of an unchanged MVCC history."""

from dataclasses import replace

import pytest

from okto_grafx.domain.errors import GrafxCorruptionDetected
from okto_grafx.domain.ids import RecordRef
from okto_grafx import connect
from okto_grafx.domain.model.record import RECORD_HEADER_SIZE, RecordHeader
from okto_grafx.domain.verify.findings import FindingKind
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine import verifier as verifier_module

from .conftest import HEAP_FILE, Stack
from .test_verifier import _populate, _rewrite_page


def _history(stack: Stack, size: int = 12):
    table = _populate(stack, rows=1)
    refs = [next(stack.heap.scan_all(table))[0]]
    for step in range(1, size):
        refs.append(stack.heap.update(table, refs[-1], (1, f"v{step}"), xmin=step + 1))
    stack.pool.flush()
    return table, refs


def _previous(stack: Stack, ref: RecordRef, previous: RecordRef) -> None:
    def mutate(page):
        content = page.read_slot(ref.slot)
        header = RecordHeader.decode(content)
        page.update_slot(
            ref.slot,
            replace(header, prev_version=previous.encode()).encode()
            + content[RECORD_HEADER_SIZE:],
        )

    _rewrite_page(stack, HEAP_FILE, ref.page, mutate)


def _assert_matches_full(stack: Stack, monkeypatch: pytest.MonkeyPatch):
    optimized = stack.verifier().verify("all")
    with monkeypatch.context() as patch:
        patch.setattr(verifier_module, "_CANONICAL_VERSION_CHAIN", None)
        full = stack.verifier().verify("all")
    assert optimized == full
    return optimized


@pytest.mark.parametrize("size", [32, 128])
def test_healthy_history_has_linear_walk_work(stack: Stack, monkeypatch, size):
    _table, refs = _history(stack, size)
    walked = []
    original = HeapStore._walk_version_chain

    def counted(self, ref, verified=None):
        result = original(self, ref, verified)
        if verified is not None:
            walked.extend(result)
        return result

    monkeypatch.setattr(HeapStore, "_walk_version_chain", counted)
    report = _assert_matches_full(stack, monkeypatch)
    assert not report.findings
    assert report.records_checked == size
    assert len(walked) == size
    assert set(walked) == set(refs)
    # The optimization must not truncate the public API's returned history.
    assert stack.heap.version_chain(refs[-1]) == tuple(reversed(refs))


@pytest.mark.parametrize(
    "damage",
    ["cycle", "self_cycle", "missing_page", "descriptor", "free_slot", "short_header"],
)
def test_corrupt_suffixes_keep_every_finding(stack: Stack, monkeypatch, damage):
    _table, refs = _history(stack)
    if damage == "cycle":
        _previous(stack, refs[0], refs[5])
    elif damage == "self_cycle":
        _previous(stack, refs[0], refs[0])
    elif damage == "missing_page":
        _previous(stack, refs[0], RecordRef(999999, 1))
    elif damage == "descriptor":
        _previous(stack, refs[0], RecordRef(refs[0].page, 0))
    elif damage == "free_slot":
        _rewrite_page(
            stack, HEAP_FILE, refs[0].page, lambda page: page.free_slot(refs[0].slot)
        )
    else:
        _rewrite_page(
            stack,
            HEAP_FILE,
            refs[0].page,
            lambda page: page.update_slot(refs[0].slot, b"bad"),
        )
    report = _assert_matches_full(stack, monkeypatch)
    assert report.findings_of(FindingKind.VERSION_CHAIN)


def test_reused_verifier_does_not_retain_success_after_damage(stack: Stack):
    _table, refs = _history(stack)
    verifier = stack.verifier()
    assert not verifier.verify("all").findings
    _previous(stack, refs[0], refs[3])
    assert verifier.verify("all").findings_of(FindingKind.VERSION_CHAIN)


def test_custom_chain_hook_keeps_one_call_per_stored_predecessor(
    stack: Stack, monkeypatch
):
    _table, refs = _history(stack)
    calls = []
    original = HeapStore.version_chain

    def observed(self, ref):
        calls.append(ref)
        return original(self, ref)

    monkeypatch.setattr(HeapStore, "version_chain", observed)
    assert not stack.verifier().verify("all").findings
    assert calls == refs[1:]


def test_failed_walk_cannot_certify_a_partial_suffix(stack: Stack):
    _table, refs = _history(stack)
    _previous(stack, refs[0], refs[2])
    verified = {}
    with pytest.raises(GrafxCorruptionDetected):
        stack.heap._walk_version_chain(refs[-1], verified)
    assert verified == {}


def test_reused_suffix_still_obeys_finite_walk_bound(stack: Stack):
    _table, refs = _history(stack)
    verified = {refs[0].encode(): 10**9}
    with pytest.raises(GrafxCorruptionDetected) as raised:
        stack.heap._walk_version_chain(refs[-1], verified)
    assert raised.value.details["field"] == "chain_length"
    assert verified == {refs[0].encode(): 10**9}


def test_newest_first_walk_shares_the_entire_successful_suffix(stack: Stack):
    _table, refs = _history(stack)
    verified = {}
    assert stack.heap._walk_version_chain(refs[-1], verified) == tuple(reversed(refs))
    assert verified == {ref.encode(): index + 1 for index, ref in enumerate(refs)}
    for ref in reversed(refs):
        assert stack.heap._walk_version_chain(ref, verified) == ()


def test_custom_slot_reader_keeps_the_original_chain_protocol(stack: Stack, monkeypatch):
    _table, refs = _history(stack)
    calls = []
    original = HeapStore._read_slot

    def observed(self, ref):
        calls.append(ref)
        return original(self, ref)

    monkeypatch.setattr(HeapStore, "_read_slot", observed)
    assert not stack.verifier().verify("records").findings
    assert len(calls) == sum(range(2, len(refs) + 1))


def test_read_only_verification_refreshes_after_foreign_history_update(tmp_path):
    root = tmp_path / "history"
    with connect(root) as writer:
        with writer.begin("write") as transaction:
            transaction.execute("CREATE NODE TABLE P(id INT64, v INT64, PRIMARY KEY(id))")
            transaction.execute("CREATE (:P {id: 1, v: 0})")
        for value in range(1, 4):
            with writer.begin("write") as transaction:
                transaction.execute("MATCH (p:P {id: 1}) SET p.v = $v", {"v": value})
        writer.checkpoint()
        with connect(root, read_only=True) as reader:
            before = reader.verify("all")
            assert not before.findings
            assert before.records_checked == 4
            with writer.begin("write") as transaction:
                transaction.execute("MATCH (p:P {id: 1}) SET p.v = 4")
            writer.checkpoint()
            after = reader.verify("all")
            assert not after.findings
            assert after.records_checked == 5
            assert reader.execute("MATCH (p:P) RETURN p.v").rows == ((4,),)
