"""Checkpoint-C verifier uses exact feature masks, byte evidence and independent fixtures."""

import hashlib
from zipfile import ZipFile

import pytest

from okto_grafx import connect
from okto_grafx.domain.model.catalog import _CAPABILITY_TO_BIT
from tools.qualify_temporal_wheels import (
    capability_bit, typed_fixture, validate_refusal, package_snapshot, wheel_snapshot, prove_package,
)


@pytest.mark.parametrize("capability", ("decimal", "typed_collections", "type_bundle", "node_labels"))
@pytest.mark.parametrize("known", (31, (1 << 19)-1, (1 << 24)-1))
@pytest.mark.parametrize("state,mode", (("materialized","read"), ("materialized","write"),
    ("pending","read"), ("pending","write"), ("live","read"), ("live","write")))
def test_exact_unknown_mask_required_for_every_selected_reader(capability, known, state, mode):
    unknown = capability_bit(capability) & ~known
    valid = {"code":"schema_version_mismatch", "details":{"unsupported":unknown}}
    if (state,mode) == ("pending","read"):
        valid = {"code":"unsupported_operation", "details":{"field":"read_only_consistency"}}
    validate_refusal({"refusal":valid},state,mode,capability=capability,known_bits=known)
    for bad in ({"code":"lease_timeout","details":{}},
                {"code":"schema_version_mismatch","details":{"unsupported":unknown | (1 << 30)}},
                {"code":"schema_version_mismatch","details":{"unsupported":0}},
                {"code":"unsupported_operation","details":{"field":"unrelated"}}):
        with pytest.raises(AssertionError):
            validate_refusal({"refusal":bad},state,mode,capability=capability,known_bits=known)


@pytest.mark.parametrize("capability", ("decimal","typed_collections","type_bundle"))
def test_current_reader_cannot_masquerade_as_old(capability):
    with pytest.raises(AssertionError):
        validate_refusal({"refusal":{"code":"unsupported_operation","details":{"field":"read_only_consistency"}}},
                         "pending","read",capability=capability,known_bits=(1 << 28)-1)


@pytest.mark.parametrize("capability", ("decimal","typed_collections","type_bundle"))
def test_fixture_requires_exact_declared_capabilities_and_values(capability):
    fixture = typed_fixture(capability)
    with connect(":memory:", codec="pure") as db:
        db.ensure_identity_indexes()
        prior = set(db._catalog.catalog.required_capabilities())
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE Typed(id INT64," + ",".join(f"{n} {t}" for n,t,_ in fixture) + ",PRIMARY KEY(id))")
            tx.execute("CREATE(:Typed {id:1," + ",".join(f"{n}:${n}" for n,_,_ in fixture) + "})",
                       {n:v for n,_,v in fixture})
            tx.execute("CREATE(:Typed {id:2})")
        actual = db.execute("MATCH(n:Typed) RETURN n.id," + ",".join(f"n.{n}" for n,_,_ in fixture) + " ORDER BY n.id").rows
        assert actual == ((1, *(v for _,_,v in fixture)), (2, *(None for _ in fixture)))
        added = set(db._catalog.catalog.required_capabilities()) - prior
        assert sum(_CAPABILITY_TO_BIT[name] for name in added) == capability_bit(capability)
        assert not db.verify("all").findings


def test_package_proof_matches_installed_wheel_and_source_without_masking_changes(tmp_path):
    root = tmp_path / "source"
    package = root / "src" / "okto_grafx"
    package.mkdir(parents=True)
    (package / "__init__.py").write_bytes(b"first")
    (package / "py.typed").write_bytes(b"")
    (package / "__pycache__").mkdir()
    (package / "__pycache__" / "ignored.pyc").write_bytes(b"compiled")
    wheel = tmp_path / "candidate.whl"
    with ZipFile(wheel,"w") as archive:
        archive.writestr("okto_grafx/__init__.py", b"first")
        archive.writestr("okto_grafx/py.typed", b"")
        archive.writestr("okto_grafx-0.0.6.dist-info/METADATA", b"not a package module")
    files = package_snapshot(package)
    assert files == wheel_snapshot(wheel)
    proof = prove_package({"files":files},wheel,root)
    assert proof["package_files"] == 2 and proof["source_checked"]
    assert proof["sha256"] == hashlib.sha256(wheel.read_bytes()).hexdigest()
    with pytest.raises(AssertionError):
        prove_package({"files":{}},wheel,root)
    (package / "__init__.py").write_bytes(b"changed")
    with pytest.raises(AssertionError):
        prove_package({"files":files},wheel,root)
    (package / "__init__.py").write_bytes(b"first")
    (package / "unexpected.py").write_bytes(b"unpackaged")
    with pytest.raises(AssertionError):
        prove_package({"files":files},wheel,root)


def test_label_fixture_adds_only_the_new_required_bit_and_preserves_identity(tmp_path):
    from tools.qualify_temporal_wheels import write_label_fixture, label_fixture_rows

    with connect(tmp_path / "db") as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64,PRIMARY KEY(id))")
            tx.execute("CREATE(:N {id:1})")
        db.ensure_identity_indexes()
        with db.begin("read") as tx:
            original = tx.scan_rows_v1("N", limit=1).rows[0].record_id
        prior = set(db._catalog.catalog.required_capabilities())
        with db.begin() as tx:
            write_label_fixture(tx)
        assert db.execute("MATCH(n) RETURN n.id,labels(n) ORDER BY n.id").rows == label_fixture_rows()
        added = set(db._catalog.catalog.required_capabilities()) - prior
        assert sum(_CAPABILITY_TO_BIT[name] for name in added) == capability_bit("node_labels") == 1 << 28
        with db.begin("read") as tx:
            assert tx.scan_rows_v1("N", limit=1).rows[0].record_id == original
        assert not db.verify("all").findings
        with pytest.raises(AssertionError):
            validate_refusal({"refusal":{"code":"unsupported_operation","details":{"field":"read_only_consistency"}}},
                             "pending", "read", capability="node_labels", known_bits=(1 << 29)-1)
