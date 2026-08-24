"""The vector engine: embedding spaces, the HNSW index, and the two-regime similarity search.

This is the door SPEC-VEC opens onto the rest of the database (CONTRACT.md section 8.8). It owns
the lifecycle of an embedding space (FR-1, FR-3), the validation every vector write passes
(BR-5), the identity rule that keeps two spaces from ever being compared (FR-2, BR-1), the
planner that chooses between an exact scan and a filter-aware traversal (FR-5), and the snapshot
correctness of what a search returns (FR-9).

Where the index lives
---------------------
:class:`VectorHnswIndex` extends the ``ProximityIndex`` of the index framework, so the vector
index IS one of the database's secondary indexes rather than a second index framework standing
beside it. It inherits the paged file, the durable ``built_through_lsn``, staleness detection and
the refusal that follows it, the entry format, the log records, staging, commit, rollback,
horizon-bounded reconciliation and the verification walk. What it adds is the graph.

Staleness is the part worth naming, because its absence is a wrong answer that hides well. A
proximity index answers from its own entries without consulting the heap, so an index that is
behind the heap returns a plausible, confidently-ordered top-k that silently omits rows. The
framework marks that state durably and refuses the lookup; a memory-resident index cannot, and
this class had to stop being one.

The class lives here and not in ``domain/vector/`` because it owns pages, and because a domain
module may not import the engine (amendment A12). The pure half -- the graph, the space rules,
the planner, the key -- stays in the domain, which is the split that division of labour implies.

Two regimes, two correctness contracts
--------------------------------------
The regime is a decision about COST; the two contracts it selects are decisions about TRUTH.

* **Exact.** Below the calibrated threshold the engine reads every candidate FROM THE HEAP and
  applies the caller's snapshot to the heap version. The heap is the authority on what exists, so
  recall is 1.0 by construction and a stale entry cannot put a deleted row into a result.
* **Approximate.** Above it the engine traverses the versioned index, where every entry carries
  the commit that created it and the commit that ended it, and visibility is decided from those
  stamps by the framework's own predicate. No page is read on the hot path.

Each rule is load-bearing in exactly one regime, so a test can show which one answered. Adding
heap confirmation to the traversal as well would make both unkillable and neither provable
(amendment A67); divergence between the index and the heap is instead REPORTED, by the
framework's ``IndexManager.verify``.

Locks and host code
-------------------
This engine spawns no thread and shares no mutable state between instances. The one lock it
uses, the guard over the derived graph, is handed in by the composition root (the engine imports
no mechanism, CF-13) and is held across REFERENCE operations only: publishing a complete
picture, retiring one, certifying one. Nothing under it reaches the pool, the heap, the resolver,
the candidate filter, the metrics sink or the ``VectorMath`` port, so the boundary amendment
A91 guards -- calling host-supplied code while holding an internal lock -- cannot arise, and
neither can the lock-order inversion with the pool's own guard (pool, then graph guard; never
the reverse). The host code the engine does call runs with its invariants already established.

The derived graph and its publication rule (P0.5)
-------------------------------------------------
The graph is derived state: it, the three maps that translate between graph nodes and index
entries, and the log position it reflects form ONE picture, :class:`_GraphSnapshot`, built in
locals and published by a single reference assignment. A search captures the picture once and
answers from it. That is what makes the fast path safe under concurrency: nothing that can be
captured is ever partial, a build that fails leaves the published picture alone (it may be
another thread's complete one), and two builds can never be mixed -- the graph of one with the
maps of the other. A commit certifies a picture as current only when it verified, BEFORE the
store moved, that the picture was current and then noted its own changes into it; a picture
that was already behind is retired instead, because a mark that says "current" over a graph
missing another process's rows is the silent short answer of LESSONS L22 one path over.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxError,
    GrafxIndexError,
    GrafxVectorValidationError,
)
from okto_grafx.domain.ids import Csn, Lsn, RecordId, RecordRef
from okto_grafx.domain.index.entry import IndexEntry
from okto_grafx.domain.index.records import IndexChange, IndexOperation
from okto_grafx.domain.index.visibility import (
    IndexVisibility,
    SnapshotLike,
    entry_visible,
)
from okto_grafx.domain.model.record import HeapVersion
from okto_grafx.domain.model.schema import EmbeddingSpaceDef, TableDef
from okto_grafx.domain.model.value import VectorValue
from okto_grafx.domain.ports.clock import Clock
from okto_grafx.domain.ports.events import EventSink
from okto_grafx.domain.ports.metrics import MetricsSink
from okto_grafx.domain.ports.vectormath import DistanceMetric, VectorMath
from okto_grafx.domain.vector.filter import CandidateFilter
from okto_grafx.domain.vector.key import (
    VECTOR_KEY_SIZE,
    VectorIndexDefinition,
    vector_digest,
)
from okto_grafx.domain.vector.hnsw import (
    DEFAULT_EF_CONSTRUCTION,
    DEFAULT_EF_SEARCH,
    DEFAULT_NEIGHBOURS,
    HnswGraph,
    TraversalStats,
)
from okto_grafx.domain.vector.planner import (
    DEFAULT_EXACT_SCAN_THRESHOLD,
    REGIME_APPROXIMATE,
    REGIME_EXACT,
    RegimePlan,
    plan_regime,
)
from okto_grafx.domain.vector.space import (
    require_active,
    require_space_identity,
    validate_components,
    validate_query_components,
)
from okto_grafx.domain.wal.record import WalRecord
from okto_grafx.engine.catalog_store import CatalogStore
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.index_manager import (
    IndexManager,
    ProximityIndex,
    StagingTransaction,
)
from okto_grafx.engine.metrics_catalog import MetricEmitter, metric

__all__ = [
    "DEFAULT_INDEX_SEED",
    "GraphGuard",
    "PHASE_PLAN",
    "PHASE_TRAVERSE",
    "PHASE_VALIDATE",
    "ScoredEntry",
    "VectorResolver",
    "VectorHnswIndex",
    "VectorHit",
    "VectorSearchResult",
    "VectorEngine",
]

DEFAULT_INDEX_SEED: int = 0x0C701A11F0C0FFEE
"""The seed every index is built from unless the caller chooses another one.

A fixed seed is what makes a graph reproducible: the same sequence of insertions produces the
same towers, the same neighbourhoods and therefore the same approximate answer on every machine
and in every run (G2b, amendment A5). It is a constant rather than a clock reading precisely so
that a failing search can be replayed.
"""

PHASE_PLAN: str = "plan"
"""The phase that validates the query and chooses the regime."""

PHASE_TRAVERSE: str = "traverse"
"""The phase that visits candidates: an exact scan of the filtered set, or a graph traversal."""

PHASE_VALIDATE: str = "validate"
"""The phase that turns visited candidates into the ranked result."""

VectorResolver = Callable[[RecordRef], "tuple[RecordId, Sequence[float]]"]
"""How the graph learns what one stored entry IS: the record it names and its components.

Both come from the heap, and both come from ONE read. The record identity is needed because the
key of an entry is the encoded vector -- the index framework derives every key from the indexed
column of the row -- so the key cannot answer which record an entry belongs to and the row must.

Injected rather than reached for, so the index holds no catalog of its own and a test can build a
graph over components it chose. The engine supplies the real one.
"""

_LATENCY = metric("oktografx_vector_query_latency_seconds").name
_EXACT_FALLBACK = metric("oktografx_vector_exact_fallback_total").name
_ACHIEVED_K = metric("oktografx_vector_achieved_k").name
_SELECTIVITY = metric("oktografx_vector_filter_selectivity_ratio").name
_INDEX_ENTRIES = metric("oktografx_vector_index_entries").name
_SPACES_RETIRED = metric("oktografx_vector_space_retired_total").name
_COVERAGE = metric("oktografx_vector_space_coverage_ratio").name
_INDEX_AGE = metric("oktografx_vector_index_age_seconds").name


@dataclass(frozen=True, slots=True)
class ScoredEntry:
    """One entry of the index together with the score it earned against a query."""

    entry: IndexEntry
    record_id: RecordId
    score: float

    @property
    def ref(self) -> RecordRef:
        """Return the heap location of the version this hit names."""
        return self.entry.ref


@dataclass(frozen=True, slots=True)
class VectorHit:
    """One neighbour of a similarity search.

    ``retired`` says the vector still answers searches while its space no longer accepts writes,
    which is what makes a migration observable to the caller doing it (FR-3, AC-4).
    """

    record_id: RecordId
    score: float
    ref: RecordRef
    retired: bool


@dataclass(frozen=True, slots=True)
class VectorSearchResult:
    """The answer to one similarity search, with how it was produced.

    ``regime`` and ``achieved_k`` are never omitted and never inferred by the caller: an
    approximation always says so, and a search that could not reach the requested neighbour count
    says that too, rather than returning a short list that looks complete (BR-2).
    """

    hits: tuple[VectorHit, ...]
    regime: str
    achieved_k: int
    requested_k: int
    space: str
    filter_cardinality: int | None


def _require_positive_k(k: int) -> int:
    """Return the requested neighbour count, refusing one that could not describe a result."""
    if isinstance(k, bool) or not isinstance(k, int):
        raise GrafxConfigurationError(
            f"A similarity search needs an integer neighbour count; got {type(k).__name__}.",
            field="k",
            value=repr(k),
        )
    if k < 1:
        raise GrafxConfigurationError(
            f"A similarity search needs a neighbour count of at least 1; got {k}.",
            field="k",
            value=k,
        )
    return k


def _require_snapshot(snapshot: object) -> SnapshotLike:
    """Return the snapshot, refusing an argument that cannot answer the visibility question."""
    if not hasattr(snapshot, "visible"):
        raise GrafxConfigurationError(
            f"A similarity search needs a snapshot that can answer visible(xmin, xmax); got "
            f"{type(snapshot).__name__}.",
            field="snapshot",
            value=type(snapshot).__name__,
        )
    return snapshot  # type: ignore[return-value]


def _duplicate_version(
    space: EmbeddingSpaceDef, record_id: RecordId, here: RecordRef, there: RecordRef
) -> GrafxIndexError:
    """Build the refusal for a record that shows two visible versions to one snapshot.

    One snapshot sees one version of a record; two is a broken state, and a ranking that named the
    record twice would be a wrong result rather than a detectable one. The refusal exists on BOTH
    search paths, because each regime reaches the state through a different door and a guard on
    one path says nothing about the other (amendment A66).
    """
    return GrafxIndexError(
        f"Embedding space {space.name!r} shows two visible versions of record {record_id} at "
        f"page {here.page} slot {here.slot} and page {there.page} slot {there.slot}; one "
        f"snapshot sees one version.",
        field="record_id",
        space=space.name,
        value=record_id,
    )


def _filter_cardinality(candidate_filter: CandidateFilter | None) -> int | None:
    """Return the cardinality a filter declares, treating an unusable answer as unknown.

    A filter is host-supplied code, so its own failure must not leave this component through a
    door that promises only ``Grafx*`` types. An estimate that cannot be obtained is simply
    unknown, which the planner already handles by assuming the filter is as large as the space.
    """
    if candidate_filter is None:
        return None
    try:
        declared = getattr(candidate_filter, "cardinality", None)
    except GrafxError:
        raise
    except Exception:  # noqa: BLE001 - a host estimate that fails is an unknown estimate
        return None
    if declared is None:
        return None
    if isinstance(declared, bool) or not isinstance(declared, int) or declared < 0:
        return None
    return declared


def _guarded_admits(
    candidate_filter: CandidateFilter | None,
) -> Callable[[RecordId], bool] | None:
    """Return the predicate of a filter, with a foreign failure translated into the taxonomy.

    ``admits`` decides membership, so a failure in it cannot be dropped the way a failed metric
    emission can: swallowing it would silently shrink the result, which is the wrong-result shape
    BR-2 exists to prevent. It is re-raised as an index error instead, with the original attached,
    so the caller learns that its own predicate failed and no non-``Grafx*`` type leaves the door
    (CONTRACT.md section 11 item 5).
    """
    if candidate_filter is None:
        return None
    inner = candidate_filter.admits

    def admits(record_id: RecordId) -> bool:
        """Ask the caller's filter about one record, refusing in the taxonomy when it fails."""
        try:
            return bool(inner(record_id))
        except GrafxError:
            raise
        except Exception as failure:  # noqa: BLE001 - translated, never swallowed
            raise GrafxIndexError(
                f"The candidate filter of this search failed on record {record_id}: "
                f"{type(failure).__name__}.",
                field="candidate_filter",
                value=record_id,
            ) from failure

    return admits


class GraphGuard(Protocol):
    """What the derived graph needs from a lock: a context manager that can wait, wake, and say
    which thread is asking.

    The composition root hands one in (``adapters.graph_guard.ConditionGuard``; CF-13: the
    mechanism arrives by construction, the engine imports none). It is held across reference
    operations on the published picture only -- never across the walk, the resolver, the pool
    or the ``VectorMath`` port (A91, LESSONS L2) -- and the same guard may be shared by every
    index of one engine: a wake meant for another index costs its waiters one re-check.

    ``thread_token`` is what keeps a build from waiting on itself: host code the build calls
    through the ``VectorMath`` port may call back into a search on the same index, and that
    search must be told apart from another thread's.
    """

    def __enter__(self) -> object:
        """Take the guard."""
        ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> bool | None:
        """Release the guard, whether or not the body raised."""
        ...

    def wait_for(
        self, predicate: Callable[[], bool], timeout: float | None = None
    ) -> bool:
        """Release the guard while waiting for the predicate, up to the timeout; return its value."""
        ...

    def notify_all(self) -> None:
        """Wake every thread waiting on this guard."""
        ...

    def thread_token(self) -> int:
        """Return a token identifying the calling thread for as long as it lives."""
        ...


class _UnguardedBuild:
    """The guard of a composition that hands none in: nothing to take, nothing to wait for.

    The engine's own suite builds the vector engine directly and drives it from one thread; a
    wait that returns at once is what keeps that composition from waiting on a notification
    nothing will send. A multi-threaded host composes through the assembly, which hands in a
    real condition.
    """

    __slots__ = ()

    def __enter__(self) -> object:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        return None

    def wait_for(
        self, predicate: Callable[[], bool], timeout: float | None = None
    ) -> bool:
        """Answer the predicate at once: with no other thread, there is nothing to wait for."""
        return bool(predicate())

    def notify_all(self) -> None:
        """Wake nobody: an unguarded composition has no waiters."""
        return None

    def thread_token(self) -> int:
        """Return the one token of the one thread an unguarded composition has."""
        return 0


_BUILD_WAIT_SECONDS: float = 0.5
"""One slice of waiting behind another thread's build of the same index."""

_BUILD_CATCH_UP_PASSES: int = 3
"""Passes a cold build spends catching up with commits that landed while it ran.

Each pass walks the store again, takes into the picture what it lacks, and re-reads the header;
a store that keeps moving is left behind after the last pass, and the mark says so, so the next
search rebuilds. Bounded, so a commit storm cannot hold a search hostage.
"""

_BUILD_WAIT_SLICES: int = 120
"""Slices a waiter spends behind another thread's build before it builds for itself.

A duplicate build is wasted work and never a wrong answer -- both pictures are complete, and the
publication keeps the fresher one -- so the patience only bounds how long a search can be held
behind a build that is slow past reason, or behind an unguarded composition's instant no.
"""


@dataclass(frozen=True, slots=True)
class _GraphSnapshot:
    """One complete, consistent picture of the index: the graph, its maps and the mark.

    ``mark`` is a ``built_through_lsn`` reading taken BEFORE a walk that fed the picture -- at
    the start of the build, or at the start of a catch-up pass -- which is the only reading a
    picture can honestly claim: an entry the store received after it was read is either in the
    walk (then the picture is ahead of its mark, which costs one rebuild) or not (then the mark
    says so). The maps are mutated in place by the warm path, under the same
    discipline as the graph -- an entry is registered before its node becomes reachable -- and a
    search that captured this picture keeps every part of it for as long as it runs.
    """

    graph: HnswGraph
    node_of_ref: dict[int, int]
    entry_of_node: dict[int, IndexEntry]
    record_of_node: dict[int, RecordId]
    mark: Lsn

    def certified(self, mark: Lsn) -> _GraphSnapshot:
        """Return this same picture -- same graph, same maps -- carrying a newer mark."""
        return _GraphSnapshot(
            graph=self.graph,
            node_of_ref=self.node_of_ref,
            entry_of_node=self.entry_of_node,
            record_of_node=self.record_of_node,
            mark=mark,
        )


class VectorHnswIndex(ProximityIndex):
    """A navigable small world graph over the framework's proximity store.

    Everything durable belongs to the store. The graph is derived state over the entries the
    store holds and the vectors the heap holds, so it is dropped whenever the entries move
    underneath it and rebuilt on demand -- the lifetime rule amendment A63 asks for, answered by
    the structure that owns the entries rather than by a second invalidation scheme.

    Adjacency is deliberately NOT stored as entries. One row insertion links up to thirty-two
    neighbours bidirectionally at layer zero alone, so storing edges would turn one row into
    roughly a hundred log records and would make the graph a second durable structure to keep
    consistent with the first. It is cheaper, and far easier to prove correct, to derive it.
    """

    __slots__ = (
        "_space_id",
        "_space_name",
        "_dimension",
        "_metric",
        "_storage_dtype",
        "_math",
        "_resolve",
        "_seed",
        "_neighbours",
        "_ef_construction",
        "_ef_search",
        "_guard",
        "_snapshot",
        "_building",
        "_builder",
    )

    def __init__(
        self,
        definition: IndexDefinition,
        pool: object,
        metrics: MetricsSink,
        *,
        space_id: int,
        space_name: str,
        dimension: int,
        metric_of_space: DistanceMetric,
        storage_dtype: str,
        math: VectorMath,
        resolve: VectorResolver,
        seed: int = DEFAULT_INDEX_SEED,
        neighbours: int = DEFAULT_NEIGHBOURS,
        ef_construction: int = DEFAULT_EF_CONSTRUCTION,
        ef_search: int = DEFAULT_EF_SEARCH,
        guard: GraphGuard | None = None,
    ) -> None:
        """Build the index of one embedding space over one paged store.

        ``guard`` is the lock the published picture is replaced under (see :class:`GraphGuard`);
        without one the index is fit for a single thread only.
        """
        super().__init__(definition, pool, metrics)  # type: ignore[arg-type]
        self._space_id = space_id
        self._space_name = space_name
        self._dimension = dimension
        self._metric = metric_of_space
        self._storage_dtype = storage_dtype
        self._math = math
        self._resolve = resolve
        self._seed = seed
        self._neighbours = neighbours
        self._ef_construction = ef_construction
        self._ef_search = ef_search
        self._guard: GraphGuard = _UnguardedBuild() if guard is None else guard
        # The published picture, or None while there is none. Replaced by ONE assignment under
        # the guard, captured by ONE read; never edited into a different picture in place.
        self._snapshot: _GraphSnapshot | None = None
        # True while a thread of this process builds a picture; other searches wait behind it.
        # The builder's thread token is kept beside it, so a call the build itself makes back
        # into this index (host code behind the VectorMath port) is never made to wait on it.
        self._building: bool = False
        self._builder: int | None = None

    # --- identity ---------------------------------------------------------------------------

    @property
    def space_id(self) -> int:
        """Return the numeric identity of the embedding space this index covers."""
        return self._space_id

    @property
    def space_name(self) -> str:
        """Return the name of the embedding space this index covers."""
        return self._space_name

    @property
    def dimension(self) -> int:
        """Return the number of components every vector in this index carries."""
        return self._dimension

    @property
    def metric_of_space(self) -> DistanceMetric:
        """Return the distance metric of the embedding space this index covers."""
        return self._metric

    @property
    def storage_dtype(self) -> str:
        """Return the dtype the components of this index are stored in."""
        return self._storage_dtype

    @property
    def ef_search(self) -> int:
        """Return the beam width a search uses before the requested neighbour count raises it."""
        return self._ef_search

    def require_readable(self) -> None:
        """Refuse a read of an index that is behind the heap, in the framework's own words.

        The framework keeps this refusal private because its own read doors call it. A vector
        search reaches the index through two paths -- a traversal and an exact scan that
        enumerates the same entries -- so the engine needs to ask the same question at its own
        door, and asking it twice through one implementation is what keeps a single answer.
        """
        self._require_readable()

    def require_own_key(self, key: bytes) -> bytes:
        """Return the key, refusing one this index could not have written.

        The key used to BE the encoded vector, and that shape had a door: ``stage_insert`` takes
        bytes from the caller, so a key could be assembled from the format's own building blocks
        without passing the encoder that refuses a bad one -- and a hand-built key carrying a NaN
        decoded cleanly, because the value codec deliberately does not re-check what a write door
        already refused, and reached a loggable record. Validating only on the way IN left that
        open (LESSONS L14).

        The derivation settled for CF-9 removes the door rather than guarding it: a key is now a
        digest, so no components travel in it and there is nothing to decode. What remains to
        check is the shape, and it is checked here, before a record exists.
        """
        raw = bytes(key)
        if len(raw) != VECTOR_KEY_SIZE:
            raise GrafxIndexError(
                f"Index {self.name!r} keys a vector by a {VECTOR_KEY_SIZE}-byte digest; this key "
                f"is {len(raw)} bytes.",
                field="key",
                index=self.name,
                expected=VECTOR_KEY_SIZE,
                value=len(raw),
            )
        return raw

    def stage_insert(
        self, txn: StagingTransaction, key: bytes, ref: RecordRef, csn: Csn
    ) -> WalRecord:
        """Stage an entry, refusing a key this index may not store before a record exists."""
        self.require_own_key(key)
        return super().stage_insert(txn, key, ref, csn)

    def stage_delete(
        self, txn: StagingTransaction, key: bytes, ref: RecordRef, csn: Csn
    ) -> WalRecord:
        """Stage a tombstone, refusing a key this index may not store before a record exists."""
        self.require_own_key(key)
        return super().stage_delete(txn, key, ref, csn)

    def key_for(self, components: Sequence[float]) -> bytes:
        """Return the index key of one stored vector, as the framework derives every key.

        The definition declares the ``vector_digest_v1`` derivation and implements it, so the
        framework's verifier re-derives a key the same way this index wrote it and reports a row
        that changed without the index following. Producing the digest through the one shared
        function is what keeps the two sides from drifting apart.
        """
        return vector_digest(
            VectorValue(tuple(components), self._space_id, self._storage_dtype)
        )

    # --- counting ---------------------------------------------------------------------------

    def live_count(self) -> int:
        """Return how many entries have not been ended by any commit."""
        return sum(1 for entry in self.walk() if entry.live)

    def visible_count(self, snapshot: SnapshotLike) -> int:
        """Return how many entries one snapshot may see."""
        return len(self.visible_entries(_require_snapshot(snapshot)))

    # --- the derived graph ------------------------------------------------------------------

    def invalidate_graph(self) -> None:
        """Drop the published picture, so the next search rebuilds it from the entries the store holds.

        The picture is REPLACED, never edited in place: a search in flight on another thread
        holds the picture it started with and finishes on it, stale by at most one commit, rather
        than on one that changes under it (C9 round-3 B5).

        This is the unconditional door -- a rebuild, a stale mark, a RESET -- and it is the ONLY
        unconditional one. A builder that fails drops its locals and touches nothing published,
        and a commit that finds its picture superseded retires THAT picture (``_retire``): the
        picture either would otherwise drop may be another thread's complete, correct build, and
        dropping a success on somebody else's failure was one of the P0.5 interleavings.
        """
        with self._guard:
            self._snapshot = None

    def graph(self) -> HnswGraph:
        """Return the graph of the current picture, building one when there is none or it is behind."""
        return self.snapshot().graph

    def snapshot(self) -> _GraphSnapshot:
        """Return one complete picture of the index as of now, building it when needed.

        THE RULE (P0.5). A picture is built in locals and published by ONE reference
        assignment, so nothing that can be captured is ever partial, and two builds can never be
        mixed. A caller captures the picture ONCE and answers from it; it never reads
        ``self._snapshot`` a second time.

        Freshness is decided against the header's ``built_through_lsn`` read BEFORE the walk
        begins, and the picture carries that reading as its mark. A commit that lands during the
        build is not noted into the picture (nothing is published to note into); before the
        picture is published the build CATCHES UP with it (``_catch_up``): the header is read
        again and, while it has moved, the picture is built again from a walk taken after that
        reading. A caller with a fixed snapshot could not see that commit anyway; a caller whose
        predicate admits it -- ``SnapshotLike`` is structural -- gets it too, as the protocol
        before this one gave it (the M0A cross-review). If the store still moves past the last
        pass, the mark stays behind the header and the next search rebuilds: never a
        certification the build did not verify.

        One build at a time per index: a thread that finds a build in flight waits on the guard
        for it, in bounded slices, and takes the published picture when it wakes. Past
        ``_BUILD_WAIT_SLICES`` it builds for itself, which is wasted work and never a wrong
        answer. The build is OWNED by the thread that started it: only the owner clears the
        flag and wakes the waiters, and a call the owner itself makes back into this index --
        host code behind the ``VectorMath`` port calling ``search`` -- builds its own picture
        at once rather than waiting on the build it is part of (the M0A cross-review's
        self-wait: sixty seconds of a search waiting for itself).

        The header is read OUTSIDE the guard on every turn: reading it pins a page and takes the
        pool's lock, and this process's lock order is pool, then guard, never the reverse.
        Nothing under the guard reaches the pool, the heap, the resolver or the ``VectorMath``
        port (A91, LESSONS L2).
        """
        token = self._guard.thread_token()
        owner = False
        waited = 0
        while True:
            header = self.built_through_lsn
            with self._guard:
                current = self._snapshot
                if current is not None:
                    if current.mark == header:
                        return current
                    # The store moved under this picture -- a commit of this process noted
                    # elsewhere, or a commit of another process (LESSONS L22). Retire exactly
                    # this one; whatever is published by the time the build ends is judged then.
                    self._snapshot = None
                if not self._building:
                    self._building = True
                    self._builder = token
                    owner = True
                    break
                if self._builder == token:
                    # The build in flight is THIS thread's: host code it called through the
                    # VectorMath port came back in through search(). Waiting would be waiting
                    # on ourselves; build a picture for this call and leave the outer build be.
                    break
                finished = self._guard.wait_for(
                    lambda: not self._building, timeout=_BUILD_WAIT_SECONDS
                )
                waited += 1
                if not finished and waited >= _BUILD_WAIT_SLICES:
                    break
        try:
            built = self._catch_up(self._build(header))
        except BaseException:
            # A build that does not finish leaves NOTHING behind -- and takes nothing away. Its
            # locals go with this frame; the published picture, which may be another thread's
            # complete build, is not touched. The two early refusals inside the build (the
            # resolver failing, a dimension mismatch) used to re-raise with a partial graph
            # published, and every later search answered out of the fragment (C9 round-2 B3).
            if owner:
                self._release_build()
            raise
        with self._guard:
            current = self._snapshot
            if current is None or current.mark < built.mark:
                self._snapshot = built
            else:
                # Somebody published a picture at least as fresh while this one was built --
                # the duplicate-build case. Both are complete; the published one is kept, and
                # this search answers from it rather than from a picture nobody else can see.
                built = current
            if owner:
                self._building = False
                self._builder = None
                self._guard.notify_all()
        return built

    def _catch_up(self, picture: _GraphSnapshot) -> _GraphSnapshot:
        """Replace a not-yet-published picture that the store moved under with a fresh build.

        The walk that fed the build is a moment in the past. A commit that landed after it is in
        the store and not in the picture, and nothing noted it there, because a picture under
        construction is not published. A caller whose snapshot is fixed cannot see that commit
        -- but ``SnapshotLike`` is taken by shape (A19), a predicate that admits it is a
        supported caller, and the protocol before this one answered it whole (it noted commits
        into the partial graph it had already published). So: read the header again and, while
        it has moved past the mark, build the picture AGAIN from a walk taken after that reading.

        Rebuilt, not patched. A pass that only offered the new walk to the old picture took in
        what landed -- and kept what LEFT: an entry tombstoned and then reconciled away while
        the build was parked is absent from the walk, so the old picture's copy of it stayed
        live, with no ending stamp, and the published snapshot answered a deleted row as current
        (M0A verification, Codex). A build over the new walk holds exactly what the store holds.
        Bounded: past the last pass the mark stays behind the header and the next search
        rebuilds; never a certification the build did not verify.
        """
        for _pass in range(_BUILD_CATCH_UP_PASSES):
            header = self.built_through_lsn
            if header == picture.mark:
                return picture
            picture = self._build(header)
        return picture

    def _release_build(self) -> None:
        """Give the build up as its owner: clear the flag and wake whoever waited for it."""
        with self._guard:
            self._building = False
            self._builder = None
            self._guard.notify_all()

    def _build(self, mark: Lsn) -> _GraphSnapshot:
        """Build a complete picture in locals over the entries the store holds, marked at ``mark``.

        A tombstoned entry is inserted with the live ones: it must not be RETURNED, and it must
        stay traversable as a bridge while an older snapshot can still see it, which is the same
        rule the ACORN traversal applies to a filtered node. Every refusal on the way in
        propagates, and the caller drops the picture with the frame.
        """
        picture = _GraphSnapshot(
            graph=HnswGraph(
                self._math,
                self._metric,
                seed=self._seed ^ self._space_id,
                neighbours=self._neighbours,
                ef_construction=self._ef_construction,
            ),
            node_of_ref={},
            entry_of_node={},
            record_of_node={},
            mark=mark,
        )
        for entry in sorted(
            self.walk(), key=lambda item: (item.born_csn, item.ref.encode())
        ):
            self._install(picture, entry)
        return picture

    def _retire(self, picture: _GraphSnapshot) -> None:
        """Drop ``picture`` if it is the published one; leave any other picture alone.

        The compare is what keeps one thread's outcome from erasing another's: a warm-path
        refusal, or a removal, contaminates the picture it happened to, and that is the only
        picture it may discard.
        """
        with self._guard:
            if self._snapshot is picture:
                self._snapshot = None

    def _certify(self, picture: _GraphSnapshot, mark: Lsn) -> None:
        """Republish ``picture`` carrying ``mark``, if it is still the published one.

        Only a caller that verified the picture was current BEFORE the store moved, and then
        noted its own changes into it, may call this; the compare refuses the certification when
        the picture was retired or replaced in between.
        """
        with self._guard:
            if self._snapshot is picture:
                self._snapshot = picture.certified(mark)

    def _install(self, picture: _GraphSnapshot, entry: IndexEntry) -> None:
        """Put one stored entry into a picture, resolving its components through the resolver.

        A refusal propagates and the picture is left to the caller: dropped when it was a local
        under construction, retired when it was the published one (``_note``), because a
        refusal inside ``graph.insert`` can leave the graph half-linked and a half-linked graph
        is discarded, never repaired.
        """
        encoded = entry.ref.encode()
        node = picture.node_of_ref.get(encoded)
        if node is not None:
            picture.entry_of_node[node] = entry
            return
        self._install_fresh(picture, entry, encoded)

    def _install_fresh(
        self, picture: _GraphSnapshot, entry: IndexEntry, encoded: int
    ) -> None:
        """Resolve, check and insert one entry that the picture does not hold yet."""
        try:
            resolved = self._resolve(entry.ref)
        except GrafxError as failure:
            raise GrafxIndexError(
                f"Index {self.name!r} holds an entry at page {entry.ref.page} slot "
                f"{entry.ref.slot} whose row the heap cannot supply: {failure.message}",
                field="ref",
                index=self.name,
                page=entry.ref.page,
                slot=entry.ref.slot,
            ) from failure
        record_id, values = resolved
        components = tuple(float(value) for value in values)
        if len(components) != self._dimension:
            raise GrafxIndexError(
                f"Index {self.name!r} holds vectors of {self._dimension} components; the row at "
                f"page {entry.ref.page} slot {entry.ref.slot} carries {len(components)}.",
                field="dimension",
                index=self.name,
                expected=self._dimension,
                value=len(components),
            )
        entries = picture.entry_of_node
        node = len(entries) + 1
        while node in entries:
            node += 1
        # Nothing becomes reachable before every step that can still refuse has succeeded. The
        # insertion scores the new node against its neighbours, so it can refuse on arithmetic
        # this index cannot rank -- and publishing the mappings first would leave an entry with
        # no graph node behind it: invisible to a search, uncountable by the planner, and a bare
        # KeyError out of walk() and lookup() forever.
        #
        # The entry and the record are registered BEFORE the node becomes reachable. graph.insert
        # links the node into the connectivity chain, and a traversal on another thread can reach
        # it the moment it does; a node it can reach must have an entry to answer with, or the
        # traversal dies with a bare KeyError (C9 round-3 B5). A failure inside insert is handled
        # by the caller, which drops or retires the whole picture.
        entries[node] = entry
        picture.record_of_node[node] = record_id
        picture.graph.insert(node, components)
        picture.node_of_ref[encoded] = node

    def _note(self, picture: _GraphSnapshot, change: IndexChange) -> bool:
        """Bring one published picture in line with one applied change; say whether it survived.

        Returns False when the change retired the picture -- a RESET, a removal, or a refusal on
        the way in -- so the caller stops noting into a picture nobody can reach any more and,
        above all, does not certify it.
        """
        if change.operation is IndexOperation.RESET:
            self._retire(picture)
            return False
        encoded = change.ref.encode()
        node = picture.node_of_ref.get(encoded)
        if change.operation is IndexOperation.INSERT:
            if node is None:
                try:
                    self._install(
                        picture,
                        IndexEntry(
                            key=change.key,
                            ref=change.ref,
                            versioned=True,
                            born_csn=change.csn,
                        ),
                    )
                except BaseException:
                    # EVERY refusal on the way into the graph discards the picture, not only
                    # the late one inside graph.insert: the two early refusals used to re-raise
                    # with the graph intact, and a search then answered out of a graph missing
                    # the row (C9 round-2 B4: five nodes, live_count six, stale False).
                    self._retire(picture)
                    raise
            return True
        if node is None:
            return True
        if change.operation is IndexOperation.TOMBSTONE:
            picture.entry_of_node[node] = picture.entry_of_node[node].ended_at(
                change.csn
            )
            return True
        # A removal mutates the graph's neighbour lists and unlinks a node; done in place under
        # a traversal on another thread, that traversal can step onto a node that is no longer
        # there. Derived state is discarded rather than edited, and rebuilt on the next search
        # (a reconcile pass removes many entries in one go and pays one rebuild for all of them).
        self._retire(picture)
        return False

    # --- the write path ---------------------------------------------------------------------

    def commit(self, txn: StagingTransaction, csn: Csn) -> int:
        """Apply the transaction's staged changes, then bring the published picture in line.

        The picture is captured ONCE, before the store moves, together with the verdict on
        whether it was current at that moment: its mark against the header, which the
        transaction manager refreshed from the device at the start of this commit. A picture
        that was already behind -- another PROCESS committed since it was built -- is retired
        rather than certified, because this commit notes only its OWN changes: certifying it
        would publish a graph missing that process's rows with a mark that says current, the
        silent short answer of LESSONS L22 on the warm path (found by the P0.5 probe).
        """
        staged = self.pending(txn)
        picture = self._snapshot
        current = picture is not None and picture.mark == self.built_through_lsn
        applied = super().commit(txn, csn)
        if picture is None:
            return applied
        if not current:
            self._retire(picture)
            return applied
        for change in staged:
            if not self._note(picture, change):
                return applied
        self._certify(picture, self.built_through_lsn)
        return applied

    def apply(self, record: WalRecord) -> None:
        """Redo one log record, then bring the published picture in line with it (as commit does)."""
        from okto_grafx.domain.index.records import change_of

        change = change_of(record)
        picture = self._snapshot
        current = picture is not None and picture.mark == self.built_through_lsn
        super().apply(record)
        if change.index != self.name or picture is None:
            return
        if not current:
            self._retire(picture)
            return
        if self._note(picture, change):
            self._certify(picture, self.built_through_lsn)

    def mark_stale(self, reason: str) -> None:
        """Record staleness durably, and drop the graph derived from the entries it doubts."""
        super().mark_stale(reason)
        self.invalidate_graph()

    def clear_stale(self, built_through: Lsn) -> None:
        """Declare the index rebuilt, and rebuild the graph from what it now holds."""
        super().clear_stale(built_through)
        self.invalidate_graph()

    # --- searching ---------------------------------------------------------------------------

    def search(
        self,
        query: Sequence[float],
        k: int,
        snapshot: SnapshotLike,
        admits: Callable[[RecordId], bool] | None = None,
        *,
        ef: int | None = None,
    ) -> tuple[tuple[ScoredEntry, ...], TraversalStats]:
        """Return the best visible, admitted versions for a query, with traversal statistics.

        The predicate the traversal evaluates is the conjunction of two things: the snapshot may
        see this entry, and the caller's filter admits this record. Both are asked DURING
        expansion, so an entry that fails either one is still a bridge to entries that pass both.

        A stale index refuses here rather than answering: an omission looks exactly like an empty
        neighbourhood, and this is the door where that would be invisible.

        THE CONTRACT ON ``snapshot``. A transaction's fixed view (amendment A19: the predicate
        belongs to the transaction manager) sees exactly what it is entitled to: the picture is
        complete for every position the build verified, and a commit above the view's position
        is invisible to it anyway. ``SnapshotLike`` is taken by shape, so any predicate is a
        caller; what one that admits later commits is promised is linearization, not
        "everything at return": the picture answered from was certified for a header position
        the build verified (commits landing during the build are caught up, in bounded passes),
        a commit past the last pass leaves the mark behind, and the NEXT search takes it.
        """
        predicate = _require_snapshot(snapshot)
        self.require_readable()
        width = self._ef_search if ef is None else ef
        if width < k:
            width = k
        # The picture this search answers from is fixed HERE, by one capture. A commit on
        # another thread may retire or replace the published picture while the traversal runs;
        # the traversal keeps this one and finishes on one consistent picture. A node that
        # vanished from it (removed concurrently) is simply not visible.
        picture = self.snapshot()
        entries = picture.entry_of_node
        records = picture.record_of_node

        def visible_and_admitted(node: int) -> bool:
            """Return True when this snapshot may see the entry and the filter admits it."""
            entry = entries.get(node)
            if entry is None or not entry_visible(entry, predicate):
                return False
            if admits is None:
                return True
            record = records.get(node)
            return record is not None and bool(admits(record))

        ranked, stats = picture.graph.search(query, width, visible_and_admitted)
        scored = [
            ScoredEntry(entry=entries[node], record_id=records[node], score=score)
            for score, node in ranked
            if node in entries and node in records
        ]
        scored.sort(key=lambda item: (-item.score, item.record_id))
        return tuple(scored[:k]), stats

    def __repr__(self) -> str:
        return (
            f"VectorHnswIndex(name={self.name!r}, space={self._space_name!r}, "
            f"stale={self.stale})"
        )


class VectorEngine:
    """Embedding spaces, their indexes, and the similarity search over them.

    Ports arrive by construction and never by import: the arithmetic comes through ``VectorMath``,
    the numbers through ``MetricsSink``, the durations through ``Clock`` and the lifecycle notices
    through ``EventSink``. The catalog says what a space is, the heap is the authority on what
    exists, and the index registry owns every index of the database including this one.
    """

    __slots__ = (
        "_catalog",
        "_heap",
        "_math",
        "_metrics",
        "_clock",
        "_events",
        "_pool",
        "_indexes",
        "_threshold",
        "_seed",
        "_neighbours",
        "_ef_construction",
        "_ef_search",
        "_by_space",
        "_maintained_at",
        "_guard",
    )

    def __init__(
        self,
        *,
        catalog: CatalogStore,
        heap: HeapStore,
        math: VectorMath,
        metrics: MetricsSink,
        clock: Clock,
        events: EventSink | None = None,
        pool: object | None = None,
        indexes: IndexManager | None = None,
        exact_scan_threshold: int = DEFAULT_EXACT_SCAN_THRESHOLD,
        seed: int = DEFAULT_INDEX_SEED,
        neighbours: int = DEFAULT_NEIGHBOURS,
        ef_construction: int = DEFAULT_EF_CONSTRUCTION,
        ef_search: int = DEFAULT_EF_SEARCH,
        guard: GraphGuard | None = None,
    ) -> None:
        """Build the engine over one catalog, one heap and one index registry.

        ``guard`` is the lock every index of this engine publishes its derived graph under (see
        :class:`GraphGuard`). The composition root hands in a ``threading.Condition``; a
        composition that hands in none gets indexes fit for a single thread.
        """
        if isinstance(exact_scan_threshold, bool) or not isinstance(
            exact_scan_threshold, int
        ):
            raise GrafxConfigurationError(
                f"The exact scan threshold must be an integer; got "
                f"{type(exact_scan_threshold).__name__}.",
                field="exact_scan_threshold",
                value=repr(exact_scan_threshold),
            )
        if exact_scan_threshold < 0:
            raise GrafxConfigurationError(
                f"The exact scan threshold must not be negative; got {exact_scan_threshold}.",
                field="exact_scan_threshold",
                value=exact_scan_threshold,
            )
        self._catalog = catalog
        self._heap = heap
        self._math = math
        self._metrics = MetricEmitter(metrics)
        self._clock = clock
        self._events = events
        self._pool = pool
        self._indexes = indexes
        self._threshold = exact_scan_threshold
        self._seed = seed
        self._neighbours = neighbours
        self._ef_construction = ef_construction
        self._ef_search = ef_search
        self._by_space: dict[str, VectorHnswIndex] = {}
        self._maintained_at: dict[str, float] = {}
        self._guard = guard

    # --- spaces -------------------------------------------------------------------------------

    @property
    def exact_scan_threshold(self) -> int:
        """Return the filtered cardinality at or below which a search scans exactly."""
        return self._threshold

    def spaces(self) -> tuple[EmbeddingSpaceDef, ...]:
        """Return every embedding space of the catalog, ordered by numeric identity."""
        return self._catalog.catalog.spaces()

    def space(self, name: str) -> EmbeddingSpaceDef:
        """Return one embedding space by name, refusing a name the catalog does not know."""
        return self._catalog.catalog.space(name)

    def create_space(self, definition: EmbeddingSpaceDef) -> None:
        """Install one embedding space and persist the catalog (FR-1, TR-2).

        Several spaces may cover one property at once, each with its own index and its own
        identity; that coexistence is what makes an embedding model upgrade a migration the
        caller drives at its own pace rather than a destructive rebuild (FR-3, D6).

        The INDEX is not created here, because an index covers a (table, space) pair and a space
        exists before any table declares a column in it. :meth:`attach` creates it.
        """
        if not isinstance(definition, EmbeddingSpaceDef):
            raise GrafxConfigurationError(
                f"An embedding space is defined by an EmbeddingSpaceDef; got "
                f"{type(definition).__name__}.",
                field="definition",
                value=type(definition).__name__,
            )
        self._catalog.catalog.add_space(definition)
        self._catalog.save()
        self._emit(
            "vector.space_created",
            space=definition.name,
            dimension=definition.dimension,
        )

    def retire_space(self, name: str) -> None:
        """Close the write door of one embedding space, leaving it readable forever (FR-3)."""
        retired = self._catalog.catalog.retire_space(name)
        self._catalog.save()
        self._metrics.increment(_SPACES_RETIRED)
        self._publish_space_metrics()
        self._emit("vector.space_retired", space=retired.name)

    def attach(
        self, table: TableDef, space_name: str, catalog: object = None
    ) -> VectorHnswIndex:
        """Create and register the index of one embedding space over one table (TR-2).

        One index per (property, space) is what the specification asks for, and the property is a
        column of a table -- so the index is created when a table declares the column, not when
        the space is declared.

        ``catalog`` is for the one caller that knows about a space the LIVE catalog does not yet:
        the DDL path builds schema changes on a per-transaction working copy, so a space and a
        table declared in the same transaction exist only there until the commit installs them.
        Resolving the space from the live catalog would refuse exactly the statement the quick
        start opens with.
        """
        source = catalog if catalog is not None else self._catalog.catalog
        space = source.space(space_name)
        position = self._vector_column_of(table, space)
        registry = self._require_registry()
        index = VectorHnswIndex(
            VectorIndexDefinition(
                name=f"vector_{table.name}_{space.name}",
                table_id=table.table_id,
                table_name=table.name,
                positions=(position,),
                visibility=IndexVisibility.PROXIMITY,
            ),
            self._pool,
            self._metrics.sink,
            space_id=space.space_id,
            space_name=space.name,
            dimension=space.dimension,
            metric_of_space=space.metric,
            storage_dtype=space.storage_dtype,
            math=self._math,
            resolve=self._resolver_for(space),
            seed=self._seed,
            neighbours=self._neighbours,
            ef_construction=self._ef_construction,
            ef_search=self._ef_search,
            guard=self._guard,
        )
        registry.register(index)
        self._by_space[space.name] = index
        self._maintained_at[space.name] = self._clock.monotonic()
        self._publish_space_metrics()
        return index

    def detach(self, space_name: str) -> bool:
        """Forget this engine's per-space state for ONE space, and say whether any was held.

        The caller is the undo of a refused schema STATEMENT: ``attach`` ran for a space the
        statement declared, the statement was then refused, and exactly that attachment must go.
        ``discard_unknown`` prunes against a whole catalog and is the rollback-of-a-transaction
        door; pruning by catalog here would also drop the attachments of ANOTHER open
        transaction, whose spaces are not in this one's base picture either. Registry entries
        are the index manager's and are unwound there by name. Never raises.
        """
        held = self._by_space.pop(space_name, None) is not None
        self._maintained_at.pop(space_name, None)
        return held

    def discard_unknown(self, catalog: object) -> tuple[str, ...]:
        """Drop the spaces this engine holds an index for and that catalog does not know.

        The rollback of a schema transaction is the caller: ``attach`` ran at statement time and
        installed per-space state here, and the transaction that declared the space is now not
        going to happen. The registry half is pruned by table id in the index manager; this is
        the vector engine's own map, which would otherwise answer ``index(space)`` for a space
        that does not exist. Returns the names dropped, and never raises -- it runs inside an
        unwind.
        """
        try:
            alive = {space.name for space in catalog.spaces()}
        except Exception:  # noqa: BLE001 - an unwind must not gain a second failure
            return ()
        dropped = tuple(name for name in self._by_space if name not in alive)
        for name in dropped:
            self._by_space.pop(name, None)
            self._maintained_at.pop(name, None)
        return dropped

    def _require_registry(self) -> IndexManager:
        """Return the index registry, refusing an engine that was built without one.

        There is exactly one index implementation and it lives in a paged file, so an engine with
        no registry cannot create a space's index at all. Refusing here is the fail-closed
        answer; the alternative -- a memory-resident index when no registry is present -- is the
        second implementation this component was told not to have.
        """
        if self._indexes is None or self._pool is None:
            raise GrafxConfigurationError(
                "A vector index lives in a paged file registered with the index manager, so the "
                "vector engine needs both `pool` and `indexes` to create one.",
                field="indexes",
                value=None,
            )
        return self._indexes

    def index(self, space_name: str) -> VectorHnswIndex:
        """Return the index of one embedding space, refusing a space with no index yet."""
        existing = self._by_space.get(space_name)
        if existing is None:
            raise GrafxIndexError(
                f"Embedding space {space_name!r} has no index; a table declaring a column in it "
                f"must be attached before it can be searched.",
                field="space",
                value=space_name,
            )
        return existing

    def indexes(self) -> Iterable[VectorHnswIndex]:
        """Return every index this engine holds, in space name order."""
        return tuple(self._by_space[name] for name in sorted(self._by_space))

    def _resolver_for(self, space: EmbeddingSpaceDef) -> VectorResolver:
        """Return the callable that reads the components of one version out of the heap."""

        def resolve(ref: RecordRef) -> tuple[RecordId, Sequence[float]]:
            """Return the record and the components of the version at a heap location."""
            version = self._heap.read(ref)
            return version.record_id, self._vector_of_version(
                space, version, ref
            ).values

        return resolve

    # --- validation ---------------------------------------------------------------------------

    def validate_vector(
        self, space: EmbeddingSpaceDef, values: Sequence[float]
    ) -> tuple[float, ...]:
        """Return the components as they will be stored, or refuse the write (FR-1, BR-5).

        This is the whole write door of the vector type. A wrong dimension, a NaN, an infinity, a
        component a float32 page could only hold as an infinity, or a vector that is not unit
        length in a space that declares normalized vectors -- each is refused here, before the
        caller has anything to persist, so nothing reaches the heap, the log or the index.
        """
        if not isinstance(space, EmbeddingSpaceDef):
            raise GrafxConfigurationError(
                f"A vector is validated against an EmbeddingSpaceDef; got "
                f"{type(space).__name__}.",
                field="space",
                value=type(space).__name__,
            )
        require_active(space)
        return validate_components(space, values)

    # --- index writes ---------------------------------------------------------------------------

    def stage_insert(
        self,
        space_name: str,
        record_id: RecordId,
        ref: RecordRef,
        values: Sequence[float],
        csn: Csn,
        txn: StagingTransaction,
    ) -> WalRecord:
        """Stage the index entry one vector write owes, validating the vector first.

        Nothing is installed: the record goes into the transaction, and the entry appears when
        that transaction commits. A refusal here therefore leaves the index, the heap and the log
        exactly as they were (BR-5).
        """
        space = self._catalog.catalog.space(space_name)
        require_active(space)
        stored = validate_components(space, values)
        index = self.index(space_name)
        del record_id
        return index.stage_insert(txn, index.key_for(stored), ref, csn)

    def stage_delete(
        self,
        space_name: str,
        record_id: RecordId,
        ref: RecordRef,
        values: Sequence[float],
        csn: Csn,
        txn: StagingTransaction,
    ) -> WalRecord:
        """Stage the tombstone of one vector version.

        A retired space still tombstones and still reconciles: retirement closes the door on new
        vectors, not on removing the ones already there, or a caller could never finish a
        migration it was told to drive.
        """
        space = self._catalog.catalog.space(space_name)
        index = self.index(space_name)
        del record_id
        return index.stage_delete(
            txn, index.key_for(validate_components(space, values)), ref, csn
        )

    def commit(self, space_name: str, txn: StagingTransaction, csn: Csn) -> int:
        """Apply everything a transaction staged into one space's index."""
        applied = self.index(space_name).commit(txn, csn)
        self._maintained_at[space_name] = self._clock.monotonic()
        self._publish_space_metrics()
        return applied

    def rollback(self, space_name: str, txn: StagingTransaction) -> int:
        """Drop everything a transaction staged into one space's index."""
        return self.index(space_name).rollback(txn)

    def reconcile(
        self, space_name: str, horizon: Lsn, txn: StagingTransaction | None = None
    ) -> object:
        """Measure or perform the horizon-bounded reconciliation of one space's index (BR-3).

        With no transaction the pass MEASURES and removes nothing, because a cleanup the log
        never saw is what BR-3 forbids. With one, every removal is staged as a log record and
        applied when that transaction commits.
        """
        index = self.index(space_name)
        report = index.reconcile(horizon, txn)
        self._emit(
            "vector.index_reconciled",
            space=space_name,
            removed=report.removed,
            reclaimable=report.reclaimable,
            horizon=horizon,
        )
        return report

    # --- searching ------------------------------------------------------------------------------

    def search(
        self,
        *,
        space: str,
        query: Sequence[float],
        k: int,
        snapshot: SnapshotLike,
        candidate_filter: CandidateFilter | None = None,
    ) -> VectorSearchResult:
        """Return the nearest neighbours of a query inside one embedding space.

        The regime is chosen from the expected size of the filtered set and is reported in the
        result; the neighbour count actually achieved is reported beside it. A query that declares
        a different space than the vectors it would be compared against is refused before a single
        distance is computed (BR-1).

        The size of the space is the count of entries no commit has ended. FR-5 asks for an
        estimate, and this is the estimate that describes the work: an exact scan reads every
        entry and discards the ones the snapshot cannot see.
        """
        started = self._reading()
        definition = self._catalog.catalog.space(space)
        _require_positive_k(k)
        _require_snapshot(snapshot)
        components = validate_query_components(definition, query)
        index = self.index(space)
        # Both regimes enumerate their candidates from the index -- the exact one reads the heap
        # to decide what each candidate IS, but it never learns about a row the index never
        # received. So an index behind the heap omits rows in both regimes, and the refusal
        # belongs here rather than only inside the traversal.
        index.require_readable()
        plan = plan_regime(
            space_size=index.live_count(),
            filter_cardinality=_filter_cardinality(candidate_filter),
            threshold=self._threshold,
        )
        self._observe_phase(plan.regime, PHASE_PLAN, started)
        if plan.is_exact:
            hits = self._search_exactly(
                definition, index, components, k, snapshot, candidate_filter
            )
        else:
            hits = self._search_approximately(
                definition, index, components, k, snapshot, candidate_filter
            )
        self._publish_search_metrics(plan, len(hits))
        return VectorSearchResult(
            hits=hits,
            regime=plan.regime,
            achieved_k=len(hits),
            requested_k=k,
            space=definition.name,
            filter_cardinality=plan.filter_cardinality,
        )

    def _search_exactly(
        self,
        space: EmbeddingSpaceDef,
        index: VectorHnswIndex,
        query: tuple[float, ...],
        k: int,
        snapshot: SnapshotLike,
        candidate_filter: CandidateFilter | None,
    ) -> tuple[VectorHit, ...]:
        """Scan the filtered set against the heap, which is the authority on what exists.

        Two passes, and the order is the rule. The first reads every candidate and establishes
        that it belongs to this space; the second scores. A cross-space candidate therefore fails
        before ANY distance has been computed and no partial ranking exists to hand back, which is
        what BR-1 requires and what a scan-and-score loop could not provide.
        """
        started = self._reading()
        admits = _guarded_admits(candidate_filter)
        candidates: list[tuple[int, tuple[float, ...]]] = []
        location: dict[int, RecordRef] = {}
        scanned: set[int] = set()
        for entry in index.walk():
            # Two entries may name one heap location -- an entry filed under a key the row no
            # longer carries sits beside the one that matches it, and the walk yields both. That
            # is a divergence for the verifier to report, not a reason to read the same row
            # twice: the traversal side dedupes by location because the graph holds one node per
            # location, and the scan has to agree with it or the two regimes would disagree on a
            # damaged index. The refusal below is for two DISTINCT locations showing one record,
            # which is the MVCC violation it was written for.
            encoded = entry.ref.encode()
            if encoded in scanned:
                continue
            scanned.add(encoded)
            version = self._heap.read(entry.ref)
            if not snapshot.visible(version.xmin, version.xmax):
                continue
            if admits is not None and not admits(version.record_id):
                continue
            stored = self._vector_of_version(space, version, entry.ref)
            require_space_identity(space, stored.space_ref, origin="stored vector")
            if version.record_id in location:
                raise _duplicate_version(
                    space, version.record_id, entry.ref, location[version.record_id]
                )
            location[version.record_id] = entry.ref
            candidates.append((version.record_id, stored.values))
        self._observe_phase(REGIME_EXACT, PHASE_TRAVERSE, started)
        started = self._reading()
        ranked = (
            self._math.top_k(query, candidates, k, space.metric) if candidates else []
        )
        retired = not space.is_active
        hits = tuple(
            VectorHit(
                record_id=record_id,
                score=score,
                ref=location[record_id],
                retired=retired,
            )
            for record_id, score in ranked
        )
        self._observe_phase(REGIME_EXACT, PHASE_VALIDATE, started)
        return hits

    def _search_approximately(
        self,
        space: EmbeddingSpaceDef,
        index: VectorHnswIndex,
        query: tuple[float, ...],
        k: int,
        snapshot: SnapshotLike,
        candidate_filter: CandidateFilter | None,
    ) -> tuple[VectorHit, ...]:
        """Traverse the versioned index, evaluating the filter during navigation."""
        started = self._reading()
        admits = _guarded_admits(candidate_filter)
        scored, _stats = index.search(query, k, snapshot, admits)
        self._observe_phase(REGIME_APPROXIMATE, PHASE_TRAVERSE, started)
        started = self._reading()
        retired = not space.is_active
        seen: dict[int, RecordRef] = {}
        for item in scored:
            previous = seen.get(item.record_id)
            if previous is not None:
                raise _duplicate_version(space, item.record_id, item.ref, previous)
            seen[item.record_id] = item.ref
        hits = tuple(
            VectorHit(
                record_id=item.record_id,
                score=item.score,
                ref=item.ref,
                retired=retired,
            )
            for item in scored
        )
        self._observe_phase(REGIME_APPROXIMATE, PHASE_VALIDATE, started)
        return hits

    def _vector_of_version(
        self, space: EmbeddingSpaceDef, version: HeapVersion, ref: RecordRef
    ) -> VectorValue:
        """Return the stored vector of one heap version, refusing a row that carries none."""
        table = self._catalog.catalog.table_by_id(version.table_id)
        position = self._vector_column_of(table, space)
        values = version.values
        if position >= len(values):
            raise GrafxIndexError(
                f"The row at page {ref.page} slot {ref.slot} has no value for the vector column "
                f"of table {table.name!r}.",
                field="values",
                space=space.name,
                page=ref.page,
                slot=ref.slot,
            )
        stored = values[position]
        if not isinstance(stored, VectorValue):
            raise GrafxVectorValidationError(
                f"The row at page {ref.page} slot {ref.slot} carries "
                f"{type(stored).__name__} where embedding space {space.name!r} expects a vector.",
                field="values",
                reason="not_a_vector",
                space=space.name,
                page=ref.page,
                slot=ref.slot,
            )
        return stored

    def _vector_column_of(self, table: TableDef, space: EmbeddingSpaceDef) -> int:
        """Return the position of the column of a table that stores vectors of one space."""
        for position, column in enumerate(table.columns):
            if column.is_vector and column.vector_space == space.name:
                return position
        for column in table.columns:
            if column.is_vector:
                raise GrafxIndexError(
                    f"Table {table.name!r} stores vectors of embedding space "
                    f"{column.vector_space!r}, not of {space.name!r}.",
                    field="vector_space",
                    space=space.name,
                    value=column.vector_space,
                )
        raise GrafxIndexError(
            f"Table {table.name!r} has no vector column, so it cannot take part in a search of "
            f"embedding space {space.name!r}.",
            field="vector_space",
            space=space.name,
            value=table.name,
        )

    # --- metrics and events -----------------------------------------------------------------------

    def _reading(self) -> float:
        """Return a monotonic reading when anything is collecting, and zero when nothing is."""
        return self._clock.monotonic() if self._metrics.enabled else 0.0

    def _publish(self, action: Callable[[], None]) -> None:
        """Run one publication, dropping a failure that belongs to the host sink.

        Observability is not a transaction participant: a sink supplied by the host may fail for
        reasons of its own, and a search that already produced a correct answer must not fail
        because nobody could be told about it -- nor may the host's exception leave this
        component, which promises only ``Grafx*`` types. A ``Grafx*`` failure is re-raised
        deliberately: that class means the emission itself was invalid, which is this component's
        defect and must stay loud.
        """
        try:
            action()
        except GrafxError:
            raise
        except Exception:  # noqa: BLE001 - a host sink never fails the operation it observes
            return

    def _observe_phase(self, regime: str, phase: str, started: float) -> None:
        """Record how long one phase of a search took, labelled by regime and phase."""
        if not self._metrics.enabled:
            return
        elapsed = self._clock.monotonic() - started
        if elapsed < 0.0:
            elapsed = 0.0
        self._publish(
            lambda: self._metrics.observe(
                _LATENCY, elapsed, {"regime": regime, "phase": phase}
            )
        )

    def _publish_search_metrics(self, plan: RegimePlan, achieved: int) -> None:
        """Publish what one search did: its regime, its selectivity and its neighbour count."""
        if plan.is_exact:
            self._publish(lambda: self._metrics.increment(_EXACT_FALLBACK))
        if not self._metrics.enabled:
            return
        self._publish(lambda: self._metrics.observe(_ACHIEVED_K, float(achieved)))
        self._publish(lambda: self._metrics.observe(_SELECTIVITY, plan.selectivity))

    def _publish_space_metrics(self) -> None:
        """Publish the size, the coverage and the age of every index this engine holds.

        Coverage is a space as a fraction of every vector the engine holds, so the coverage of the
        spaces sums to one while any vector exists at all. That is what makes the residual
        coverage of a retired space fall visibly as the caller migrates out of it (AC-3, AC-4).
        """
        if not self._metrics.enabled:
            return
        live = {name: index.live_count() for name, index in self._by_space.items()}
        total = sum(live.values())
        now = self._clock.monotonic()
        for name, index in self._by_space.items():
            labels = {"space": name}
            self._publish(
                lambda index=index, labels=labels: self._metrics.set_gauge(
                    _INDEX_ENTRIES, float(len(index.walk())), labels
                )
            )
            share = (live[name] / total) if total else 0.0
            self._publish(
                lambda share=share, labels=labels: self._metrics.set_gauge(
                    _COVERAGE, share, labels
                )
            )
            since = self._maintained_at.get(name)
            if since is not None:
                age = now - since
                self._publish(
                    lambda age=age, labels=labels: self._metrics.set_gauge(
                        _INDEX_AGE, age if age > 0.0 else 0.0, labels
                    )
                )

    def coverage(self) -> dict[str, float]:
        """Return the coverage of every space this engine holds, which sums to one or to zero."""
        live = {name: index.live_count() for name, index in self._by_space.items()}
        total = sum(live.values())
        if not total:
            return {name: 0.0 for name in live}
        return {name: count / total for name, count in live.items()}

    def _emit(self, event: str, **payload: object) -> None:
        """Notify the host of a lifecycle change, with this engine's invariants already held.

        The notice is the last step of an operation that has already completed, and it runs with
        no lock held because this component holds none (A91). A sink that fails is dropped: the
        space was created or retired either way.
        """
        if self._events is None:
            return
        sink = self._events
        self._publish(lambda: sink.emit(event, dict(payload)))

    def __repr__(self) -> str:
        return (
            f"VectorEngine(indexes={len(self._by_space)}, "
            f"exact_scan_threshold={self._threshold})"
        )
