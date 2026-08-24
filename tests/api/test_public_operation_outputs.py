"""Operational facade results are detached values, never collaborator capabilities."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from okto_grafx import connect
from okto_grafx.adapters.metrics_contained import ContainedMetricsSink
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxIndexError,
    GrafxTransactionStateError,
)
from okto_grafx.domain.ids import MAX_PAGE_INDEX, MAX_SLOT_ID, RecordRef
from okto_grafx.domain.index.entry import IndexEntry
from okto_grafx.domain.model.value import VectorValue
from okto_grafx.domain.page.layout import MAX_U64
from okto_grafx.domain.vector.filter import RecordIdFilter
from okto_grafx.domain.verify.findings import (
    FindingKind,
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
        retained=(
            _HostileText("wal/0002.wal", calls),
            _HostileText("wal/0003.wal", calls),
        ),
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
            candidate_filter=RecordIdFilter.of({7}),
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


def test_search_vectors_detaches_vector_value_and_filter_before_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forged subclasses and hostile collection overrides never reach the engine."""
    calls = _calls()

    class HostileVector(VectorValue):
        __slots__ = ("_armed",)

        def __getattribute__(self, name: str) -> object:
            if name in {"values", "space_ref", "dtype"}:
                try:
                    armed = object.__getattribute__(self, "_armed")
                except AttributeError:
                    armed = False
                if armed:
                    calls["attribute"] += 1
                    raise AssertionError("query field override ran")
            return object.__getattribute__(self, name)

    class HostileFrozenSet(frozenset[int]):
        def __iter__(self):  # noqa: ANN204 - deliberately hostile built-in override
            calls["call"] += 1
            raise AssertionError("filter iterator override ran")

        def __len__(self) -> int:
            calls["call"] += 1
            raise AssertionError("filter length override ran")

    query = HostileVector(values=(1.0,), space_ref=1, dtype="float32")
    object.__setattr__(query, "values", (_HostileFloat(1.25, calls),))
    object.__setattr__(query, "space_ref", _HostileInt(1, calls))
    object.__setattr__(query, "dtype", _HostileText("float32", calls))
    object.__setattr__(query, "_armed", True)
    candidate_filter = RecordIdFilter.of({7})
    object.__setattr__(
        candidate_filter,
        "record_ids",
        HostileFrozenSet((_HostileInt(7, calls),)),
    )

    def inspect_search(
        _vectors: VectorEngine, **arguments: object
    ) -> VectorSearchResult:
        observed_query = arguments["query"]
        observed_filter = arguments["candidate_filter"]
        assert type(observed_query) is VectorValue
        assert type(observed_query.values) is tuple  # type: ignore[union-attr]
        assert observed_query.values == (1.25,)  # type: ignore[union-attr]
        assert type(observed_query.values[0]) is float  # type: ignore[union-attr]
        assert type(observed_query.space_ref) is int  # type: ignore[union-attr]
        assert type(observed_query.dtype) is str  # type: ignore[union-attr]
        assert type(observed_filter) is RecordIdFilter
        assert type(observed_filter.record_ids) is frozenset  # type: ignore[union-attr]
        assert all(type(identifier) is int for identifier in observed_filter.record_ids)  # type: ignore[union-attr]
        assert type(arguments["snapshot"]).__name__ == "Snapshot"
        return VectorSearchResult(
            hits=(VectorHit(7, 1.0, RecordRef(1, 1), False),),
            regime="exact",
            achieved_k=1,
            requested_k=1,
            space="s",
            filter_cardinality=1,
        )

    monkeypatch.setattr(VectorEngine, "search", inspect_search)
    with connect(":memory:") as database:
        reader = database.begin("read")
        observed = database.search_vectors(
            reader,
            space="s",
            query=query,
            k=1,
            candidate_filter=candidate_filter,
        )
        reader.rollback()

    assert observed.hits[0].record_id == 7
    assert calls == _calls()


def test_query_callback_that_rolls_back_runs_before_page_access_and_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Input execution may finish a transaction, but liveness is rechecked under the section."""
    reached = False

    def should_not_search(_vectors: VectorEngine, **_arguments: object) -> object:
        nonlocal reached
        reached = True
        raise AssertionError("a rolled-back query reached the vector engine")

    monkeypatch.setattr(VectorEngine, "search", should_not_search)
    with connect(":memory:") as database:
        reader = database.begin("read")

        class RollbackSequence:
            def __iter__(self):  # noqa: ANN204 - adversarial iterable protocol
                reader.rollback()
                return iter((1.0,))

        with pytest.raises(GrafxTransactionStateError):
            database.search_vectors(
                reader,
                space="s",
                query=RollbackSequence(),  # type: ignore[arg-type]
                k=1,
            )

    assert not reader.active
    assert reached is False


@pytest.mark.parametrize("identifier", (0, -1, MAX_U64, True))
def test_search_vectors_refuses_unusable_filter_ids_before_search(
    monkeypatch: pytest.MonkeyPatch, identifier: object
) -> None:
    reached = False

    def should_not_search(_vectors: VectorEngine, **_arguments: object) -> object:
        nonlocal reached
        reached = True
        raise AssertionError("an invalid filter reached the vector engine")

    candidate_filter = RecordIdFilter.of({1})
    object.__setattr__(candidate_filter, "record_ids", frozenset({identifier}))
    monkeypatch.setattr(VectorEngine, "search", should_not_search)
    with connect(":memory:") as database:
        reader = database.begin("read")
        with pytest.raises(GrafxConfigurationError):
            database.search_vectors(
                reader,
                space="s",
                query=(1.0,),
                k=1,
                candidate_filter=candidate_filter,
            )
        reader.rollback()

    assert reached is False


_VECTOR_HIT_ONE = VectorHit(1, 1.0, RecordRef(1, 1), False)
_VECTOR_HIT_TWO = VectorHit(2, 0.5, RecordRef(1, 2), False)


@pytest.mark.parametrize(
    ("result", "candidate_filter"),
    (
        (
            VectorSearchResult((_VECTOR_HIT_ONE,), "exact", 1, 1, "other", None),
            None,
        ),
        (
            VectorSearchResult(
                (VectorHit(0, 1.0, RecordRef(1, 1), False),),
                "exact",
                1,
                1,
                "s",
                None,
            ),
            None,
        ),
        (
            VectorSearchResult((_VECTOR_HIT_TWO,), "exact", 1, 1, "s", 1),
            RecordIdFilter.of({1}),
        ),
        (
            VectorSearchResult((_VECTOR_HIT_ONE,), "exact", 1, 1, "s", 2),
            RecordIdFilter.of({1}),
        ),
        (
            VectorSearchResult((_VECTOR_HIT_ONE,), "exact", 1, 1, "s", 1),
            None,
        ),
        (
            VectorSearchResult(
                (_VECTOR_HIT_TWO, _VECTOR_HIT_ONE), "exact", 2, 2, "s", None
            ),
            None,
        ),
        (
            VectorSearchResult(
                (_VECTOR_HIT_ONE, VectorHit(2, 0.5, RecordRef(1, 1), False)),
                "exact",
                2,
                2,
                "s",
                None,
            ),
            None,
        ),
        (
            VectorSearchResult(
                (_VECTOR_HIT_ONE, VectorHit(2, 0.5, RecordRef(1, 2), True)),
                "exact",
                2,
                2,
                "s",
                None,
            ),
            None,
        ),
        (
            VectorSearchResult((_VECTOR_HIT_ONE,), "exact", 0, 1, "s", None),
            None,
        ),
    ),
)
def test_search_vectors_refuses_contradictory_results(
    monkeypatch: pytest.MonkeyPatch,
    result: VectorSearchResult,
    candidate_filter: RecordIdFilter | None,
) -> None:
    monkeypatch.setattr(VectorEngine, "search", lambda _vectors, **_arguments: result)
    with connect(":memory:") as database:
        reader = database.begin("read")
        with pytest.raises(GrafxConfigurationError):
            database.search_vectors(
                reader,
                space="s",
                query=(1.0,),
                k=1 if result.requested_k != 2 else 2,
                candidate_filter=candidate_filter,
            )
        reader.rollback()


def test_search_vectors_accepts_ranked_under_k_boundary_ids_and_exact_filter_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid short filtered results keep raw cardinality and both usable id boundaries."""
    candidate_filter = RecordIdFilter.of({1, 9, MAX_U64 - 1})
    result = VectorSearchResult(
        hits=(
            VectorHit(1, -0.5, RecordRef(1, 1), True),
            VectorHit(MAX_U64 - 1, -0.5, RecordRef(1, 2), True),
        ),
        regime="exact",
        achieved_k=2,
        requested_k=3,
        space="s",
        filter_cardinality=3,
    )
    monkeypatch.setattr(VectorEngine, "search", lambda _vectors, **_arguments: result)

    with connect(":memory:") as database:
        reader = database.begin("read")
        observed = database.search_vectors(
            reader,
            space="s",
            query=(1.0,),
            k=3,
            candidate_filter=candidate_filter,
        )
        reader.rollback()

    assert observed == result


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("page", -2),
        ("page", MAX_PAGE_INDEX + 1),
        ("slot", -2),
        ("slot", MAX_SLOT_ID + 1),
        ("lsn", -1),
        ("lsn", MAX_U64 + 1),
    ),
)
def test_verify_refuses_out_of_range_finding_locations(
    monkeypatch: pytest.MonkeyPatch, field: str, value: int
) -> None:
    location = replace(FindingLocation(), **{field: value})
    report = VerificationReport(
        scope="all",
        findings=(VerificationFinding(FindingKind.RECORD_HEADER, location, "bad"),),
        pages_checked=1,
        files_checked=("heap.dat",),
    )
    monkeypatch.setattr(Verifier, "verify", lambda _verifier, _scope: report)
    with connect(":memory:") as database:
        with pytest.raises(GrafxConfigurationError):
            database.verify("all")


def test_verify_accepts_all_inclusive_location_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    location = FindingLocation(
        page=MAX_PAGE_INDEX,
        slot=MAX_SLOT_ID,
        lsn=MAX_U64,
    )
    report = VerificationReport(
        scope="all",
        findings=(VerificationFinding(FindingKind.PAGE_TYPE, location, "boundary"),),
        pages_checked=1,
        files_checked=("heap.dat",),
    )
    monkeypatch.setattr(Verifier, "verify", lambda _verifier, _scope: report)

    with connect(":memory:") as database:
        assert database.verify("all") == report


@pytest.mark.parametrize(
    ("scope", "report"),
    (
        (
            "all",
            VerificationReport(
                "all",
                (VerificationFinding("future_kind", FindingLocation(), "bad"),),
                1,
                0,
                0,
                ("heap.dat",),
            ),
        ),
        ("all", VerificationReport("all", files_checked=("",))),
        (
            "all",
            VerificationReport(
                "all", pages_checked=2, files_checked=("heap.dat", "heap.dat")
            ),
        ),
        ("pages", VerificationReport("pages", records_checked=1)),
        (
            "records",
            VerificationReport(
                "records", records_checked=1, files_checked=("heap.dat",)
            ),
        ),
        ("all", VerificationReport("all", pages_checked=1, files_checked=())),
        (
            "all",
            VerificationReport("all", pages_checked=0, files_checked=("heap.dat",)),
        ),
    ),
)
def test_verify_refuses_contradictory_report_counters_and_files(
    monkeypatch: pytest.MonkeyPatch, scope: str, report: VerificationReport
) -> None:
    monkeypatch.setattr(Verifier, "verify", lambda _verifier, _scope: report)
    with connect(":memory:") as database:
        with pytest.raises(GrafxConfigurationError):
            database.verify(scope)


_RECYCLE_BASE = RecycleReport(7, retained=("tail",), lag_segments=0)


@pytest.mark.parametrize(
    "report",
    (
        replace(
            _RECYCLE_BASE,
            recycled=("old", "old"),
            reclaimed_bytes=1,
        ),
        replace(_RECYCLE_BASE, deferred=("held", "held")),
        replace(_RECYCLE_BASE, retained=("tail", "tail"), lag_segments=1),
        replace(
            _RECYCLE_BASE,
            recycled=("old",),
            deferred=("old",),
            reclaimed_bytes=1,
        ),
        replace(_RECYCLE_BASE, lag_segments=1),
        replace(_RECYCLE_BASE, reclaimed_bytes=1),
        replace(_RECYCLE_BASE, recycled=("old",), reclaimed_bytes=0),
        replace(_RECYCLE_BASE, deferred=("",)),
        replace(_RECYCLE_BASE, horizon_lsn=MAX_U64 + 1),
    ),
)
def test_checkpoint_refuses_contradictory_recycle_reports(
    monkeypatch: pytest.MonkeyPatch, report: RecycleReport
) -> None:
    monkeypatch.setattr(TransactionManager, "checkpoint", lambda _manager: report)
    with connect(":memory:") as database:
        with pytest.raises(GrafxConfigurationError):
            database.checkpoint()


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
