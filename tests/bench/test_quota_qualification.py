"""RF06 workload contracts exercise native quota boundaries, without timing."""
from dataclasses import replace

import pytest
import okto_grafx

from bench.harness.campaign_fixtures import Scenario, build_fixture, matrix, oracle, query_for
from bench.harness import quota_qualification as quota
from bench.harness.campaign_worker import error_record, measure_statement
from bench.harness.campaign_schema import CampaignError
from okto_grafx.engine import query_engine as qe
from okto_grafx.errors import GrafxError
from tests.query.work_statistics import comparable_statistics


def case(count=65, field="max_traversal_expansions", limit=None):
    return Scenario("quota-test", "landing", nodes=max(count, 2), edges_per_node=0,
                    fanout=count, quota_field=field, quota_limit=count if limit is None else limit)


def prepare(root, scenario):
    with okto_grafx.connect(root, codec="pure", vector_math="pure") as db:
        build_fixture(db, scenario)


def outcome(db, scenario):
    try:
        result = db.execute(*query_for(scenario))
        return ("success", result.rows, comparable_statistics(result.statistics))
    except GrafxError as error:
        return ("error", error.code, dict(error.details))


def test_catalog_and_configuration_are_explicit():
    cases = quota.matrix(Scenario)
    assert {s.name for s in cases} <= {s.name for s in matrix()}
    assert {s.quota_field for s in cases} == set(quota.FIELDS)
    assert {s.fanout for s in cases} >= {1, 63, 64, 65, 128}
    for scenario in cases:
        assert Scenario.from_dict(scenario.to_dict()) == scenario
        assert quota.connection_options(scenario) == {scenario.quota_field: scenario.quota_limit}
    assert quota.expected_outcome(case(65, limit=64)) == "quota_refusal"
    assert quota.expected_outcome(case(65, limit=65)) == "success"
    assert all(s.work_counter == "certificate_attempts" for s in cases if s.role == "target")
    assert any(s.role == "target" for s in cases)
    assert any(s.query_memory_budget_bytes for s in cases if s.role == "control")


@pytest.mark.parametrize("changes", [{"quota_limit":0}, {"quota_limit":True},
    {"quota_limit":1.5}, {"quota_field":"unknown"}, {"quota_field":None},
    {"family":"node_scan"}, {"reader":"retained"}, {"churn_percent":25}])
def test_bad_quota_scenarios_fail_closed(changes):
    with pytest.raises(ValueError):
        replace(case(), **changes)


@pytest.mark.parametrize("field", quota.FIELDS)
@pytest.mark.parametrize("limit", [1, 63, 64, 65, 66])
def test_native_refusal_and_success_equal_scalar(tmp_path, monkeypatch, field, limit):
    scenario = case(65, field, limit)
    root = tmp_path / "db"
    prepare(root, scenario)
    with okto_grafx.connect(root, **quota.connection_options(scenario)) as db:
        actual = outcome(db, scenario)
        assert actual[0] == ("success" if limit >= 65 else "error")
        if actual[0] == "success":
            assert actual[1] == oracle(scenario)
        else:
            assert actual[2]["field"] == field
            assert actual[2]["observed"] == limit + 1
        monkeypatch.setattr(qe, "_admits_batched_landings", lambda *_: False)
        assert outcome(db, scenario) == actual
        assert db._queries._owner_budget._used_bytes == 0
        assert db.execute("RETURN 1").rows == ((1,),)


@pytest.mark.parametrize("count", [63, 64, 65, 128])
def test_real_batches_and_multiplicity(tmp_path, count):
    scenario = case(count)
    root = tmp_path / "db"
    prepare(root, scenario)
    with okto_grafx.connect(root, **quota.connection_options(scenario)) as db:
        result = db.execute(*query_for(scenario))
        assert result.rows == oracle(scenario)
        assert result.statistics["landing_batches"] == count // 64 + (count % 64 > 1)
        # Candidate work includes the scalar tail; batches count only grouped reads.
        assert result.statistics["landing_candidates"] == count
        assert result.statistics.get("certificate_retries", 0) == 0


def test_worker_refuses_unapplied_quota(tmp_path):
    scenario = case(2)
    root = tmp_path / "db"
    prepare(root, scenario)
    with okto_grafx.connect(root) as db:
        with pytest.raises(CampaignError, match="quota"):
            measure_statement(db, scenario, 0)


def test_duplicate_destinations_preserve_edges_and_visibility(tmp_path, monkeypatch):
    from tests.query.test_batched_landing_without_vectors import _hide

    scenario = replace(case(8), nodes=4)
    root = tmp_path / "db"
    prepare(root, scenario)
    with okto_grafx.connect(root, **quota.connection_options(scenario)) as db:
        result = db.execute(*query_for(scenario))
        assert result.rows == ((0, 0), (0, 0), (0, 1), (0, 1), (0, 2), (0, 2), (0, 3), (0, 3))
        assert result.statistics["landing_batches"] == 1
        assert result.statistics["landing_candidates"] == 4
        _hide(monkeypatch, db, (1, 2), table="N")
        result = db.execute(*query_for(scenario))
        assert result.rows == ((0, 0), (0, 0), (0, 3), (0, 3))
        assert result.statistics["landing_batches"] == 1
        monkeypatch.setattr(qe, "_admits_batched_landings", lambda *_: False)
        assert outcome(db, scenario)[1] == result.rows
        assert db._queries._owner_budget._used_bytes == 0


def test_corruption_after_zero_remaining_does_not_replace_quota(tmp_path, monkeypatch):
    from okto_grafx.domain.index.definition import RECORD_ID_KEY_DERIVATION
    from okto_grafx.domain.index.keys import record_id_key
    from okto_grafx.engine.index_manager import IndexStore
    from okto_grafx.errors import GrafxCorruptionDetected

    scenario = case(4, limit=2)
    root = tmp_path / "db"
    prepare(root, scenario)
    with okto_grafx.connect(root, **quota.connection_options(scenario)) as db:
        target = next(i for i in db._indexes.active_indexes()
                      if i.definition.table_name == "N"
                      and i.definition.key_derivation == RECORD_ID_KEY_DERIVATION)
        original = IndexStore._candidates_unchecked
        forbidden = []

        def corrupt(store, key):
            # IDs are allocated from one: the third destination is id=2, rid=3.
            if store.file == target.file and key == record_id_key(3):
                forbidden.append(key)
                raise GrafxCorruptionDetected("third landing corruption")
            yield from original(store, key)

        monkeypatch.setattr(IndexStore, "_candidates_unchecked", corrupt)
        result = outcome(db, scenario)
        assert result[0] == "error"
        assert result[2]["field"] == "max_traversal_expansions"
        assert result[2]["observed"] == 3
        assert forbidden == []
        monkeypatch.setattr(qe, "_admits_batched_landings", lambda *_: False)
        assert outcome(db, scenario) == result
        assert forbidden == []
        # Positive control: with quota beyond the boundary, the injected read
        # really fails; this avoids certifying a dead corruption injector.
        monkeypatch.setattr(db._queries, "_max_traversal_expansions", 4)
        with pytest.raises(GrafxCorruptionDetected):
            db.execute(*query_for(scenario))
        assert forbidden
        assert db._queries._owner_budget._used_bytes == 0


def test_configured_memory_and_cursor_close_keep_scalar_guard(tmp_path):
    scenario = replace(case(4), query_memory_budget_bytes=1048576)
    root = tmp_path / "db"
    prepare(root, scenario)
    with okto_grafx.connect(root, query_memory_budget_bytes=scenario.query_memory_budget_bytes,
                           **quota.connection_options(scenario)) as db:
        result = db.execute(*query_for(scenario))
        assert result.rows == oracle(scenario)
        assert result.statistics.get("landing_batches", 0) == 0
        assert db._queries._owner_budget._used_bytes == 0
        query, params = query_for(scenario)
        with db.query(query, params).cursor(batch_size=1) as cursor:
            assert next(iter(cursor)) == oracle(scenario)[0]
        assert db._queries._owner_budget._used_bytes == 0
        assert db.execute(query, params).rows == result.rows


def test_native_quota_error_artifact_retains_code_and_details(tmp_path):
    import json

    scenario = case(2, limit=1)
    root = tmp_path / "db"
    prepare(root, scenario)
    with okto_grafx.connect(root, **quota.connection_options(scenario)) as db:
        with pytest.raises(GrafxError) as raised:
            db.execute(*query_for(scenario))
        report = json.loads(json.dumps(error_record(raised.value)))
        assert report["code"] == raised.value.code
        assert report["details"] == dict(raised.value.details)
        assert report["details"]["observed"] == 2
