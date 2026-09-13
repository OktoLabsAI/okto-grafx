"""Native logical relationship authority: identity, exact bytes and fail-closed load."""

from __future__ import annotations

import struct
from dataclasses import replace

import pytest

from okto_grafx.domain.errors import (
    GrafxConfigurationError, GrafxCorruptionDetected, GrafxSchemaVersionMismatch, GrafxRecoveryRefused,
)
from okto_grafx.domain.model import catalog as catalog_module
from okto_grafx.domain.model.catalog import Catalog
from okto_grafx.domain.model.relationship_type import (
    RELATIONSHIP_TYPES_CAPABILITY, RelationshipTypeDef,
)
from okto_grafx.domain.model.schema import ColumnDef, TableDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.page import crc32c
from okto_grafx.domain.recovery.decision import committed_replay
from okto_grafx.engine.catalog_store import read_catalog_page_images
from okto_grafx.engine.commit_redo import CommitRedo

from .test_catalog_v2 import _identity
from .test_catalog_replay_images import records, staged


def schema() -> Catalog:
    """Two endpoint pairs with independent physical identity, sharing logical properties."""
    result = Catalog()
    for key, name in enumerate(("A", "B", "C"), 1):
        result.add_table(TableDef(key, name, "node", (ColumnDef("id", ValueType.INT64),)))
    for key, name, source, target in ((4, "AB", "A", "B"), (5, "BC", "B", "C")):
        result.add_table(TableDef(key, name, "rel", (ColumnDef("num", ValueType.INT64),),
                                  from_table=source, to_table=target))
    result.upgrade_index_catalog(tuple(replace(
        _identity(nonce=key + 20), name=f"rid_t_{key:08x}", table_id=key, table_name=name,
    ) for key, name in enumerate(("A", "B", "C"), 1)))
    return result


def with_group() -> Catalog:
    result = schema()
    result.add_relationship_type(RelationshipTypeDef("REL", (4, 5)))
    return result


def checksum(body: bytes) -> bytes:
    return body + struct.pack("<I", crc32c(body))


def extension() -> bytes:
    # Independent encoding oracle: count, length-prefixed name, count, physical ids.
    return struct.pack("<IH", 1, 3) + b"REL" + struct.pack("<III", 2, 4, 5)


def test_group_bytes_and_identity_are_separate_from_physical_tables() -> None:
    original = schema()
    prior = original.serialize()
    grouped = original.copy()
    definition = RelationshipTypeDef("REL", (4, 5))
    assert grouped.add_relationship_type(definition) is definition
    assert grouped.tables() == original.tables()
    assert grouped.relationship_tables("REL") == (original.table("AB"), original.table("BC"))
    assert grouped.relationship_tables("AB") == ()  # Not a second logical type for REL edges.
    assert original.relationship_tables("AB") == (original.table("AB"),)
    assert grouped.relationship_tables("A") == grouped.relationship_tables("Missing") == ()
    assert grouped.relationship_type_name(4) == grouped.relationship_type_name(5) == "REL"
    assert original.relationship_type_name(4) == "AB"
    assert not grouped.has_table("REL")  # Physical identity lookup is never aliased.
    with pytest.raises(GrafxConfigurationError):
        grouped.relationship_type_name(1)
    expected = bytearray(prior[:-4])
    bits = struct.unpack_from("<Q", expected, 28)[0]
    struct.pack_into("<Q", expected, 28, bits | (1 << 19))
    assert grouped.serialize() == checksum(bytes(expected) + extension())
    restored = Catalog.deserialize(grouped.serialize())
    assert restored == grouped
    assert restored.serialize() == grouped.serialize()
    assert restored.relationship_types() == (definition,)
    assert restored.relationship_type_name(5) == "REL"
    assert original.serialize() is prior


@pytest.mark.parametrize("members", [(), [], (True,), (0,), (2**32,), (5, 4), (4, 4), (1.0,)])
def test_definition_refuses_mutable_or_ambiguous_member_identity(members: object) -> None:
    with pytest.raises(GrafxConfigurationError):
        RelationshipTypeDef("REL", members)  # type: ignore[arg-type]


@pytest.mark.parametrize("name", ["", "has space", "é", "a" * 129, None, 123])
def test_definition_refuses_invalid_names(name: object) -> None:
    with pytest.raises(GrafxConfigurationError):
        RelationshipTypeDef(name, (4, 5))  # type: ignore[arg-type]


@pytest.mark.parametrize("definition", [
    RelationshipTypeDef("REL", (1,)), RelationshipTypeDef("REL", (4, 99)),
    RelationshipTypeDef("AB", (4, 5)),
])
def test_invalid_group_never_partially_mutates_catalog(definition: RelationshipTypeDef) -> None:
    catalog = schema()
    before = catalog.serialize()
    with pytest.raises(GrafxConfigurationError):
        catalog.add_relationship_type(definition)
    assert catalog.serialize() is before
    assert catalog.relationship_types() == ()
    assert not catalog.requires_capability(RELATIONSHIP_TYPES_CAPABILITY)


@pytest.mark.parametrize("change", ["pair", "columns", "endpoint", "kind"])
def test_complete_schema_validation_precedes_installation(change: str) -> None:
    catalog = schema()
    other = catalog.table("BC")
    if change == "pair":
        other = replace(other, from_table="A", to_table="B")
    elif change == "columns":
        other = replace(other, columns=(*other.columns, ColumnDef("extra", ValueType.STRING)))
    elif change == "endpoint":
        other = replace(other, to_table="Missing")
    else:
        other = replace(other, to_table="AB")
    # Fault fixture below the public installation door, not a supported schema mutation.
    catalog._install_table(other)
    before = catalog.copy()
    with pytest.raises(GrafxConfigurationError):
        catalog.add_relationship_type(RelationshipTypeDef("REL", (4, 5)))
    assert catalog == before


def test_group_requires_explicit_v2_and_does_not_redefine_existing_type() -> None:
    legacy = Catalog()
    with pytest.raises(GrafxConfigurationError):
        legacy.add_relationship_type(RelationshipTypeDef("REL", (1,)))
    assert legacy.format_version == 1 and legacy.relationship_types() == ()
    catalog = with_group()
    before = catalog.serialize()
    for group in (RelationshipTypeDef("REL", (4, 5)), RelationshipTypeDef("Other", (4,))):
        with pytest.raises(GrafxConfigurationError):
            catalog.add_relationship_type(group)
        assert catalog.serialize() is before
    with pytest.raises(GrafxConfigurationError):
        catalog.add_table(TableDef(6, "REL", "node", ()))
    with pytest.raises(GrafxConfigurationError):
        catalog.add_nullable_column("AB", ColumnDef("extra", ValueType.STRING))
    assert catalog.serialize() is before


def test_copy_has_private_group_maps_and_no_hidden_capability_change() -> None:
    original = with_group()
    before = original.serialize()
    clone = original.copy()
    assert clone.serialize() is before
    clone.add_table(TableDef(6, "CA", "rel", (), from_table="C", to_table="A"))
    clone.add_relationship_type(RelationshipTypeDef("BACK", (6,)))
    assert tuple(group.name for group in clone.relationship_types()) == ("BACK", "REL")
    assert tuple(group.name for group in original.relationship_types()) == ("REL",)
    assert original.serialize() is before
    assert Catalog.deserialize(clone.serialize()) == clone


@pytest.mark.parametrize("names", [("REL", "REL"), ("ZZZ", "AAA")])
def test_duplicate_or_out_of_order_groups_refuse_with_valid_checksum(names: tuple[str, str]) -> None:
    raw = with_group().serialize()
    prefix = raw[:-4-len(extension())]
    tail = struct.pack("<I", 2)
    for name, key in zip(names, (4, 5)):
        tail += struct.pack("<H", len(name)) + name.encode("ascii") + struct.pack("<II", 1, key)
    with pytest.raises(GrafxCorruptionDetected):
        Catalog.deserialize(checksum(prefix + tail))


def test_one_physical_member_cannot_acquire_two_logical_names_on_load() -> None:
    raw = with_group().serialize()
    prefix = raw[:-4-len(extension())]
    tail = struct.pack("<I", 2)
    for name in ("AAA", "ZZZ"):
        tail += struct.pack("<H", 3) + name.encode("ascii") + struct.pack("<II", 1, 4)
    with pytest.raises(GrafxCorruptionDetected):
        Catalog.deserialize(checksum(prefix + tail))


@pytest.mark.parametrize("tail", [
    struct.pack("<I", 0), struct.pack("<I", 0xFFFFFFFF),
    struct.pack("<IH", 1, 3) + b"REL" + struct.pack("<I", 0xFFFFFFFF),
    struct.pack("<IH", 1, 3) + b"REL" + struct.pack("<III", 2, 4, 4),
    struct.pack("<IH", 1, 3) + b"REL" + struct.pack("<III", 2, 5, 4),
    struct.pack("<IH", 1, 3) + b"REL" + struct.pack("<III", 2, 4, 99),
    struct.pack("<IH", 1, 3) + b"REL" + struct.pack("<III", 2, 1, 4),
    struct.pack("<IH", 1, 2) + b"AB" + struct.pack("<III", 2, 4, 5),
])
def test_valid_checksum_does_not_authorize_invalid_group_metadata(tail: bytes) -> None:
    raw = with_group().serialize()
    with pytest.raises(GrafxCorruptionDetected):
        Catalog.deserialize(checksum(raw[:-4-len(extension())] + tail))


def test_every_truncated_extension_refuses_even_with_recomputed_checksum() -> None:
    raw = with_group().serialize()
    prefix = raw[:-4-len(extension())]
    for length in range(len(extension())):
        with pytest.raises(GrafxCorruptionDetected):
            Catalog.deserialize(checksum(prefix + extension()[:length]))


def test_unknown_reader_capability_refuses_before_decoding_group(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = with_group().serialize()
    monkeypatch.setattr(catalog_module, "_KNOWN_CAPABILITY_BITS", catalog_module._KNOWN_CAPABILITY_BITS & ~(1 << 19))
    with pytest.raises(GrafxSchemaVersionMismatch):
        Catalog.deserialize(raw)


def test_removing_capability_cannot_turn_group_bytes_into_ordinary_catalog() -> None:
    body = bytearray(with_group().serialize()[:-4])
    bits = struct.unpack_from("<Q", body, 28)[0]
    struct.pack_into("<Q", body, 28, bits & ~(1 << 19))
    with pytest.raises(GrafxCorruptionDetected):
        Catalog.deserialize(checksum(bytes(body)))


def test_complete_staged_page_images_preserve_groups_without_read_or_write() -> None:
    catalog = with_group()
    pool, store, images = staged(catalog, 1000)
    device = pool.storage
    before = store.persisted_image()
    writes = list(device.write_calls)
    device.read_calls.clear()
    result = read_catalog_page_images(images, page_size=512, sequence=1000)
    assert result == catalog
    assert result.relationship_type_name(4) == result.relationship_type_name(5) == "REL"
    assert device.read_calls == [] and device.write_calls == writes
    assert store.persisted_image() == before


def test_native_committed_replay_preserves_groups_and_is_repeatable() -> None:
    catalog = with_group()
    pool, store, images = staged(catalog, 1000)
    replay = committed_replay(records(images, 1000))
    CommitRedo(pool).apply(replay)
    assert store.read_from_pages() == catalog
    CommitRedo(pool).apply(replay)
    assert store.read_from_pages().relationship_type_name(5) == "REL"


def test_group_catalog_without_commit_has_no_recovery_effects() -> None:
    pool, store, images = staged(with_group(), 1000)
    before = store.read_from_pages()
    writes = list(pool.storage.write_calls)
    CommitRedo(pool).apply(committed_replay(records(images, 1000)[:-1]))
    assert store.read_from_pages() == before
    assert pool.storage.write_calls == writes


def test_invalid_group_after_image_is_refused_before_first_page_apply() -> None:
    corrupt = with_group()
    # A valid checksum/COMMIT cannot authorize a nonexistent physical member.
    corrupt._relationship_types["REL"] = RelationshipTypeDef("REL", (4, 99))
    corrupt._invalidate_derived()
    pool, store, images = staged(corrupt, 1000)
    before = store.read_from_pages()
    writes = list(pool.storage.write_calls)
    with pytest.raises(GrafxCorruptionDetected):
        CommitRedo(pool).apply(committed_replay(records(images, 1000)))
    assert pool.storage.write_calls == writes
    assert store.read_from_pages() == before


@pytest.mark.parametrize("change", ["remove", "rename", "membership", "endpoints", "physical_name"])
def test_replay_cannot_redefine_previously_proved_group_identity(change: str) -> None:
    first = with_group()
    later = schema()
    if change == "endpoints":
        later._install_table(replace(later.table("AB"), to_table="C"))
    elif change == "physical_name":
        # Rebuild the source fixture so it remains individually canonical.
        del later._tables[("rel", "AB")]
        later._install_table(replace(later.table_by_id(4), name="AC"))
    if change != "remove":
        later.add_relationship_type(RelationshipTypeDef(
            "OTHER" if change == "rename" else "REL", (4,) if change == "membership" else (4, 5),
        ))
    assert Catalog.deserialize(later.serialize()) == later
    pool, store, first_images = staged(first, 1000)
    _, _, later_images = staged(later, 2000)
    before = store.read_from_pages()
    writes = list(pool.storage.write_calls)
    replay = committed_replay((*records(first_images, 1000), *records(later_images, 2000)))
    with pytest.raises(GrafxRecoveryRefused) as error:
        CommitRedo(pool).apply(replay)
    assert error.value.details["field"] == "relationship_types"
    assert store.read_from_pages() == before
    assert pool.storage.write_calls == writes


def test_replay_permits_new_group_without_changing_previous_identity() -> None:
    first = with_group()
    later = first.copy()
    later.add_table(TableDef(6, "CA", "rel", (), from_table="C", to_table="A"))
    later.add_relationship_type(RelationshipTypeDef("BACK", (6,)))
    pool, store, first_images = staged(first, 1000)
    _, _, later_images = staged(later, 2000)
    replay = committed_replay((*records(first_images, 1000), *records(later_images, 2000)))
    CommitRedo(pool).apply(replay)
    assert store.read_from_pages() == later
