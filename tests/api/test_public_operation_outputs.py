"""Operational facade results are detached values, never collaborator capabilities."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from okto_grafx import connect
from okto_grafx.adapters.metrics_contained import ContainedMetricsSink
from okto_grafx.domain.errors import GrafxConfigurationError, GrafxIndexError
from okto_grafx.domain.ids import RecordRef
from okto_grafx.domain.index.entry import IndexEntry
from okto_grafx.domain.verify.findings import (
    FindingLocation,
    VerificationFinding,
    VerificationReport,
)
from okto_grafx.domain.wal.replay import RecycleReport
from okto_grafx.engine.index_manager import IndexManager, IndexStore
from okto_grafx.engine.public_views import MetricsSnapshotView
from okto_grafx.engine.txn_manager import TransactionManager
from okto_grafx.engine.vector_engine import VectorEngine, VectorHit, VectorSearchResult
from okto_grafx.engine.verifier import Verifier


def _calls() -> dict[str, int]:
    return {"attribute": 0, "call": 0, "convert": 0, "repr": 0}


class _Capability:
    _counts: dict[str, int]

    def __call__(self) -> None:
        self._counts["call"] += 1
        raise AssertionError("a public result retained or invoked a callback")


class _HostileText(_Capability, str):
    def __new__(cls, value: str, counts: dict[str, int]) -> _HostileText:
        instance = str.__new__(cls, value)
        instance._counts = counts
        return instance

    def __str__(self) -> str:
        self._counts["convert"] += 1
        raise AssertionError("hostile __str__ ran")

    def __repr__(self) -> str:
        self._counts["repr"] += 1
        raise AssertionError("hostile __repr__ ran")


class _HostileInt(_Capability, int):
    def __new__(cls, value: int, counts: dict[str, int]) -> _HostileInt:
        instance = int.__new__(cls, value)
        instance._counts = counts
        return instance

    def __int__(self) -> int:
        self._counts["convert"] += 1
        raise AssertionError("hostile __int__ ran")

    def __repr__(self) -> str:
        self._counts["repr"] += 1
        raise AssertionError("hostile __repr__ ran")


class _HostileFloat(_Capability, float):
    def __new__(cls, value: float, counts: dict[str, int]) -> _HostileFloat:
        instance = float.__new__(cls, value)
        instance._counts = counts
        return instance

    def __float__(self) -> float:
        self._counts["convert"] += 1
        raise AssertionError("hostile __float__ ran")

    def __repr__(self) -> str:
        self._counts["repr"] += 1
        raise AssertionError("hostile __repr__ ran")


class _HostileBytes(_Capability, bytes):
    def __new__(cls, value: bytes, counts: dict[str, int]) -> _HostileBytes:
        instance = bytes.__new__(cls, value)
        instance._counts = counts
        return instance

    def __bytes__(self) -> bytes:
        self._counts["convert"] += 1
        raise AssertionError("hostile __bytes__ ran")


class _HostileReport(VerificationReport):
    __slots__ = ("_armed", "_counts")

    def __getattribute__(self, name: str) -> object:
        if name in {
            "scope",
            "findings",
            "pages_checked",
            "records_checked",
            "index_entries_checked",
            "files_checked",
        }:
            try:
                armed = object.__getattribute__(self, "_armed")
            except AttributeError:
                armed = False
            if armed:
                counts = object.__getattribute__(self, "_counts")
                counts["attribute"] += 1
                raise AssertionError("report subclass field callback ran")
        return object.__getattribute__(self, name)


class _HostileEntry(IndexEntry):
    __slots__ = ("_armed", "_counts")

    def __getattribute__(self, name: str) -> object:
        if name in {"key", "ref", "versioned", "born_csn", "dead_csn", "page", "slot"}:
            try:
                armed = object.__getattribute__(self, "_armed")
            except AttributeError:
                armed = False
            if armed:
                counts = object.__getattribute__(self, "_counts")
                counts["attribute"] += 1
                raise AssertionError("index entry subclass field callback ran")
        return object.__getattribute__(self, name)


def test_metrics_snapshot_is_a_deeply_frozen_detached_builtin_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutating the sink's nested graph cannot rewrite a previously returned observation."""
    calls = _calls()
    labels = {_HostileText("role", calls): _HostileText("writer", calls)}
    sample = {"labels": labels, "value": _HostileFloat(3.5, calls)}
    samples = [sample]
    metric = {"kind": _HostileText("counter", calls), "samples": samples}
    source = {_HostileText("oktografx_test_total", calls): metric}

    def hostile_snapshot(_sink: ContainedMetricsSink) -> object:
        return source

    with connect(":memory:") as database:
        monkeypatch.setattr(ContainedMetricsSink, "snapshot", hostile_snapshot)
        observed = database.snapshot_metrics()

    assert type(observed) is MetricsSnapshotView
    frozen_metric = observed["oktografx_test_total"]
    assert type(frozen_metric) is MetricsSnapshotView
    frozen_sample = frozen_metric["samples"][0]  # type: ignore[index]
    assert type(frozen_sample) is MetricsSnapshotView
    assert type(frozen_sample["labels"]) is MetricsSnapshotView
    assert type(frozen_sample["value"]) is float
    assert frozen_sample["labels"] == {"role": "writer"}

    labels.clear()
    sample["value"] = 99.0
    samples.clear()
    metric.clear()
    source.clear()
    assert frozen_sample["labels"] == {"role": "writer"}
    assert frozen_sample["value"] == 3.5
    with pytest.raises(TypeError):
        observed["new"] = object()  # type: ignore[index]
    assert calls == _calls()


def test_metrics_snapshot_rejects_a_capability_leaf_without_invoking_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _calls()
    capability = _Capability()
    capability._counts = calls
    monkeypatch.setattr(
        ContainedMetricsSink,
        "snapshot",
        lambda _sink: {"metric": {"callback": capability}},
    )

    with connect(":memory:") as database:
        with pytest.raises(GrafxConfigurationError) as refusal:
            database.snapshot_metrics()

    assert refusal.value.details["field"] == "metrics.snapshot.metric.callback"
    assert calls == _calls()


def test_verify_rebuilds_hostile_report_subclasses_and_detaches_every_leaf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _calls()
    location = FindingLocation(
        file=_HostileText("heap.dat", calls),
        page=_HostileInt(4, calls),
        slot=_HostileInt(2, calls),
        lsn=_HostileInt(7, calls),
        index=_HostileText("p_by_id", calls),
    )
    finding = VerificationFinding(
        kind=_HostileText("record_header", calls),
        location=location,
        detail=_HostileText("bad header", calls),
    )
    raw = _HostileReport(
        scope=_HostileText("all", calls),
        findings=(finding,),
        pages_checked=_HostileInt(8, calls),
        records_checked=_HostileInt(3, calls),
        index_entries_checked=_HostileInt(1, calls),
        files_checked=(_HostileText("heap.dat", calls),),
    )
    object.__setattr__(raw, "_counts", calls)
    object.__setattr__(raw, "_armed", True)

    def hostile_verify(_verifier: Verifier, scope: str) -> VerificationReport:
        assert type(scope) is str and scope == "all"
        return raw

    monkeypatch.setattr(Verifier, "verify", hostile_verify)
    with connect(":memory:") as database:
        observed = database.verify(_HostileText("all", calls))

    assert type(observed) is VerificationReport
    assert type(observed.scope) is str
    assert type(observed.findings[0]) is VerificationFinding
    assert type(observed.findings[0].location) is FindingLocation
    assert type(observed.findings[0].location.page) is int
    assert type(observed.files_checked[0]) is str
    object.__setattr__(raw, "pages_checked", 999)
    assert observed.pages_checked == 8
    assert calls == _calls()


def test_checkpoint_rebuilds_the_recycle_report_without_filename_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _calls()
    raw = RecycleReport(
        horizon_lsn=_HostileInt(12, calls),
        recycled=(_HostileText("wal/0001.wal", calls),),
        deferred=(_HostileText("wal/0002.wal", calls),),
        retained=(_HostileText("wal/0003.wal", calls),),
        reclaimed_bytes=_HostileInt(4096, calls),
        lag_segments=_HostileInt(1, calls),
        reader_present=False,
    )
    monkeypatch.setattr(TransactionManager, "checkpoint", lambda _manager: raw)

    with connect(":memory:") as database:
        observed = database.checkpoint()

    assert type(observed) is RecycleReport
    assert type(observed.horizon_lsn) is int
    assert all(
        type(name) is str
        for name in observed.recycled + observed.deferred + observed.retained
    )
    object.__setattr__(raw, "recycled", ("changed",))
    assert observed.recycled == ("wal/0001.wal",)
    with pytest.raises(FrozenInstanceError):
        observed.recycled = ()  # type: ignore[misc]
    assert calls == _calls()


def test_inspect_index_rebuilds_hostile_entries_and_nested_record_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _calls()
    with connect(":memory:") as database:
        with database.begin("write") as schema:
            schema.execute("CREATE NODE TABLE P(id INT64, PRIMARY KEY(id))")
        index_name = database._indexes.indexes()[0].name
        raw = _HostileEntry(
            key=_HostileBytes(b"key", calls),
            ref=RecordRef(_HostileInt(3, calls), _HostileInt(2, calls)),
            versioned=False,
            born_csn=_HostileInt(0, calls),
            dead_csn=_HostileInt(0, calls),
            page=_HostileInt(4, calls),
            slot=_HostileInt(1, calls),
        )
        object.__setattr__(raw, "_counts", calls)
        object.__setattr__(raw, "_armed", True)
        original_walk = IndexStore.walk

        def hostile_walk(index: IndexStore) -> tuple[IndexEntry, ...]:
            if index.name == index_name:
                return (raw,)
            return tuple(original_walk(index))

        monkeypatch.setattr(IndexStore, "walk", hostile_walk)
        observed = database.inspect_index(_HostileText(index_name, calls))

    assert type(observed) is tuple
    assert type(observed[0]) is IndexEntry
    assert type(observed[0].key) is bytes
    assert type(observed[0].ref) is RecordRef
    assert type(observed[0].ref.page) is int
    object.__setattr__(raw, "key", b"changed")
    assert observed[0].key == b"key"
    assert calls == _calls()


def test_search_vectors_canonicalizes_k_and_rebuilds_finite_scores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _calls()
    raw_hit = VectorHit(
        record_id=_HostileInt(7, calls),
        score=_HostileFloat(0.75, calls),
        ref=RecordRef(_HostileInt(5, calls), _HostileInt(1, calls)),
        retired=False,
    )
    raw = VectorSearchResult(
        hits=(raw_hit,),
        regime=_HostileText("exact", calls),
        achieved_k=_HostileInt(1, calls),
        requested_k=_HostileInt(3, calls),
        space=_HostileText("s", calls),
        filter_cardinality=_HostileInt(1, calls),
    )

    def hostile_search(
        _vectors: VectorEngine, **arguments: object
    ) -> VectorSearchResult:
        assert type(arguments["space"]) is str
        assert type(arguments["k"]) is int
        return raw

    monkeypatch.setattr(VectorEngine, "search", hostile_search)
    with connect(":memory:") as database:
        reader = database.begin("read")
        observed = database.search_vectors(
            reader,
            space=_HostileText("s", calls),
            query=(1.0,),
            k=_HostileInt(3, calls),
        )
        reader.rollback()

    assert type(observed) is VectorSearchResult
    assert type(observed.hits[0]) is VectorHit
    assert type(observed.hits[0].score) is float
    assert type(observed.hits[0].record_id) is int
    assert type(observed.hits[0].ref) is RecordRef
    object.__setattr__(raw_hit, "score", 99.0)
    assert observed.hits[0].score == 0.75
    assert calls == _calls()


@pytest.mark.parametrize("bad_k", (True, False, 0, -1, 1.5, "1"))
def test_search_vectors_refuses_an_invalid_k_before_entering_the_vector_engine(
    monkeypatch: pytest.MonkeyPatch, bad_k: object
) -> None:
    reached = False

    def should_not_search(_vectors: VectorEngine, **_arguments: object) -> object:
        nonlocal reached
        reached = True
        raise AssertionError("invalid k reached the vector collaborator")

    monkeypatch.setattr(VectorEngine, "search", should_not_search)
    with connect(":memory:") as database:
        reader = database.begin("read")
        with pytest.raises(GrafxConfigurationError) as refusal:
            database.search_vectors(
                reader,
                space="s",
                query=(1.0,),
                k=bad_k,  # type: ignore[arg-type]
            )
        reader.rollback()

    assert refusal.value.details["field"] == "k"
    assert reached is False


@pytest.mark.parametrize("score", (float("nan"), float("inf"), float("-inf")))
def test_search_vectors_refuses_non_finite_scores(
    monkeypatch: pytest.MonkeyPatch, score: float
) -> None:
    result = VectorSearchResult(
        hits=(VectorHit(1, score, RecordRef(1, 1), False),),
        regime="exact",
        achieved_k=1,
        requested_k=1,
        space="s",
        filter_cardinality=None,
    )
    monkeypatch.setattr(VectorEngine, "search", lambda _vectors, **_arguments: result)

    with connect(":memory:") as database:
        reader = database.begin("read")
        with pytest.raises(GrafxConfigurationError) as refusal:
            database.search_vectors(reader, space="s", query=(1.0,), k=1)
        reader.rollback()

    assert refusal.value.details["field"] == "vector.hit.score"


@pytest.mark.parametrize(
    ("door", "collaborator"),
    (
        ("snapshot_metrics", (ContainedMetricsSink, "snapshot")),
        ("verify", (Verifier, "verify")),
        ("checkpoint", (TransactionManager, "checkpoint")),
        ("inspect_index", (IndexManager, "index")),
        ("search_vectors", (VectorEngine, "search")),
    ),
)
@pytest.mark.parametrize("failure_kind", ("ordinary", "grafx", "signal"))
def test_each_public_operation_contains_only_ordinary_collaborator_failures(
    monkeypatch: pytest.MonkeyPatch,
    door: str,
    collaborator: tuple[type[object], str],
    failure_kind: str,
) -> None:
    """Grafx identity and process-control signals survive; plain exceptions become typed."""
    failure: BaseException
    if failure_kind == "ordinary":
        failure = RuntimeError("ordinary collaborator failure")
        expected: type[BaseException] = GrafxConfigurationError
    elif failure_kind == "grafx":
        failure = GrafxIndexError("typed collaborator failure")
        expected = GrafxIndexError
    else:
        failure = KeyboardInterrupt("process signal")
        expected = KeyboardInterrupt

    def explode(*_args: object, **_kwargs: object) -> object:
        raise failure

    monkeypatch.setattr(collaborator[0], collaborator[1], explode)
    with connect(":memory:") as database:
        reader = database.begin("read") if door == "search_vectors" else None
        operation = {
            "snapshot_metrics": database.snapshot_metrics,
            "verify": lambda: database.verify("all"),
            "checkpoint": database.checkpoint,
            "inspect_index": lambda: database.inspect_index("missing"),
            "search_vectors": lambda: database.search_vectors(
                reader,
                space="s",
                query=(1.0,),
                k=1,  # type: ignore[arg-type]
            ),
        }[door]
        with pytest.raises(expected) as raised:
            operation()
        if failure_kind != "ordinary":
            assert raised.value is failure
        else:
            assert raised.value.details["field"] == door  # type: ignore[attr-defined]
            assert raised.value.__cause__ is failure
        if reader is not None:
            reader.rollback()
