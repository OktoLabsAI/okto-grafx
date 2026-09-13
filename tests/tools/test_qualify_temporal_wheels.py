"""The wheel verifier must reject unrelated refusals and audit every file."""

import pytest

from tools.qualify_temporal_wheels import snapshot, validate_refusal, temporal_values


@pytest.mark.parametrize("state,mode", [("materialized", "read"), ("materialized", "write"),
                                       ("pending", "write"), ("live", "read"), ("live", "write")])
def test_only_the_temporal_capability_proves_incompatible_layout(state, mode):
    valid = {"refusal": {"code": "schema_version_mismatch", "details": {"unsupported": 1 << 23}}}
    validate_refusal(valid, state, mode)
    for wrong in (
        {"code": "lease_timeout", "details": {}},
        {"code": "schema_version_mismatch", "details": {"unsupported": 1 << 22}},
        {"code": "schema_version_mismatch", "details": {"unsupported": (1 << 22) | (1 << 23)}},
    ):
        with pytest.raises(AssertionError):
            validate_refusal({"refusal": wrong}, state, mode)


def test_pending_read_requires_checkpoint_consistency_refusal():
    validate_refusal({"refusal": {"code": "unsupported_operation", "details": {"field": "read_only_consistency"}}}, "pending", "read")
    with pytest.raises(AssertionError):
        validate_refusal({"refusal": {"code": "unsupported_operation", "details": {"field": "unrelated"}}}, "pending", "read")


@pytest.mark.parametrize("state,mode", [("materialized", "read"), ("materialized", "write"),
                                       ("pending", "write"), ("live", "read"), ("live", "write")])
def test_namespace_admission_requires_exact_namespace_capability(state, mode):
    for mask in (1 << 24, 1 << 23, (1 << 23) | (1 << 24)):
        result = {"refusal":{"code":"schema_version_mismatch","details":{"unsupported":mask}}}
        if mask == 1 << 24:
            validate_refusal(result,state,mode,capability="graph_namespaces")
        else:
            with pytest.raises(AssertionError):
                validate_refusal(result,state,mode,capability="graph_namespaces")


def test_pending_namespace_read_refuses_before_recovery():
    result = {"refusal":{"code":"unsupported_operation","details":{"field":"read_only_consistency"}}}
    validate_refusal(result,"pending","read",capability="graph_namespaces")
    with pytest.raises(AssertionError):
        validate_refusal(result,"materialized","read",capability="graph_namespaces")


@pytest.mark.parametrize("state,mode", [("materialized","read"),("materialized","write"),
                                       ("pending","write"),("live","read"),("live","write")])
def test_vector_owner_fence_requires_exact_new_capability(state, mode):
    for mask in (1 << 25, 1 << 24, (1 << 24) | (1 << 25)):
        result = {"refusal":{"code":"schema_version_mismatch","details":{"unsupported":mask}}}
        if mask == 1 << 25:
            validate_refusal(result,state,mode,capability="vector_owners")
        else:
            with pytest.raises(AssertionError):
                validate_refusal(result,state,mode,capability="vector_owners")


def test_snapshot_includes_control_wal_and_new_or_removed_files(tmp_path):
    for name in ("heap.dat", "catalog.dat", "control/commit.state", "wal/log.0"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"original")
    first = snapshot(tmp_path)
    assert set(first) == {"heap.dat", "catalog.dat", "control/commit.state", "wal/log.0"}
    assert snapshot(tmp_path) == first
    (tmp_path / "control/commit.state").write_bytes(b"changed")
    (tmp_path / "wal/log.0").unlink()
    (tmp_path / "unexpected.dat").write_bytes(b"new")
    second = snapshot(tmp_path)
    changed = {name for name in first.keys() | second.keys() if first.get(name) != second.get(name)}
    assert changed == {"control/commit.state", "wal/log.0", "unexpected.dat"}


@pytest.mark.parametrize("run", ["run-4", "run-5"])
def test_actual_wheel_receipt_retains_all_seeded_values(run):
    """Audit the retained integration receipt when present; normal unit runs need no artifacts."""
    from pathlib import Path
    import json
    from okto_grafx.domain.model.temporal_interchange import temporal_json_value
    path = Path(".grafx-tmp/fp5-wheel-qualification") / run / "report.json"
    if not path.exists():
        pytest.skip("Run the isolated installed-wheel matrix before its receipt audit")
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["passed"] and len(report["cases"]) == 12
    expected = [temporal_json_value(value) for value in temporal_values()]
    for case in report["cases"]:
        validate_refusal(case["refusal"], case["state"], case["mode"])
        assert case["before"] == case["after"] and case["changed_files"] == []
        assert case["recovered"]["values"] == expected
