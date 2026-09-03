"""Short synthetic tests for the pure P0.3 heap-census aggregation."""

from __future__ import annotations

import json

import pytest

from okto_grafx.domain.ids import PROVISIONAL_CSN
from okto_grafx.domain.model.record import RecordHeader
from tools.perf_round.heap_census import (
    HeapCensusRefused,
    summarize_heap_census,
    summarize_mvcc_headers,
    summarize_tail_distances,
)


def test_tail_distance_uses_chain_order_not_numeric_page_arithmetic() -> None:
    summary = summarize_tail_distances(
        [((90, 3, 700, 11), {90: 1, 3: 2, 11: 3})]
    ).as_dict()

    assert summary == {
        "total": 6,
        "min": 0,
        "p50": 0,
        "p90": 3,
        "p99": 3,
        "max": 3,
        "last_10_percent": 3,
        "last_10_percent_ratio": 0.5,
    }


def test_tail_distance_refuses_duplicate_and_unknown_pages() -> None:
    with pytest.raises(HeapCensusRefused, match="repeats"):
        summarize_tail_distances([((4, 9, 4), {4: 1})])
    with pytest.raises(HeapCensusRefused, match="absent"):
        summarize_tail_distances([((4, 9), {8: 1})])


def test_mvcc_summary_matches_the_engine_live_predicate() -> None:
    headers = (
        RecordHeader(record_id=101, xmin=2, xmax=0),
        RecordHeader(record_id=102, xmin=3, xmax=PROVISIONAL_CSN),
        RecordHeader(record_id=103, xmin=4, xmax=8),
        RecordHeader(record_id=104, xmin=0, xmax=0),
        RecordHeader(record_id=105, xmin=PROVISIONAL_CSN, xmax=0),
    )

    assert summarize_mvcc_headers(headers).as_dict() == {
        "versions_total": 5,
        "live_committed_open": 2,
        "dead_total": 3,
        "dead_committed_ended": 1,
        "dead_no_csn_birth": 1,
        "dead_provisional_birth": 1,
    }


def test_heap_census_is_global_aggregate_and_emits_no_locations_or_stamps() -> None:
    secret_record_id = 7_777_777_777
    report = summarize_heap_census(
        [
            (
                (9001, 2, 4000),
                (
                    (9001, RecordHeader(record_id=secret_record_id, xmin=12)),
                    (2, RecordHeader(record_id=secret_record_id, xmin=12, xmax=20)),
                ),
            ),
            (
                (71,),
                ((71, RecordHeader(record_id=44, xmin=PROVISIONAL_CSN)),),
            ),
        ]
    )

    assert report["mvcc_headers"] == {
        "versions_total": 3,
        "live_committed_open": 1,
        "dead_total": 2,
        "dead_committed_ended": 1,
        "dead_no_csn_birth": 0,
        "dead_provisional_birth": 1,
    }
    assert report["live_version_tail_distance_pages"] == {
        "total": 1,
        "min": 2,
        "p50": 2,
        "p90": 2,
        "p99": 2,
        "max": 2,
        "last_10_percent": 0,
        "last_10_percent_ratio": 0.0,
    }
    encoded = json.dumps(report, sort_keys=True)
    assert str(secret_record_id) not in encoded
    assert "9001" not in encoded
    assert "record_id" not in encoded
    assert "page_id" not in encoded
    assert "xmin" not in encoded
    assert "xmax" not in encoded


def test_invalid_header_stamp_is_refused_instead_of_guessed() -> None:
    with pytest.raises(HeapCensusRefused, match="xmin"):
        summarize_mvcc_headers((RecordHeader(record_id=1, xmin=-1),))


def test_dead_header_outside_chain_is_also_refused() -> None:
    with pytest.raises(HeapCensusRefused, match="absent"):
        summarize_heap_census(
            [
                (
                    (10,),
                    ((99, RecordHeader(record_id=1, xmin=2, xmax=3)),),
                )
            ]
        )


def test_empty_population_has_explicit_null_distribution() -> None:
    assert summarize_heap_census([]) == {
        "mvcc_headers": {
            "versions_total": 0,
            "live_committed_open": 0,
            "dead_total": 0,
            "dead_committed_ended": 0,
            "dead_no_csn_birth": 0,
            "dead_provisional_birth": 0,
        },
        "live_version_tail_distance_pages": {
            "total": 0,
            "min": None,
            "p50": None,
            "p90": None,
            "p99": None,
            "max": None,
            "last_10_percent": 0,
            "last_10_percent_ratio": None,
        },
    }
