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

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from math import log

from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.ports.vectormath import DistanceMetric, VectorMath
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


def _insert_ranked(
    ranked: list[tuple[float, int]], score: float, node: int, limit: int | None = None
) -> None:
    """Insert one scored node into a list ordered best first, keeping at most ``limit`` of them.

    Ordering is descending by score and ascending by node id, which is the tie rule of the
    ``VectorMath`` port (CONTRACT.md section 4.6). Doing it by hand rather than by a heap keeps
    the order total and observable: a heap would leave equal scores in whatever order the sift
    happened to produce.

    ``limit`` of None means the list is not truncated. The beam of a traversal uses that,
    because a node dropped from the beam is a node that was marked visited and will never be
    expanded -- which would silently break the exhaustiveness the wide-beam case depends on.
    """
    for position, existing in enumerate(ranked):
        if score > existing[0] or (score == existing[0] and node < existing[1]):
            ranked.insert(position, (score, node))
            break
    else:
        ranked.append((score, node))
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
        "_metric",
        "_neighbours",
        "_neighbours_zero",
        "_ef_construction",
        "_random",
        "_level_scale",
        "_values",
        "_levels",
        "_links",
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
    ) -> None:
        """Build an empty graph whose shape is decided by the seed and the neighbour count."""
        self._math = math
        self._metric = metric
        self._neighbours = _require_positive("neighbours", neighbours)
        self._neighbours_zero = self._neighbours * 2
        self._ef_construction = _require_positive("ef_construction", ef_construction)
        self._random = SplitMix64(seed)
        self._level_scale = 1.0 / log(self._neighbours) if self._neighbours > 1 else 1.0
        self._values: dict[int, tuple[float, ...]] = {}
        self._levels: dict[int, int] = {}
        self._links: list[dict[int, list[int]]] = [{}]
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
        """Return the components stored for one node."""
        try:
            return self._values[node]
        except KeyError as failure:
            raise GrafxConfigurationError(
                f"The vector graph holds no node {node!r}.",
                field="node",
                value=repr(node),
            ) from failure

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
        level = self._draw_level()
        self._values[node] = components
        self._levels[node] = level
        self._append_to_chain(node)
        while len(self._links) <= level:
            self._links.append({})
        if self._entry_point is None:
            self._entry_point = node
            self._top_level = level
            return
        current = self._entry_point
        for layer in range(self._top_level, level, -1):
            current = self._descend(components, current, layer)
        for layer in range(min(level, self._top_level), -1, -1):
            found = self._search_layer(
                components, (current,), self._ef_construction, layer, None
            )
            capacity = self._capacity(layer)
            for _score, neighbour in found[:capacity]:
                self._link(node, neighbour, layer)
            if found:
                current = found[0][1]
        if level > self._top_level:
            self._top_level = level
            self._entry_point = node

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
            for neighbour in orphans:
                peers = adjacency.get(neighbour)
                if peers is not None and node in peers:
                    peers.remove(node)
            for position in range(len(orphans) - 1):
                self._link(orphans[position], orphans[position + 1], layer)
        self._splice_from_chain(node)
        del self._values[node]
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
        components = tuple(float(component) for component in query)
        current = self._entry_point
        hops = 0
        for layer in range(self._top_level, 0, -1):
            current, layer_hops = self._descend_counted(components, current, layer)
            hops += layer_hops
        ranked, stats = self._search_layer_counted(components, (current,), ef, 0, admits)
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

    def _score(self, query: tuple[float, ...], node: int) -> float:
        """Return the similarity of a stored node to the query, higher meaning closer."""
        return self._math.score(query, self._values[node], self._metric)

    def _link(self, left: int, right: int, layer: int) -> None:
        """Connect two nodes at one layer and trim both neighbourhoods back to capacity."""
        if left == right or left not in self._values or right not in self._values:
            return
        adjacency = self._links[layer]
        for owner, other in ((left, right), (right, left)):
            peers = adjacency.setdefault(owner, [])
            if other not in peers:
                peers.append(other)
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
        values = self._values[node]
        ranked: list[tuple[float, int]] = []
        for peer in peers:
            _insert_ranked(ranked, self._score(values, peer), peer, capacity)
        kept = [peer for _score, peer in ranked]
        adjacency[node] = kept
        retained = set(kept)
        for peer in peers:
            if peer in retained:
                continue
            other = adjacency.get(peer)
            if other is not None and node in other:
                other.remove(node)

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

    def _descend(self, query: tuple[float, ...], start: int, layer: int) -> int:
        """Return the closest node to the query reachable by greedy steps at one layer."""
        node, _hops = self._descend_counted(query, start, layer)
        return node

    def _descend_counted(
        self, query: tuple[float, ...], start: int, layer: int
    ) -> tuple[int, int]:
        """Greedily walk downhill at one layer, returning the arrival and the steps taken.

        Each step takes the single best neighbour, and a tie is broken by the lower node id. The
        pair of score and negated id therefore increases strictly on every step, so the walk
        cannot revisit a node; the step budget is a second bound that a damaged graph would need
        and a correct one never reaches.
        """
        current = start
        current_score = self._score(query, current)
        hops = 0
        budget = len(self._values) + 1
        while budget > 0:
            budget -= 1
            best = current
            best_score = current_score
            for neighbour in sorted(self.neighbours_of(current, layer)):
                score = self._score(query, neighbour)
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
        query: tuple[float, ...],
        entry_points: Iterable[int],
        ef: int,
        layer: int,
        admits: Callable[[int], bool] | None,
    ) -> tuple[tuple[float, int], ...]:
        """Return the best admitted nodes at one layer, discarding the traversal statistics."""
        ranked, _stats = self._search_layer_counted(query, entry_points, ef, layer, admits)
        return ranked

    def _search_layer_counted(
        self,
        query: tuple[float, ...],
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
        visited: set[int] = set()
        beam: list[tuple[float, int]] = []
        results: list[tuple[float, int]] = []
        bridges = 0
        hops = 0
        for node in sorted(entry_points):
            if node in visited or node not in self._values:
                continue
            visited.add(node)
            score = self._score(query, node)
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
                neighbour_score = self._score(query, neighbour)
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

    def __repr__(self) -> str:
        return (
            f"HnswGraph(nodes={len(self._values)}, top_level={self._top_level}, "
            f"neighbours={self._neighbours})"
        )
