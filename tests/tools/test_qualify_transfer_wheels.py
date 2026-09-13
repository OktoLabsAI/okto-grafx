"""Compatibility evidence must not mistake unrelated failure for format refusal."""

import pytest

from tools.qualify_transfer_wheels import snapshot, validate_refusal


def valid():
    return {"refusal": {"code": "recovery_refused", "details": {
        "operation": "logical_transfer", "reason": "unsupported_format"}},
        "changed_artifact": False, "changed_destination_parent": False}


@pytest.mark.parametrize("shape", ["plain", "node_labels", "labels_bundle"])
def test_exact_refusal_is_accepted(shape):
    validate_refusal(valid(), shape=shape)


@pytest.mark.parametrize("key,value", [("code", "lease_timeout"), ("reason", "artifact_invalid"),
                                      ("reason", "target_exists"), ("operation", "recover")])
def test_unrelated_failure_does_not_prove_compatibility(key, value):
    result = valid()
    target = result["refusal"] if key == "code" else result["refusal"]["details"]
    target[key] = value
    with pytest.raises(AssertionError):
        validate_refusal(result)


@pytest.mark.parametrize("field", ["changed_artifact", "changed_destination_parent"])
def test_no_file_or_empty_workspace_change_is_waived(field):
    result = valid()
    result[field] = True
    with pytest.raises(AssertionError):
        validate_refusal(result)


def test_snapshot_includes_empty_directories_and_control_changes(tmp_path):
    before = snapshot(tmp_path)
    (tmp_path / "control").mkdir()
    after_control = snapshot(tmp_path)
    assert after_control != before and after_control["control"] is None
    (tmp_path / ".target.incomplete").mkdir()
    assert snapshot(tmp_path) != after_control


@pytest.mark.parametrize("shape", ["decimal", "typed_collections", "type_bundle"])
@pytest.mark.parametrize("mutation", [None, "other_type", "cause", "reason", "artifact", "workspace"])
def test_typed_refusal_requires_exact_preceding_reader_limitation(shape, mutation):
    result = valid()
    result["refusal"]["details"]["reason"] = "artifact_invalid"
    cause = ({"type": "TypeError", "message": "ColumnDef.__init__() got an unexpected keyword argument 'stored_type'"}
             if shape == "typed_collections" else {"type": "KeyError", "message": "'DECIMAL'"})
    result["refusal"]["cause"] = cause
    if mutation == "other_type":
        cause["type"] = "ValueError"
    elif mutation == "cause":
        cause["message"] = "Bad checksum"
    elif mutation == "reason":
        result["refusal"]["details"]["reason"] = "unsupported_format"
    elif mutation == "artifact":
        result["changed_artifact"] = True
    elif mutation == "workspace":
        result["changed_destination_parent"] = True
    if mutation is None:
        validate_refusal(result, shape=shape)
    else:
        with pytest.raises(AssertionError):
            validate_refusal(result, shape=shape)


@pytest.mark.parametrize("shape", ["decimal", "typed_collections", "type_bundle"])
@pytest.mark.parametrize("mutation", [None, "without_resume", "wrong_cause", "typed_error", "effects"])
def test_archived_raw_exception_is_only_a_precise_nonmutating_resume_boundary(shape, mutation):
    cause = ({"type": "TypeError", "message": "ColumnDef.__init__() got an unexpected keyword argument 'stored_type'"}
             if shape == "typed_collections" else {"type": "KeyError", "message": "'DECIMAL'"})
    result = {"resume": True, "refusal": {"code": None, "details": {}, "cause": cause},
              "changed_artifact": False, "changed_destination_parent": False}
    if mutation == "without_resume":
        result["resume"] = False
    elif mutation == "wrong_cause":
        cause["message"] = "Some other type error"
    elif mutation == "typed_error":
        result["refusal"]["code"] = "lease_timeout"
    elif mutation == "effects":
        result["changed_destination_parent"] = True
    if mutation is None:
        validate_refusal(result, shape=shape)
    else:
        with pytest.raises(AssertionError):
            validate_refusal(result, shape=shape)


def test_combined_label_type_fixture_verification_does_not_depend_on_base_label():
    from okto_grafx import connect
    from tools.qualify_transfer_wheels import type_fixture, verify_types

    columns, values = type_fixture("labels_bundle")
    with connect(":memory:") as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute(f"CREATE NODE TABLE N(id INT64,body STRING,{columns},PRIMARY KEY(id))")
            props = ",".join(f"{name}:${name}" for name in values)
            tx.execute(f"CREATE(a:N {{id:1,body:'one',{props}}}), (b:N {{id:2,body:'two',{props}}}) "
                       "SET a:LabelA:`λ` REMOVE a:N, b:N", values)
        assert verify_types(db, "labels_bundle")["descriptors"][-1].startswith("LIST<STRUCT<")
