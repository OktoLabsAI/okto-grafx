"""Flexible graph flags are durable authority, not table-name conventions."""

import struct
from dataclasses import replace

import pytest

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxCorruptionDetected, GrafxSchemaVersionMismatch, GrafxRecoveryRefused
from okto_grafx.domain.model import catalog as catalog_module
from okto_grafx.domain.model.catalog import Catalog
from okto_grafx.domain.model.relationship_type import RelationshipTypeDef
from okto_grafx.domain.model.errors import SchemaMismatchError
from okto_grafx.domain.model.schema import ColumnDef, SchemaType, TableDef, FLEXIBLE_GRAPH_CAPABILITY, encode_tuple, decode_tuple
from okto_grafx.domain.model.value import encode_value
from okto_grafx.domain.page import crc32c
from okto_grafx.domain.recovery.decision import committed_replay
from okto_grafx.engine.commit_redo import CommitRedo

from .test_catalog_replay_images import records, staged
from .test_catalog_v2 import _identity


def table():
    return TableDef(1, "Physical", "node", (ColumnDef("_properties", SchemaType.ANY, nullable=False),),
                    flexible_properties=True, unlabeled=True)


def catalog(definition=None):
    result = Catalog()
    result.upgrade_index_catalog(())
    result.add_table(table() if definition is None else definition)
    return result


def test_catalog_flags_are_independently_encoded_and_preserved():
    original = catalog()
    typed = catalog(replace(table(), flexible_properties=False, unlabeled=False))
    body = bytearray(typed.serialize()[:-4])
    bits = struct.unpack_from("<Q", body, 28)[0]
    struct.pack_into("<Q", body, 28, bits | (1 << 21))
    body += b"\x03"  # One flexible, unlabeled node table; no indexes/spaces/extensions.
    assert original.serialize() == bytes(body) + struct.pack("<I", crc32c(bytes(body)))
    assert Catalog.deserialize(original.serialize()) == original
    assert original.copy().table("Physical") == table()
    assert original.requires_capability(FLEXIBLE_GRAPH_CAPABILITY)


@pytest.mark.parametrize("changes", [
    {"flexible_properties": False}, {"unlabeled": 1}, {"flexible_properties": 1},
    {"columns": (ColumnDef("wrong", SchemaType.ANY, nullable=False),)},
    {"columns": (ColumnDef("_properties", SchemaType.ANY),)},
    {"kind":"rel", "from_table":"N", "to_table":"N"},
])
def test_inconsistent_model_flags_refuse(changes):
    with pytest.raises(GrafxConfigurationError):
        replace(table(), **changes)


@pytest.mark.parametrize("value", [None, 4, "text", [], {"gone":None}])
def test_flexible_payload_is_a_nonnull_property_map_with_no_null_entries(value):
    with pytest.raises(SchemaMismatchError):
        encode_tuple(table(), (value,))
    with pytest.raises(SchemaMismatchError):
        decode_tuple(table(), encode_value(value))


def test_catalog_cannot_admit_two_unlabeled_stores():
    state = catalog()
    before = state.serialize()
    with pytest.raises(GrafxConfigurationError, match="unlabeled"):
        state.add_table(replace(table(), table_id=2, name="Other"))
    assert state.serialize() is before


def test_unknown_reader_refuses_before_catalog_replay_mutates_pages(monkeypatch):
    pool, store, images = staged(catalog(), 1000)
    before, writes = store.read_from_pages(), list(pool.storage.write_calls)
    monkeypatch.setattr(catalog_module, "_KNOWN_CAPABILITY_BITS", catalog_module._KNOWN_CAPABILITY_BITS & ~(1 << 21))
    with pytest.raises(GrafxSchemaVersionMismatch):
        CommitRedo(pool).apply(committed_replay(records(images, 1000)))
    assert store.read_from_pages() == before and pool.storage.write_calls == writes


def test_flexible_catalog_requires_commit_and_replays_idempotently():
    state = catalog()
    pool, store, images = staged(state, 1000)
    before = store.read_from_pages()
    CommitRedo(pool).apply(committed_replay(records(images, 1000)[:-1]))
    assert store.read_from_pages() == before
    replay = committed_replay(records(images, 1000))
    CommitRedo(pool).apply(replay)
    assert store.read_from_pages() == state
    CommitRedo(pool).apply(replay)
    assert store.read_from_pages() == state


def test_valid_commits_cannot_reinterpret_an_existing_table_as_unlabeled():
    first = catalog(replace(table(), flexible_properties=False, unlabeled=False))
    pool, store, before_images = staged(first, 1000)
    _, _, after_images = staged(catalog(), 2000)
    before, writes = store.read_from_pages(), list(pool.storage.write_calls)
    with pytest.raises(GrafxRecoveryRefused, match="property/label model"):
        CommitRedo(pool).apply(committed_replay((*records(before_images, 1000), *records(after_images, 2000))))
    assert store.read_from_pages() == before and pool.storage.write_calls == writes


def test_capability_cannot_be_removed_with_valid_checksum():
    body = bytearray(catalog().serialize()[:-4])
    bits = struct.unpack_from("<Q", body, 28)[0]
    struct.pack_into("<Q", body, 28, bits & ~(1 << 21))
    with pytest.raises(GrafxCorruptionDetected):
        Catalog.deserialize(bytes(body) + struct.pack("<I", crc32c(bytes(body))))


def flexible_group():
    result = catalog()
    result.add_table(replace(table(), table_id=2, name="Other", unlabeled=False))
    for key, source, target in ((3,"Physical","Other"),(4,"Other","Physical")):
        result.add_table(TableDef(key, f"edge{key}", "rel", table().columns,
                                 from_table=source, to_table=target, flexible_properties=True))
    for key, name in ((1,"Physical"),(2,"Other")):
        result.add_index_definition(replace(_identity(nonce=20+key), name=f"rid_t_{key:08x}",
                                            table_id=key, table_name=name))
    result.add_relationship_type(RelationshipTypeDef("R", (3,)))
    return result


def test_flexible_group_extension_roundtrip_and_committed_replay():
    before = flexible_group()
    after = before.copy()
    after.extend_flexible_relationship_type("R", (4,))
    assert before.relationship_types()[0].table_ids == (3,)
    assert Catalog.deserialize(after.serialize()) == after
    pool, store, old_images = staged(before, 1000)
    _, _, new_images = staged(after, 2000)
    replay = committed_replay((*records(old_images,1000), *records(new_images,2000)))
    CommitRedo(pool).apply(replay)
    assert store.read_from_pages() == after
    CommitRedo(pool).apply(replay)
    assert store.read_from_pages() == after


@pytest.mark.parametrize("members", [(), [], None, (3,), (99,), (True,)])
def test_flexible_group_bad_extension_is_nonmutating(members):
    state = flexible_group()
    encoded = state.serialize()
    with pytest.raises(GrafxConfigurationError):
        state.extend_flexible_relationship_type("R", members)
    assert state.serialize() == encoded


def test_flexible_group_committed_removal_refuses_before_page_writes():
    small = flexible_group()
    large = small.copy()
    large.extend_flexible_relationship_type("R", (4,))
    pool, store, old_images = staged(large, 1000)
    _, _, new_images = staged(small, 2000)
    before, writes = store.read_from_pages(), list(pool.storage.write_calls)
    with pytest.raises(GrafxRecoveryRefused):
        CommitRedo(pool).apply(committed_replay((*records(old_images,1000), *records(new_images,2000))))
    assert store.read_from_pages() == before and pool.storage.write_calls == writes


def test_historical_models_require_a_separate_capability_and_old_reader_refuses_before_replay(monkeypatch):
    state = catalog()
    state.enable_commit_catalog(1000)
    state.enable_system_history((1,),1000)
    assert state.requires_capability("system_history_models_v1")
    encoded = state.serialize()
    assert struct.unpack_from("<Q",encoded,28)[0] & (1 << 22)
    assert Catalog.deserialize(encoded) == state
    pool, store, images = staged(state,1000)
    before, writes = store.read_from_pages(), list(pool.storage.write_calls)
    monkeypatch.setattr(catalog_module,"_KNOWN_CAPABILITY_BITS",catalog_module._KNOWN_CAPABILITY_BITS & ~(1 << 22))
    with pytest.raises(GrafxSchemaVersionMismatch):
        CommitRedo(pool).apply(committed_replay(records(images,1000)))
    assert store.read_from_pages() == before and pool.storage.write_calls == writes


def test_missing_history_model_capability_with_valid_checksum_is_not_auto_upgraded():
    state = catalog()
    state.enable_commit_catalog(1000)
    state.enable_system_history((1,),1000)
    body = bytearray(state.serialize()[:-4])
    bits = struct.unpack_from("<Q",body,28)[0]
    struct.pack_into("<Q",body,28,bits & ~(1 << 22))
    with pytest.raises(GrafxSchemaVersionMismatch,match="model metadata"):
        Catalog.deserialize(bytes(body)+struct.pack("<I",crc32c(bytes(body))))
    state._required_capabilities -= {"system_history_models_v1"}
    state._invalidate_derived()
    with pytest.raises(GrafxConfigurationError,match="model metadata"):
        state.serialize()
