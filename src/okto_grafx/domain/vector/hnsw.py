"""The filter-aware navigable small world graph of the vector index (SPEC-VEC FR-5, FR-6).

This is the ACORN-style half of the two-regime planner: a hierarchical graph whose traversal
evaluates the caller's predicate DURING neighbour expansion, so a node that fails the predicate
is never returned and still serves as a bridge to nodes that pass it (decision ``dec_fc3351c3``).
The alternative the specification rejects -- fetch a fixed multiple of k and post-filter -- is
what makes a filtered similarity search silently lose neighbours, and it is exactly the
compensation the consumer of record had to build in application code.

Connectivity is a guarantee, not a hope
---------------------------------------
Every live node sits on a doubly linked chain in insertion order, and the two chain edges are
part of the layer-zero neighbourhood of a node although they live outside the adjacency lists.
They are therefore never dropped by neighbour trimming, which is the one place a small world
graph loses reachability. Two things follow, and both are load-bearing:

* layer zero is connected at all times, so a traversal that does not stop early reaches every
  live node;
* a beam wide enough never to fill -- ``ef`` at least the number of live nodes -- therefore
  returns EXACTLY the exhaustive answer. That is what lets the exact regime and the approximate
  regime agree on a query that straddles the regime threshold, and it is a property this module
  provides rather than a coincidence of a particular corpus.

Trimming and removal are quality measures on top of that guarantee, never the guarantee itself:
removing a node relinks its former neighbours to each other so the graph does not decay into the
chain under churn, but no correctness claim rests on that relinking.

Determinism
-----------
Level assignment draws from a seeded ``SplitMix64`` owned by the index, so the same sequence of
insertions builds the same graph on every platform and in every run (G2b, A5). Every ordering
decision -- the beam, the result set, the neighbour selection -- breaks ties by ascending node
id, so no result depends on the iteration order of a set or a dictionary.
"""

from __future__ import annotations

from array import array
from bisect import insort
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from heapq import heappop, heappush
from math import log

from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.ports.vectormath import (
    DistanceMetric,
    PreparedCosineVectorMath,
    PreparedVectorMath,
    VectorMath,
)
from okto_grafx.domain.rand import SplitMix64

__all__ = [
    "DEFAULT_NEIGHBOURS",
    "DEFAULT_EF_CONSTRUCTION",
    "DEFAULT_EF_SEARCH",
    "MAX_EF_SEARCH",
    "MAX_LEVEL",
    "TraversalStats",
    "HnswGraph",
]

DEFAULT_NEIGHBOURS: int = 16
"""Neighbours kept per node above layer zero; layer zero keeps twice as many.

Sixteen is the value the original construction recommends and the one every published
implementation defaults to. It buys a graph whose degree is small enough for the beam to be the
cost of a search rather than the expansion, while keeping enough alternative routes that the
greedy descent is not trapped by a single bad edge.
"""

DEFAULT_EF_CONSTRUCTION: int = 200
"""How wide the beam is while building. Construction happens once; search happens forever."""

DEFAULT_EF_SEARCH: int = 320
"""Calibrated search beam, before the requested neighbour count raises it.

The 8,192-by-384 frozen recall corpus needs a beam materially wider than 64 to meet the 0.90
recall floor without turning the approximate regime into an exhaustive scan.  The calibration is
an observable runtime default rather than a harness-only override, so every newly composed vector
index starts from this value unless its database configuration says otherwise.
"""

MAX_EF_SEARCH: int = 1 << 20
"""Largest configurable base beam: one million graph nodes per approximate traversal.

``ef`` is working-set policy, not an on-disk field, so no format supplies a natural upper bound.
This operational ceiling is deliberately far above the calibrated default while refusing an
accidental giant integer before it can turn every approximate query into unbounded work.  A query
asking for more than this many results may still raise its effective beam to ``k``; this bound is
on the configured baseline, not on the result cardinality API.
"""

MAX_LEVEL: int = 32
"""The tallest tower a node may be given, which bounds the per-node cost of a graph."""

_HEAP_FRONTIER_MIN_NODES: int = 4096
"""Smallest graph where selective or exhaustive traversal uses a heap frontier.

This private algorithm threshold is independent of the public exact-scan threshold, even when
their defaults happen to have the same numeric value.
"""


def _require_positive(field: str, value: int) -> int:
    """Return a positive integer parameter, refusing anything that could not build a graph."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise GrafxConfigurationError(
            f"The {field} of a vector graph must be an integer; got {type(value).__name__}.",
            field=field,
            value=repr(value),
        )
    if value < 1:
        raise GrafxConfigurationError(
            f"The {field} of a vector graph must be at least 1; got {value}.",
            field=field,
            value=value,
        )
    return value


@dataclass(frozen=True, slots=True)
class TraversalStats:
    """What one traversal did, so a test can prove the predicate was applied during navigation.

    ``bridges`` counts nodes that were expanded although the predicate refused them. A traversal
    that reports zero bridges on a selective filter did not navigate through the graph; it
    post-filtered a neighbourhood, which is the shape SPEC-VEC BR-6 exists to forbid.
    """

    visited: int
    bridges: int
    admitted: int
    hops: int
    exhaustive: bool


def _rank_key(item: tuple[float, int]) -> tuple[float, int]:
    """Return the sort key of one scored node: best score first, then the lower node id."""
    return (-item[0], item[1])


def _insert_ranked(
    ranked: list[tuple[float, int]], score: float, node: int, limit: int | None = None
) -> None:
    """Insert one scored node into a list ordered best first, keeping at most ``limit`` of them.

    Ordering is descending by score and ascending by node id, which is the tie rule of the
    ``VectorMath`` port (CONTRACT.md section 4.6). The position is found by binary search on
    exactly that key, which keeps this materialized ranking total and observable. The wide
    traversal frontier may use a heap carrying both score and node id as its total-order key, but
    its bounded result ranking still passes through this helper. An entry equal to one already
    present goes after it, where the linear scan this replaces put it (VEC-5); a test holds the
    two to the same list.

    ``limit`` of None means the list is not truncated. The ordinary traversal beam uses that;
    the separate wide-frontier heap has the same no-drop property. A node dropped from either
    frontier was already marked visited and would never be expanded, silently breaking the
    exhaustiveness the wide-beam case depends on.
    """
    insort(ranked, (score, node), key=_rank_key)
    if limit is not None and len(ranked) > limit:
        del ranked[limit:]


class HnswGraph:
    """A hierarchical navigable small world graph over integer node ids.

    The graph owns the components it ranks, so a traversal costs no page read at all. Visibility,
    tombstones and the heap live one layer up in the index; this class knows only points, edges
    and a predicate.
    """

    __slots__ = (
        "_math",
        "_prepare",
        "_prepare_cosine_with_norm",
        "_metric",
        "_neighbours",
        "_neighbours_zero",
        "_ef_construction",
        "_component_typecode",
        "_random",
        "_level_scale",
        "_values",
        "_norms",
        "_levels",
        "_links",
        "_construction_link_scores",
        "_chain_next",
        "_chain_previous",
        "_chain_head",
        "_chain_tail",
        "_entry_point",
        "_top_level",
    )

    def __init__(
        self,
        math: VectorMath,
        metric: DistanceMetric,
        *,
        seed: int,
        neighbours: int = DEFAULT_NEIGHBOURS,
        ef_construction: int = DEFAULT_EF_CONSTRUCTION,
        _component_typecode: str | None = None,
        _cache_construction_link_scores: bool = False,
    ) -> None:
        """Build an empty graph whose shape is decided by the seed and the neighbour count.

        ``_component_typecode`` is an engine-only residency optimization.  The ordinary public
        construction keeps tuples exactly as before; an engine that already selected the NumPy
        vector adapter may ask for ``f`` or ``d`` storage.  Compact components are owned as
        immutable bytes, and every call into host ``VectorMath`` receives a fresh read-only view,
        so releasing that view cannot poison the graph retained for a later search.
        """
        self._math = math
        self._prepare = math.prepare if isinstance(math, PreparedVectorMath) else None
        self._prepare_cosine_with_norm = (
            math.prepare_cosine_with_norm
            if isinstance(math, PreparedCosineVectorMath)
            else None
        )
        self._metric = metric
        self._neighbours = _require_positive("neighbours", neighbours)
        self._neighbours_zero = self._neighbours * 2
        self._ef_construction = _require_positive("ef_construction", ef_construction)
        if _component_typecode not in (None, "f", "d"):
            raise GrafxConfigurationError(
                "A compact vector graph stores either float32 ('f') or float64 ('d') "
                f"components; got {_component_typecode!r}.",
                field="component_typecode",
                value=repr(_component_typecode),
            )
        self._component_typecode = _component_typecode
        self._random = SplitMix64(seed)
        self._level_scale = 1.0 / log(self._neighbours) if self._neighbours > 1 else 1.0
        self._values: dict[int, tuple[float, ...] | bytes] = {}
        # Successful candidate norms are exact, process-local derivatives of immutable graph
        # components.  They are populated only after the ordinary scorer succeeds, so a failed
        # score retains its established timing and never leaves a cached authorization behind.
        self._norms: dict[int, tuple[tuple[float, ...] | bytes, float]] = {}
        self._levels: dict[int, int] = {}
        self._links: list[dict[int, list[int]]] = [{}]
        # A cold engine build may opt into a transient score beside each full adjacency.  Once
        # a node has overflowed, every later link used to rescore all of its unchanged peers just
        # to rank one new peer.  The parallel lists retain those already-proved values only while
        # the graph is being derived; ``_finish_construction`` drops the complete cache before the
        # picture can be published.  Ordinary/public HnswGraph construction keeps the historical
        # callback behaviour unless its caller explicitly selects this private capability.
        self._construction_link_scores: list[
            dict[int, list[float | None]]
        ] | None = (
            [{}] if _cache_construction_link_scores else None
        )
        self._chain_next: dict[int, int] = {}
        self._chain_previous: dict[int, int] = {}
        self._chain_head: int | None = None
        self._chain_tail: int | None = None
        self._entry_point: int | None = None
        self._top_level: int = 0

    # --- reading -------------------------------------------------------------------------

    def __len__(self) -> int:
        """Return how many nodes the graph holds."""
        return len(self._values)

    def __contains__(self, node: object) -> bool:
        """Return True when the graph holds a node with that id."""
        return node in self._values

    @property
    def entry_point(self) -> int | None:
        """Return the node every traversal starts from, or None while the graph is empty."""
        return self._entry_point

    @property
    def top_level(self) -> int:
        """Return the highest layer any node currently reaches."""
        return self._top_level

    def nodes(self) -> tuple[int, ...]:
        """Return every node id in ascending order, which is a stable order on every run."""
        return tuple(sorted(self._values))

    def level_of(self, node: int) -> int:
        """Return the height of the tower a node was given when it entered the graph.

        The height is the one thing the seeded generator decides, so this is what a test reads
        to show that the shape of a graph follows its seed and nothing else.
        """
        try:
            return self._levels[node]
        except KeyError as failure:
            raise GrafxConfigurationError(
                f"The vector graph holds no node {node!r}.",
                field="node",
                value=repr(node),
            ) from failure

    def values_of(self, node: int) -> tuple[float, ...]:
        """Return the components stored for one node as the established public tuple."""
        try:
            stored = self._values[node]
        except KeyError as failure:
            raise GrafxConfigurationError(
                f"The vector graph holds no node {node!r}.",
                field="node",
                value=repr(node),
            ) from failure
        if isinstance(stored, tuple):
            return stored
        return tuple(self._view(stored))

    def neighbours_of(self, node: int, layer: int) -> tuple[int, ...]:
        """Return the neighbours of a node at one layer, chain edges included at layer zero.

        The chain edges are part of the neighbourhood although they are not part of the
        adjacency lists. Keeping them outside the lists is what makes them unprunable: no
        trimming pass can see them, so no trimming pass can disconnect the graph.
        """
        result: list[int] = []
        if 0 <= layer < len(self._links):
            result.extend(self._links[layer].get(node, ()))
        if layer == 0:
            previous = self._chain_previous.get(node)
            following = self._chain_next.get(node)
            if previous is not None and previous not in result:
                result.append(previous)
            if following is not None and following not in result:
                result.append(following)
        return tuple(result)

    def chain(self) -> tuple[int, ...]:
        """Return the live nodes in chain order, which is the order they were inserted in."""
        order: list[int] = []
        current = self._chain_head
        budget = len(self._values) + 1
        while current is not None and budget > 0:
            order.append(current)
            current = self._chain_next.get(current)
            budget -= 1
        return tuple(order)

    # --- writing -------------------------------------------------------------------------

    def insert(self, node: int, values: Sequence[float]) -> None:
        """Add one node to the graph, linking it into every layer of its tower."""
        if node in self._values:
            raise GrafxConfigurationError(
                f"The vector graph already holds a node {node!r}; a version enters the graph "
                f"once.",
                field="node",
                value=repr(node),
            )
        components = tuple(float(component) for component in values)
        stored: tuple[float, ...] | bytes = components
        if self._component_typecode is not None:
            # ``array`` is only the transient packer.  Keeping its bytes, rather than the mutable
            # array or a releasable memoryview, makes the resident component storage immutable.
            stored = array(self._component_typecode, components).tobytes()
        level = self._draw_level()
        self._values[node] = stored
        self._levels[node] = level
        self._append_to_chain(node)
        while len(self._links) <= level:
            self._links.append({})
            scores = self._construction_link_scores
            if scores is not None:
                scores.append({})
        if self._entry_point is None:
            self._entry_point = node
            self._top_level = level
            return
        scorer = self._scorer(self._components(node))
        current = self._entry_point
        for layer in range(self._top_level, level, -1):
            current = self._descend(scorer, current, layer)
        for layer in range(min(level, self._top_level), -1, -1):
            found = self._search_layer(
                scorer, (current,), self._ef_construction, layer, None
            )
            capacity = self._capacity(layer)
            for _score, neighbour in found[:capacity]:
                self._link(node, neighbour, layer)
            if found:
                current = found[0][1]
        if level > self._top_level:
            self._top_level = level
            self._entry_point = node

    def _finish_construction(self) -> None:
        """Release transient link scores before this derived graph is published.

        Incremental maintenance intentionally retains the canonical scalar trimming path.  The
        cache exists only to avoid repeated work while a complete cold picture is assembled in
        locals, and keeping it afterwards would turn a build-time speedup into permanent O(E)
        duplicate residency.
        """
        self._construction_link_scores = None

    def remove(self, node: int) -> None:
        """Take one node out of the graph, keeping the remaining nodes reachable.

        The chain is spliced, which is what preserves connectivity. The former neighbours of the
        node are then linked to each other, which preserves the QUALITY of the neighbourhood --
        without it a heavily reconciled graph decays towards the bare chain. No correctness claim
        rests on the relinking; the splice is the guarantee.
        """
        if node not in self._values:
            raise GrafxConfigurationError(
                f"The vector graph holds no node {node!r}.",
                field="node",
                value=repr(node),
            )
        for layer in range(len(self._links)):
            adjacency = self._links[layer]
            orphans = tuple(adjacency.pop(node, ()))
            scores = self._construction_link_scores
            if scores is not None:
                scores[layer].pop(node, None)
            for neighbour in orphans:
                self._unlink(neighbour, node, layer)
            for position in range(len(orphans) - 1):
                self._link(orphans[position], orphans[position + 1], layer)
        self._splice_from_chain(node)
        del self._values[node]
        self._norms.pop(node, None)
        del self._levels[node]
        if self._entry_point == node:
            self._elect_entry_point()

    # --- searching -----------------------------------------------------------------------

    def search(
        self,
        query: Sequence[float],
        ef: int,
        admits: Callable[[int], bool] | None = None,
    ) -> tuple[tuple[tuple[float, int], ...], TraversalStats]:
        """Return the best admitted nodes for a query, with what the traversal did to find them.

        ``admits`` is evaluated during expansion, never afterwards: a node it refuses is expanded
        as a bridge and is absent from the result. Passing None admits everything, which is what
        construction uses.
        """
        _require_positive("ef", ef)
        if self._entry_point is None:
            return (), TraversalStats(
                visited=0, bridges=0, admitted=0, hops=0, exhaustive=True
            )
        scorer = self._scorer(tuple(float(component) for component in query))
        current = self._entry_point
        hops = 0
        for layer in range(self._top_level, 0, -1):
            current, layer_hops = self._descend_counted(scorer, current, layer)
            hops += layer_hops
        ranked, stats = self._search_layer_counted(scorer, (current,), ef, 0, admits)
        return ranked, TraversalStats(
            visited=stats.visited,
            bridges=stats.bridges,
            admitted=stats.admitted,
            hops=hops + stats.hops,
            exhaustive=stats.exhaustive,
        )

    def is_connected_at_layer_zero(self) -> bool:
        """Return True when every live node is reachable from the entry point at layer zero.

        This is the invariant the chain exists to provide, exposed so a test can assert it after
        arbitrary sequences of insertion and removal rather than trusting the argument for it.
        """
        if self._entry_point is None:
            return not self._values
        seen = {self._entry_point}
        frontier = [self._entry_point]
        while frontier:
            node = frontier.pop()
            for neighbour in self.neighbours_of(node, 0):
                if neighbour not in seen:
                    seen.add(neighbour)
                    frontier.append(neighbour)
        return len(seen) == len(self._values)

    # --- internals -----------------------------------------------------------------------

    def _capacity(self, layer: int) -> int:
        """Return how many adjacency neighbours a node keeps at one layer."""
        return self._neighbours_zero if layer == 0 else self._neighbours

    def _draw_level(self) -> int:
        """Return the tower height of the next node, from the seeded generator only."""
        draw = 1.0 - self._random.next_float()
        level = int(-log(draw) * self._level_scale)
        return level if level < MAX_LEVEL else MAX_LEVEL

    def _view(self, stored: bytes) -> memoryview:
        """Return an ephemeral read-only numeric view over one immutable component body."""
        typecode = self._component_typecode
        if (
            typecode is None
        ):  # pragma: no cover - guarded by the bytes-only compact invariant
            raise AssertionError("compact HNSW bytes require a component typecode")
        return memoryview(stored).cast(typecode)

    def _components(self, node: int) -> Sequence[float]:
        """Return one node's tuple or a fresh compact view safe for a host to release."""
        return self._stored_components(self._values[node])

    def _stored_components(
        self, stored: tuple[float, ...] | bytes
    ) -> Sequence[float]:
        """Return a fresh view over one captured immutable backing generation."""
        return stored if isinstance(stored, tuple) else self._view(stored)

    def _scorer(self, query: Sequence[float]) -> Callable[[int], float]:
        """Return a function scoring stored nodes against one query, prepared once (VEC-4).

        A traversal scores one query against every node it visits, so whatever depends on the
        query alone is computed once here and reused, when the math adapter declares the
        ``PreparedVectorMath`` capability. An adapter that implements only ``VectorMath`` is
        scored through ``score`` exactly as before. Both paths answer the same number for the
        same pair, and a test holds them to identical graphs, rankings and traversal counts.
        """
        if (
            self._metric is DistanceMetric.COSINE
            and self._prepare_cosine_with_norm is not None
        ):
            measured, cached = self._prepare_cosine_with_norm(query)

            def cosine(node: int) -> float:
                """Compute cosine similarity using the retained query or candidate norm when available."""
                stored = self._values[node]
                retained = self._norms.get(node)
                if retained is not None and retained[0] is stored:
                    return cached(self._stored_components(stored), retained[1])
                # The measuring scorer follows the legacy operation order and returns the norm
                # it actually used.  Publish only if re-entry did not replace this node's
                # immutable backing while host math was running; a refusal returns no pair and
                # therefore caches nothing.
                result, right_norm = measured(self._stored_components(stored))
                if self._values.get(node) is stored:
                    self._norms[node] = (stored, right_norm)
                    # A thread may remove this generation between the comparison and the cache
                    # write.  Revalidate after publication and retire only our own stale value;
                    # this keeps churn bounded without holding a lock across host vector math.
                    if self._values.get(node) is not stored:
                        published = self._norms.get(node)
                        if published is not None and published[0] is stored:
                            self._norms.pop(node, None)
                return result

            return cosine
        if self._prepare is not None:
            prepared = self._prepare(query, self._metric)
            return lambda node: prepared(self._components(node))
        math = self._math
        metric = self._metric
        return lambda node: math.score(query, self._components(node), metric)

    def _link(self, left: int, right: int, layer: int) -> None:
        """Connect two nodes at one layer and trim both neighbourhoods back to capacity."""
        if left == right or left not in self._values or right not in self._values:
            return
        adjacency = self._links[layer]
        cached_by_node = (
            None
            if self._construction_link_scores is None
            else self._construction_link_scores[layer]
        )
        for owner, other in ((left, right), (right, left)):
            peers = adjacency.setdefault(owner, [])
            if other not in peers:
                cached = None if cached_by_node is None else cached_by_node.get(owner)
                if cached is not None and len(cached) != len(peers):
                    # Only an exactly aligned prefix is reusable.  Forget an uncertain cache
                    # before mutating the adjacency rather than guessing which score belongs to
                    # which peer.
                    cached_by_node.pop(owner, None)
                    cached = None
                peers.append(other)
                if cached is not None:
                    # Underfull adjacencies are not scored early.  The placeholder keeps the
                    # positional proof until a later overflow actually needs this pair.
                    cached.append(None)
        self._trim(left, layer)
        self._trim(right, layer)

    def _trim(self, node: int, layer: int) -> None:
        """Keep only the closest neighbours of a node at one layer, in a deterministic order.

        Trimming drops the edge on BOTH sides. An adjacency that is allowed to become one-way is
        an adjacency a removal cannot clean up: the node would be taken out of the lists it knows
        about and would stay in the lists that know about it, leaving an edge pointing at a node
        that no longer exists. Keeping the relation symmetric is what makes removal a local
        operation instead of a scan of the whole layer -- and it costs no reachability, because
        reachability is the chain's job and the chain is not an adjacency list.
        """
        adjacency = self._links[layer]
        peers = adjacency.get(node)
        capacity = self._capacity(layer)
        if peers is None or len(peers) <= capacity:
            return
        scorer = self._scorer(self._components(node))
        cached_by_node = (
            None
            if self._construction_link_scores is None
            else self._construction_link_scores[layer]
        )
        cached = None if cached_by_node is None else cached_by_node.get(node)
        if cached is not None and len(cached) == len(peers):
            # Reuse every proved score and resolve only placeholders, in the canonical peer
            # order.  ``ranked`` is local: if a later scorer fails, no partially refreshed cache
            # is published into the disposable graph picture.
            ranked = [
                (scorer(peer) if known is None else known, peer)
                for known, peer in zip(cached, peers, strict=True)
            ]
        else:
            # The first overflow and every uncertain alignment retain the canonical complete
            # scoring order.  A failed score publishes no cache and leaves the caller to discard
            # or retire the containing graph exactly as before.
            ranked = [(scorer(peer), peer) for peer in peers]
        ranked.sort(key=_rank_key)
        retained_pairs = ranked[:capacity]
        kept = [peer for _score, peer in retained_pairs]
        adjacency[node] = kept
        if cached_by_node is not None:
            cached_by_node[node] = [score for score, _peer in retained_pairs]
        retained = set(kept)
        for peer in peers:
            if peer in retained:
                continue
            self._unlink(peer, node, layer)

    def _unlink(self, owner: int, peer: int, layer: int) -> None:
        """Remove one directed adjacency and retire a transient cache if it is uncertain."""
        adjacency = self._links[layer]
        peers = adjacency.get(owner)
        if peers is None:
            return
        try:
            position = peers.index(peer)
        except ValueError:
            return
        scores = self._construction_link_scores
        cached_by_node = None if scores is None else scores[layer]
        cached = None if cached_by_node is None else cached_by_node.get(owner)
        aligned = cached is not None and len(cached) == len(peers)
        peers.pop(position)
        if cached is None:
            return
        if aligned:
            cached.pop(position)
        else:
            # Mismatch predates this removal.  The adjacency remains authoritative; only the
            # uncertain optimization is discarded.
            assert cached_by_node is not None
            cached_by_node.pop(owner, None)

    def _append_to_chain(self, node: int) -> None:
        """Put a node at the end of the connectivity chain."""
        if self._chain_tail is None:
            self._chain_head = node
            self._chain_tail = node
            return
        self._chain_next[self._chain_tail] = node
        self._chain_previous[node] = self._chain_tail
        self._chain_tail = node

    def _splice_from_chain(self, node: int) -> None:
        """Take a node out of the connectivity chain, joining its two sides to each other."""
        previous = self._chain_previous.pop(node, None)
        following = self._chain_next.pop(node, None)
        if previous is not None:
            if following is None:
                self._chain_next.pop(previous, None)
            else:
                self._chain_next[previous] = following
        if following is not None:
            if previous is None:
                self._chain_previous.pop(following, None)
            else:
                self._chain_previous[following] = previous
        if self._chain_head == node:
            self._chain_head = following
        if self._chain_tail == node:
            self._chain_tail = previous

    def _elect_entry_point(self) -> None:
        """Choose the entry point again after the previous one left the graph.

        The tallest node wins and the lowest id breaks a tie, so the choice depends on the graph
        and never on the order a dictionary happened to hand its keys back.
        """
        best: int | None = None
        best_level = -1
        for node in sorted(self._values):
            level = self._levels[node]
            if level > best_level:
                best = node
                best_level = level
        self._entry_point = best
        self._top_level = best_level if best is not None else 0

    def _descend(self, scorer: Callable[[int], float], start: int, layer: int) -> int:
        """Return the closest node to the query reachable by greedy steps at one layer."""
        node, _hops = self._descend_counted(scorer, start, layer)
        return node

    def _descend_counted(
        self, scorer: Callable[[int], float], start: int, layer: int
    ) -> tuple[int, int]:
        """Greedily walk downhill at one layer, returning the arrival and the steps taken.

        Each step takes the single best neighbour, and a tie is broken by the lower node id. The
        pair of score and negated id therefore increases strictly on every step, so the walk
        cannot revisit a node; the step budget is a second bound that a damaged graph would need
        and a correct one never reaches.
        """
        current = start
        current_score = scorer(current)
        hops = 0
        budget = len(self._values) + 1
        while budget > 0:
            budget -= 1
            best = current
            best_score = current_score
            for neighbour in sorted(self.neighbours_of(current, layer)):
                score = scorer(neighbour)
                if score > best_score or (score == best_score and neighbour < best):
                    best = neighbour
                    best_score = score
            if best == current:
                break
            current = best
            current_score = best_score
            hops += 1
        return current, hops

    def _search_layer(
        self,
        scorer: Callable[[int], float],
        entry_points: Iterable[int],
        ef: int,
        layer: int,
        admits: Callable[[int], bool] | None,
    ) -> tuple[tuple[float, int], ...]:
        """Return the best admitted nodes at one layer, discarding the traversal statistics."""
        ranked, _stats = self._search_layer_counted(
            scorer, entry_points, ef, layer, admits
        )
        return ranked

    def _search_layer_counted(
        self,
        scorer: Callable[[int], float],
        entry_points: Iterable[int],
        ef: int,
        layer: int,
        admits: Callable[[int], bool] | None,
    ) -> tuple[tuple[tuple[float, int], ...], TraversalStats]:
        """Run one beam search, evaluating the predicate during expansion (the ACORN rule).

        The beam holds every node worth expanding, admitted or not; the result holds only the
        admitted ones. A node the predicate refuses therefore costs one score and continues to
        carry the traversal towards nodes that do pass -- the bridge behaviour a filtered search
        needs, and the reason this never becomes fetch-more-and-post-filter.

        The loop stops when the best remaining candidate cannot beat the worst kept result AND
        the result set is full. When the beam is at least as wide as the number of live nodes the
        result can never fill, so the loop runs until the beam is empty and the traversal is
        exhaustive: that is the case in which this regime and an exact scan must agree, and
        ``TraversalStats.exhaustive`` records that it happened.

        The same fullness term has a second, unwanted consequence, and it is named here rather
        than left for a reader to discover. A SELECTIVE FILTER also keeps the result set from
        filling -- only admitted nodes are kept -- so a filter that admits fewer than ``ef``
        candidates disables the pruning entirely and the traversal visits the whole graph.
        Measured on 800 nodes: 666 visited unfiltered, 800 under a one-in-seven filter and 800
        under one-in-ninety-seven. It is why recall is 1.0 in exactly those cases, and it is the
        opposite of what a regime chosen to avoid an O(n) scan is for.

        Bounding it is a real design decision rather than a patch, because the exhaustiveness
        this term produces is also what makes the two regimes provably agree at the threshold.
        A visit budget would trade that proof, and the recall that comes with it, for latency.
        The behaviour is therefore left as it is and recorded, so the trade is made deliberately
        by whoever calibrates it rather than accidentally here.
        """
        node_count = len(self._values)
        if node_count >= _HEAP_FRONTIER_MIN_NODES and (
            admits is not None or ef >= node_count
        ):
            return self._search_layer_counted_heap(
                scorer, entry_points, ef, layer, admits
            )

        visited: set[int] = set()
        beam: list[tuple[float, int]] = []
        results: list[tuple[float, int]] = []
        bridges = 0
        hops = 0
        for node in sorted(entry_points):
            if node in visited or node not in self._values:
                continue
            visited.add(node)
            score = scorer(node)
            _insert_ranked(beam, score, node)
            if admits is None or admits(node):
                _insert_ranked(results, score, node, ef)
            else:
                bridges += 1
        while beam:
            score, node = beam.pop(0)
            if len(results) >= ef and score < results[-1][0]:
                break
            hops += 1
            for neighbour in sorted(self.neighbours_of(node, layer)):
                if neighbour in visited:
                    continue
                visited.add(neighbour)
                neighbour_score = scorer(neighbour)
                if len(results) < ef or neighbour_score > results[-1][0]:
                    _insert_ranked(beam, neighbour_score, neighbour)
                    if admits is None or admits(neighbour):
                        _insert_ranked(results, neighbour_score, neighbour, ef)
                    else:
                        bridges += 1
        return tuple(results), TraversalStats(
            visited=len(visited),
            bridges=bridges,
            admitted=len(results),
            hops=hops,
            exhaustive=len(visited) >= len(self._values),
        )

    def _search_layer_counted_heap(
        self,
        scorer: Callable[[int], float],
        entry_points: Iterable[int],
        ef: int,
        layer: int,
        admits: Callable[[int], bool] | None,
    ) -> tuple[tuple[tuple[float, int], ...], TraversalStats]:
        """Run the same beam search with a heap only for predictably wide frontiers.

        The ordinary approximate path above intentionally remains the established sorted-list
        implementation: its list shifts happen in C and win for the usual bounded beam. Large
        selective searches can fail to fill the result beam, while a beam at least as wide as the
        graph cannot prune early. In either case the frontier can grow towards N; choosing this
        helper up front removes quadratic list shifts without adding a per-visit mode branch to
        the common path.
        """
        visited: set[int] = set()
        # ``(-score, node, score)`` pops score descending and node ascending while retaining the
        # original score value (including signed zero) for the unchanged strict pruning checks.
        beam: list[tuple[float, int, float]] = []
        results: list[tuple[float, int]] = []
        bridges = 0
        hops = 0
        for node in sorted(entry_points):
            if node in visited or node not in self._values:
                continue
            visited.add(node)
            score = scorer(node)
            heappush(beam, (-score, node, score))
            if admits is None or admits(node):
                _insert_ranked(results, score, node, ef)
            else:
                bridges += 1
        while beam:
            _priority, node, score = heappop(beam)
            if len(results) >= ef and score < results[-1][0]:
                break
            hops += 1
            for neighbour in sorted(self.neighbours_of(node, layer)):
                if neighbour in visited:
                    continue
                visited.add(neighbour)
                neighbour_score = scorer(neighbour)
                if len(results) < ef or neighbour_score > results[-1][0]:
                    heappush(beam, (-neighbour_score, neighbour, neighbour_score))
                    if admits is None or admits(neighbour):
                        _insert_ranked(results, neighbour_score, neighbour, ef)
                    else:
                        bridges += 1
        return tuple(results), TraversalStats(
            visited=len(visited),
            bridges=bridges,
            admitted=len(results),
            hops=hops,
            exhaustive=len(visited) >= len(self._values),
        )

    def __repr__(self) -> str:
        return (
            f"HnswGraph(nodes={len(self._values)}, top_level={self._top_level}, "
            f"neighbours={self._neighbours})"
        )
