"""Quota windows preserve the scalar refusal boundary and native charging."""
from __future__ import annotations

import pytest
from itertools import islice

from okto_grafx.engine import query_engine as qe
from okto_grafx.engine.index_manager import IndexStore
from okto_grafx.domain.index.keys import record_id_key
from okto_grafx.errors import GrafxError, GrafxCorruptionDetected
from . import test_batched_landing_without_vectors as fixture
from .test_batched_landing_traversal import _observe, _target_index
from .work_statistics import comparable_statistics

graph = fixture.graph
wide_graph = fixture.wide_graph
PREFIX = fixture.PREFIX
WIDE_QUERY = fixture.WIDE_QUERY


@pytest.mark.parametrize("option", ["_max_intermediate_rows", "_max_traversal_paths", "_max_traversal_expansions"])
@pytest.mark.parametrize("limit", [1, 2, 5, 6, 7, 1000])
def test_quota_window_matches_scalar_outcome(graph, monkeypatch, option, limit):
    calls = _observe(monkeypatch)
    monkeypatch.setattr(graph._queries, option, limit)
    query = PREFIX + "RETURN b.id, r.rank ORDER BY b.id, r.rank"

    def outcome():
        try:
            result = graph.execute(query)
            return result.rows, comparable_statistics(result.statistics)
        except GrafxError as error:
            return error.code, dict(error.details)

    candidate = outcome()
    if limit == 1000:
        assert calls == [3]
    monkeypatch.setattr(qe, "_admits_batched_landings", lambda *_: False)
    assert candidate == outcome()
    assert graph._queries._owner_budget._used_bytes == 0


def test_expansion_boundary_does_not_validate_next_destination(graph, monkeypatch):
    monkeypatch.setattr(graph._queries, "_max_traversal_expansions", 2)
    sizes = []
    reasons = []
    record = qe._record_fallback

    def fallback(context, reason):
        reasons.append(reason)
        record(context, reason)

    monkeypatch.setattr(qe, "_record_fallback", fallback)
    target_index = _target_index(graph)
    candidates = IndexStore._candidates_unchecked

    def corrupt_later(store, key):
        if store.file == target_index.file and key == record_id_key(2):
            raise GrafxCorruptionDetected("later destination must not win over quota")
        yield from candidates(store, key)

    monkeypatch.setattr(IndexStore, "_candidates_unchecked", corrupt_later)
    original = qe._OwnerLandingView.landings_many

    def observe(view, identities, context, index):
        sizes.append(len(identities))
        assert len(identities) <= 2
        return original(view, identities, context, index)

    monkeypatch.setattr(qe._OwnerLandingView, "landings_many", observe)
    with pytest.raises(GrafxError) as raised:
        graph.execute(PREFIX + "RETURN b.id ORDER BY b.id")
    assert raised.value.details["field"] == "max_traversal_expansions"
    assert raised.value.details["observed"] == 3
    assert sizes == [2]
    assert reasons == ["quota_frontier"]


@pytest.mark.parametrize("count", [63, 64, 65, 128])
def test_wide_quota_groups_are_bounded_with_no_double_charge(wide_graph, monkeypatch, count):
    original = qe._edge_steps

    def bounded(*args, **kwargs):
        steps = original(*args, **kwargs)
        return lambda identity: islice(steps(identity), count)

    monkeypatch.setattr(qe, "_edge_steps", bounded)
    for option in ("_max_intermediate_rows", "_max_traversal_paths", "_max_traversal_expansions"):
        monkeypatch.setattr(wide_graph._queries, option, count)
    calls = _observe(monkeypatch)
    candidate = wide_graph.execute(WIDE_QUERY)
    # A final one-candidate frontier deliberately uses the scalar port.
    assert calls == ([64] * (count // 64) + ([count % 64] if count % 64 > 1 else []))
    assert len(candidate.rows) == count
    monkeypatch.setattr(qe, "_admits_batched_landings", lambda *_: False)
    scalar = wide_graph.execute(WIDE_QUERY)
    assert candidate.rows == scalar.rows
    assert comparable_statistics(candidate.statistics) == comparable_statistics(scalar.statistics)


def test_invisible_candidates_do_not_spend_path_or_intermediate_quota(graph, monkeypatch):
    fixture._hide(monkeypatch, graph, ("b0", "b1", "b2"))
    for option in ("_max_intermediate_rows", "_max_traversal_paths"):
        monkeypatch.setattr(graph._queries, option, 3)
    monkeypatch.setattr(graph._queries, "_max_traversal_expansions", 6)
    calls = _observe(monkeypatch)
    candidate = graph.execute(PREFIX + "RETURN b.id ORDER BY b.id")
    assert candidate.rows == () and calls
    monkeypatch.setattr(qe, "_admits_batched_landings", lambda *_: False)
    scalar = graph.execute(PREFIX + "RETURN b.id ORDER BY b.id")
    assert candidate.rows == scalar.rows
    assert comparable_statistics(candidate.statistics) == comparable_statistics(scalar.statistics)


def test_nested_pattern_cannot_borrow_outer_statement_quota_window(graph, monkeypatch):
    monkeypatch.setattr(graph._queries, "_max_traversal_expansions", 1000)
    calls = _observe(monkeypatch)
    query = PREFIX + "RETURN b.id, [(a)-[:E]->(c:B) | c.id] AS ids ORDER BY b.id"
    candidate = graph.execute(query)
    assert calls == []
    monkeypatch.setattr(qe, "_admits_batched_landings", lambda *_: False)
    assert candidate.rows == graph.execute(query).rows
