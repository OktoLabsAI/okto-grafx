"""Independent identity/provenance contracts and native incarnation evidence."""

from dataclasses import FrozenInstanceError, replace
import json

import pytest

from okto_grafx import connect
from okto_grafx.domain.ids import PROVISIONAL_CSN
from okto_grafx.domain.query.entity_identity import EntityIdentity, EntityProvenance
from okto_grafx.errors import GrafxConfigurationError


def test_identity_qualifies_database_table_kind_and_record_incarnation():
    base = EntityIdentity(b"a" * 16, 1, "node", record_id=1)
    identities = {base, replace(base, database_uuid=b"b" * 16), replace(base, table_id=2),
                  replace(base, kind="relationship"), replace(base, record_id=2),
                  replace(base, record_id=None, provisional_id=b"p" * 16)}
    assert len(identities) == 6
    assert base == EntityIdentity(b"a" * 16, 1, "node", record_id=1)
    assert hash(base) == hash(EntityIdentity(b"a" * 16, 1, "node", record_id=1))
    assert base.committed
    assert not replace(base, record_id=None, provisional_id=b"p" * 16).committed
    with pytest.raises(FrozenInstanceError):
        base.record_id = 2


@pytest.mark.parametrize("changes", [
    {"database_uuid": b"short"}, {"database_uuid": bytearray(b"a" * 16)},
    {"table_id": True}, {"table_id": 0}, {"table_id": 1 << 32},
    {"kind": "rel"}, {"kind": []}, {"record_id": None},
    {"record_id": True}, {"record_id": -1}, {"record_id": 0}, {"record_id": (1 << 64) - 1},
    {"provisional_id": b"p" * 16}, {"record_id": None, "provisional_id": b"short"},
])
def test_identity_rejects_ambiguous_or_host_owned_fields(changes):
    with pytest.raises(GrafxConfigurationError):
        EntityIdentity(**({"database_uuid": b"a" * 16, "table_id": 1, "kind": "node", "record_id": 1} | changes))


def test_serialization_keeps_wide_ids_exact_and_returns_owned_dicts():
    entity = EntityIdentity(b"a" * 16, (1 << 32) - 1, "relationship", record_id=(1 << 64) - 2)
    encoded = json.loads(json.dumps(entity.to_dict()))
    assert encoded == {
        "format": "grafx.entity-id.v1", "database_uuid": "61" * 16,
        "table_id": "4294967295", "kind": "relationship",
        "record_id": "18446744073709551614", "provisional_id": None,
    }
    encoded["record_id"] = "1"
    assert entity.record_id == (1 << 64) - 2


@pytest.mark.parametrize("changes", [
    {"read_lsn": True}, {"read_lsn": -1}, {"read_lsn": PROVISIONAL_CSN},
    {"schema_version": 0}, {"schema_version": True}, {"schema_version": 1 << 32},
    {"version_lsn": None}, {"version_lsn": 11}, {"version_lsn": PROVISIONAL_CSN},
    {"pending": 1},
])
def test_provenance_cannot_claim_a_future_committed_version(changes):
    with pytest.raises(GrafxConfigurationError):
        EntityProvenance(**({"read_lsn": 10, "schema_version": 1, "version_lsn": 5} | changes))


def test_pending_provenance_does_not_invent_a_commit_number():
    assert EntityProvenance(10, 1, None, pending=True).to_dict() == {
        "read_lsn": "10", "schema_version": 1, "version_lsn": None, "pending": True,
    }
    assert EntityProvenance(10, 2, 5, pending=True).to_dict()["version_lsn"] == "5"


def test_two_native_databases_with_the_same_local_record_id_have_distinct_identities():
    observed = []
    for _ in range(2):
        with connect(":memory:") as db:
            with db.begin("write") as tx:
                tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
                tx.execute("CREATE (:N {id:1})")
            record = db.execute("MATCH (n:N) RETURN n").rows[0][0]
            observed.append(record.identity)
    assert observed[0].record_id == observed[1].record_id
    assert observed[0] != observed[1]


def test_identity_dto_cannot_be_smuggled_into_query_parameters_as_write_authority():
    with connect(":memory:") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, value INT64, PRIMARY KEY(id))")
            tx.execute("CREATE (:N {id:1,value:0})")
        entity = EntityIdentity(db.identity.database_uuid, 1, "node", record_id=1)
        with db.begin("write") as tx:
            with pytest.raises(GrafxConfigurationError):
                tx.execute("MATCH (n:N) SET n.value=1 RETURN $entity", {"entity": entity})
            assert tx.execute("MATCH (n:N) RETURN n.value").rows == ((0,),)


def test_native_record_identity_survives_updates_but_not_primary_key_recreation(tmp_path):
    path = tmp_path / "db"
    with connect(path) as db:
        uuid = db.identity.database_uuid
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, value INT64, PRIMARY KEY(id))")
            tx.execute("CREATE (:N {id:1,value:0})")
        first = db.execute("MATCH (n:N) RETURN n").rows[0][0]
        with db.begin("read") as old:
            assert old.execute("MATCH (n:N) RETURN n,n.value").rows == ((first, 0),)
            with db.begin("write") as tx:
                tx.execute("MATCH (n:N) SET n.value=1")
            assert db.execute("MATCH (n:N) RETURN n,n.value").rows == ((first, 1),)
            with db.begin("write") as tx:
                tx.execute("MATCH (n:N) DELETE n")
                tx.execute("CREATE (:N {id:1,value:2})")
            second = db.execute("MATCH (n:N) RETURN n").rows[0][0]
            assert second != first
            assert old.execute("MATCH (n:N) RETURN n,n.value").rows == ((first, 0),)
    with connect(path) as db:
        assert db.identity.database_uuid == uuid
        assert db.execute("MATCH (n:N) RETURN n").rows == ((second,),)
        with db.begin("write") as tx:
            tx.execute("MATCH (n:N) DELETE n")
        with db.begin("write") as tx:
            tx.execute("CREATE (:N {id:1,value:3})")
        third = db.execute("MATCH (n:N) RETURN n").rows[0][0]
        assert len({first, second, third}) == 3
        assert len({i.identity for i in (first, second, third)}) == 3
