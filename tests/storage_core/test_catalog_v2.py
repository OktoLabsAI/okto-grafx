"""Catalog-v2 authority, its exact bytes, and its v1 compatibility fence.

These tests deliberately keep their own structs and tag values.  The production encoder is the
subject under test; importing its new structs here would let both sides drift together.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import replace

import pytest

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxIndexError,
    GrafxSchemaVersionMismatch,
)
from okto_grafx.domain.index.catalog import (
    IDENTITY_SECONDARY_INDEXES_V1_CAPABILITY,
    CatalogIndexDefinition,
    IndexGenerationDescriptor,
    IndexGenerationState,
)
from okto_grafx.domain.index.definition import RECORD_ID_KEY_DERIVATION
from okto_grafx.domain.index.visibility import IndexVisibility
from okto_grafx.domain.model import catalog as catalog_module
from okto_grafx.domain.model.catalog import (
    CATALOG_FORMAT_VERSION,
    CATALOG_LEGACY_FORMAT_VERSION,
    CATALOG_MAGIC,
    HEAP_RECLAIM_V1_CAPABILITY,
    Catalog,
)
from okto_grafx.domain.model.schema import ColumnDef, EmbeddingSpaceDef, TableDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.page import crc32c
from okto_grafx.domain.ports.vectormath import DistanceMetric


_PREAMBLE = struct.Struct("<8sHHIIII")
_V2_EXTENSION = struct.Struct("<QII")
_INDEX_FIELDS = struct.Struct("<BBBBHHQ")
_GENERATION = struct.Struct("<QIB3x")
_U16 = struct.Struct("<H")
_U32 = struct.Struct("<I")
_CHECKSUM = struct.Struct("<I")

_CAPABILITY_IDENTITY = 1 << 0
_CAPABILITY_HEAP_RECLAIM = 1 << 1
_VISIBILITY_EXACT = 1
_DERIVATION_COLUMNS = 1
_DERIVATION_RECORD_ID = 2
_STATE_BUILDING = 1
_STATE_ACTIVE = 2
_STATE_STALE = 3


def _text(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return _U16.pack(len(encoded)) + encoded


def _generation(
    nonce: int,
    state: IndexGenerationState | str = IndexGenerationState.ACTIVE,
    *,
    buckets: int = 64,
) -> IndexGenerationDescriptor:
    return IndexGenerationDescriptor(nonce, buckets, state)  # type: ignore[arg-type]


def _person() -> TableDef:
    return TableDef(
        table_id=1,
        name="Person",
        kind="node",
        columns=(
            ColumnDef(name="id", type=ValueType.INT64, nullable=False),
            ColumnDef(name="email", type=ValueType.STRING),
            ColumnDef(name="tenant", type=ValueType.STRING),
        ),
        primary_key="id",
    )


def _schema_catalog(*, isolated: bool = False) -> Catalog:
    catalog = Catalog()
    catalog.add_table(_person())
    if not isolated:
        catalog.add_table(
            TableDef(
                table_id=2,
                name="Knows",
                kind="rel",
                columns=(ColumnDef(name="weight", type=ValueType.DOUBLE),),
                from_table="Person",
                to_table="Person",
            )
        )
    return catalog


def _identity(*, nonce: int = 21) -> CatalogIndexDefinition:
    return CatalogIndexDefinition(
        name="rid_t_00000001",
        table_id=1,
        table_name="Person",
        positions=(),
        visibility=IndexVisibility.EXACT,
        key_derivation=RECORD_ID_KEY_DERIVATION,
        automatic=True,
        generations=(_generation(nonce),),
    )


def _custom(
    *,
    name: str = "by_email",
    nonce: int = 11,
    positions: tuple[int, ...] = (1,),
    generations: tuple[IndexGenerationDescriptor, ...] | None = None,
) -> CatalogIndexDefinition:
    return CatalogIndexDefinition(
        name=name,
        table_id=1,
        table_name="Person",
        positions=positions,
        visibility=IndexVisibility.EXACT,
        expected_cardinality=4096,
        generations=generations or (_generation(nonce, buckets=128),),
    )


def _automatic_rehash(*, nonce: int = 31) -> CatalogIndexDefinition:
    return CatalogIndexDefinition(
        name="pk_Person",
        table_id=1,
        table_name="Person",
        positions=(0,),
        visibility=IndexVisibility.EXACT,
        automatic=True,
        expected_cardinality=8192,
        generations=(_generation(nonce, buckets=256),),
    )


def _definition_bytes(definition: CatalogIndexDefinition) -> bytes:
    visibility = {IndexVisibility.EXACT: _VISIBILITY_EXACT}[definition.visibility]
    derivation = {
        "columns": _DERIVATION_COLUMNS,
        RECORD_ID_KEY_DERIVATION: _DERIVATION_RECORD_ID,
    }[definition.key_derivation]
    state_tags = {
        IndexGenerationState.BUILDING: _STATE_BUILDING,
        IndexGenerationState.ACTIVE: _STATE_ACTIVE,
        IndexGenerationState.STALE: _STATE_STALE,
    }
    parts = [
        _text(definition.name),
        _U32.pack(definition.table_id),
        _text(definition.table_name),
        _INDEX_FIELDS.pack(
            visibility,
            derivation,
            1 if definition.automatic else 0,
            0,
            len(definition.positions),
            len(definition.generations),
            definition.expected_cardinality or 0,
        ),
        b"".join(_U32.pack(position) for position in definition.positions),
    ]
    parts.extend(
        _GENERATION.pack(
            generation.artifact_nonce,
            generation.bucket_count,
            state_tags[generation.state],
        )
        for generation in definition.generations
    )
    return b"".join(parts)


def _forge_v2(
    catalog: Catalog,
    definitions: tuple[CatalogIndexDefinition, ...],
    *,
    capability_bits: int = _CAPABILITY_IDENTITY,
    extension_reserved: int = 0,
    preamble_reserved: int = 0,
) -> bytes:
    tables = catalog.tables()
    spaces = catalog.spaces()
    body = _PREAMBLE.pack(
        CATALOG_MAGIC,
        CATALOG_FORMAT_VERSION,
        preamble_reserved,
        len(tables),
        len(spaces),
        catalog.next_table_id(),
        catalog.next_space_id(),
    )
    body += _V2_EXTENSION.pack(capability_bits, len(definitions), extension_reserved)
    body += b"".join(catalog_module._encode_table(table) for table in tables)
    body += b"".join(catalog_module._encode_space(space) for space in spaces)
    body += b"".join(_definition_bytes(definition) for definition in definitions)
    return body + _CHECKSUM.pack(crc32c(body))


def _with_checksum(raw: bytes | bytearray) -> bytes:
    body = bytes(raw[: -_CHECKSUM.size])
    return body + _CHECKSUM.pack(crc32c(body))


def _definition_offset(catalog: Catalog) -> int:
    return (
        _PREAMBLE.size
        + _V2_EXTENSION.size
        + sum(len(catalog_module._encode_table(table)) for table in catalog.tables())
        + sum(len(catalog_module._encode_space(space)) for space in catalog.spaces())
    )


def _populated_v1() -> Catalog:
    catalog = Catalog()
    catalog.add_space(
        EmbeddingSpaceDef(
            space_id=1,
            name="minilm",
            dimension=384,
            metric=DistanceMetric.COSINE,
            normalized=True,
            storage_dtype="float32",
            created_at_wall=1_700_000_000.5,
        )
    )
    catalog.add_space(
        EmbeddingSpaceDef(
            space_id=2,
            name="e5",
            dimension=1024,
            metric=DistanceMetric.COSINE,
            normalized=True,
            storage_dtype="float64",
            created_at_wall=1_700_000_000.5,
        )
    )
    catalog.retire_space("e5")
    catalog.add_table(
        TableDef(
            table_id=1,
            name="Person",
            kind="node",
            columns=(
                ColumnDef(name="id", type=ValueType.INT64, nullable=False),
                ColumnDef(name="name", type=ValueType.STRING),
            ),
            primary_key="id",
        )
    )
    catalog.add_table(
        TableDef(
            table_id=2,
            name="Chunk",
            kind="node",
            columns=(
                ColumnDef(name="id", type=ValueType.INT64, nullable=False),
                ColumnDef(name="layer", type=ValueType.STRING),
                ColumnDef(
                    name="embedding",
                    type=ValueType.VECTOR_F32,
                    vector_space="minilm",
                ),
            ),
            primary_key="id",
        )
    )
    catalog.add_table(
        TableDef(
            table_id=3,
            name="BelongsTo",
            kind="rel",
            columns=(ColumnDef(name="weight", type=ValueType.DOUBLE),),
            from_table="Chunk",
            to_table="Person",
            schema_version=4,
        )
    )
    return catalog


def test_v1_golden_bytes_and_default_format_remain_frozen() -> None:
    assert CATALOG_LEGACY_FORMAT_VERSION == 1
    assert CATALOG_FORMAT_VERSION == 2

    empty = Catalog()
    assert empty.format_version == CATALOG_LEGACY_FORMAT_VERSION
    assert empty.required_capabilities() == ()
    assert empty.index_definitions() == ()
    assert empty.serialize().hex() == (
        "4752465843544c470100000000000000000000000100000001000000b5ca98d5"
    )

    populated = _populated_v1().serialize()
    assert len(populated) == 312
    assert hashlib.sha256(populated).hexdigest() == (
        "4ea188cdb84a945506836c4ddd867cc79f381300cfe6458563387e43a82af6ae"
    )


def test_v1_round_trip_and_unrelated_schema_work_do_not_activate_v2() -> None:
    catalog = Catalog.deserialize(_schema_catalog().serialize())
    catalog.add_table(
        TableDef(
            table_id=3,
            name="Company",
            kind="node",
            columns=(ColumnDef(name="id", type=ValueType.INT64),),
            primary_key="id",
        )
    )

    raw = catalog.serialize()
    assert int.from_bytes(raw[8:10], "little") == CATALOG_LEGACY_FORMAT_VERSION
    assert Catalog.deserialize(raw).format_version == CATALOG_LEGACY_FORMAT_VERSION


def test_v2_round_trip_preserves_capability_definitions_and_generations() -> None:
    catalog = _schema_catalog()
    custom = _custom(
        positions=(1, 2),
        generations=(
            _generation(11, "stale", buckets=64),
            _generation(12, "active", buckets=128),
            _generation(13, "building", buckets=256),
        ),
    )
    definitions = (custom, _identity(), _automatic_rehash())
    catalog.upgrade_index_catalog(definitions)

    restored = Catalog.deserialize(catalog.serialize())
    assert restored == catalog
    assert restored.format_version == CATALOG_FORMAT_VERSION
    assert restored.required_capabilities() == (
        IDENTITY_SECONDARY_INDEXES_V1_CAPABILITY,
    )
    assert restored.index_definitions() == tuple(
        sorted(definitions, key=lambda definition: definition.registry_key)
    )
    assert restored.has_index_definition("BY_EMAIL")
    assert restored.index_definition("By_Email") == custom


def test_heap_reclaim_capability_is_one_way_deterministic_and_required() -> None:
    catalog = _schema_catalog()
    catalog.upgrade_index_catalog((_identity(),))
    before = catalog.serialize()

    assert catalog.enable_heap_reclaim() is catalog
    first = catalog.serialize()
    assert catalog.enable_heap_reclaim().serialize() == first
    assert first != before
    assert Catalog.deserialize(first).required_capabilities() == (
        HEAP_RECLAIM_V1_CAPABILITY,
        IDENTITY_SECONDARY_INDEXES_V1_CAPABILITY,
    )
    assert _V2_EXTENSION.unpack_from(first, _PREAMBLE.size)[0] == (
        _CAPABILITY_IDENTITY | _CAPABILITY_HEAP_RECLAIM
    )


def test_a_pre_reclaim_v2_build_refuses_the_new_required_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _schema_catalog()
    catalog.upgrade_index_catalog((_identity(),))
    catalog.enable_heap_reclaim()
    raw = catalog.serialize()

    monkeypatch.setattr(
        catalog_module,
        "_KNOWN_CAPABILITY_BITS",
        _CAPABILITY_IDENTITY,
    )
    with pytest.raises(GrafxSchemaVersionMismatch) as raised:
        Catalog.deserialize(raw)
    assert raised.value.details["unsupported"] == _CAPABILITY_HEAP_RECLAIM


def test_v2_layout_and_numeric_tags_are_pinned_independently() -> None:
    catalog = _schema_catalog(isolated=True)
    definition = _custom()
    catalog.upgrade_index_catalog((definition,))
    raw = catalog.serialize()

    preamble = _PREAMBLE.unpack_from(raw)
    assert preamble == (CATALOG_MAGIC, 2, 0, 1, 0, 2, 1)
    assert _V2_EXTENSION.unpack_from(raw, _PREAMBLE.size) == (1, 1, 0)

    definition_offset = _definition_offset(catalog)
    assert raw[definition_offset : -_CHECKSUM.size] == _definition_bytes(definition)

    identity_catalog = _schema_catalog()
    identity = _identity()
    identity_catalog.upgrade_index_catalog((identity,))
    identity_raw = identity_catalog.serialize()
    identity_offset = _definition_offset(identity_catalog)
    assert identity_raw[identity_offset : -_CHECKSUM.size] == _definition_bytes(
        identity
    )


def test_v2_serialisation_is_canonical_across_definition_insertion_order() -> None:
    definitions = (_identity(), _custom(), _automatic_rehash())
    first = _schema_catalog()
    second = _schema_catalog()
    first.upgrade_index_catalog(definitions)
    second.upgrade_index_catalog(tuple(reversed(definitions)))

    assert first.serialize() == second.serialize()


@pytest.mark.parametrize(
    ("capability_bits", "extension_reserved", "preamble_reserved", "error"),
    [
        (0, 0, 0, GrafxCorruptionDetected),
        (_CAPABILITY_IDENTITY | (1 << 63), 0, 0, GrafxSchemaVersionMismatch),
        (_CAPABILITY_IDENTITY, 1, 0, GrafxCorruptionDetected),
        (_CAPABILITY_IDENTITY, 0, 1, GrafxCorruptionDetected),
    ],
)
def test_v2_refuses_missing_or_unknown_capabilities_and_reserved_bits(
    capability_bits: int,
    extension_reserved: int,
    preamble_reserved: int,
    error: type[Exception],
) -> None:
    raw = _forge_v2(
        _schema_catalog(),
        (_identity(),),
        capability_bits=capability_bits,
        extension_reserved=extension_reserved,
        preamble_reserved=preamble_reserved,
    )
    with pytest.raises(error):
        Catalog.deserialize(raw)


def test_v2_checksum_truncation_and_trailing_bytes_fail_closed() -> None:
    catalog = _schema_catalog()
    catalog.upgrade_index_catalog((_identity(),))
    raw = catalog.serialize()

    damaged = bytearray(raw)
    damaged[_PREAMBLE.size + 1] ^= 0x40
    with pytest.raises(GrafxCorruptionDetected):
        Catalog.deserialize(bytes(damaged))
    with pytest.raises(GrafxCorruptionDetected):
        Catalog.deserialize(raw[: _PREAMBLE.size + 5])
    with pytest.raises(GrafxCorruptionDetected):
        Catalog.deserialize(raw + b"\x00")


@pytest.mark.parametrize(
    ("field_offset", "value"),
    [
        (0, 99),  # visibility
        (1, 99),  # key derivation
        (2, 2),  # automatic bool
        (3, 1),  # reserved
    ],
)
def test_v2_refuses_unknown_index_tags_and_flags(field_offset: int, value: int) -> None:
    catalog = _schema_catalog(isolated=True)
    raw = bytearray(_forge_v2(catalog, (_custom(),)))
    offset = _definition_offset(catalog)
    name_length = _U16.unpack_from(raw, offset)[0]
    offset += _U16.size + name_length + _U32.size
    table_name_length = _U16.unpack_from(raw, offset)[0]
    fields_offset = offset + _U16.size + table_name_length
    raw[fields_offset + field_offset] = value

    with pytest.raises(GrafxCorruptionDetected):
        Catalog.deserialize(_with_checksum(raw))


def test_v2_refuses_unknown_generation_state_and_reserved_bytes() -> None:
    catalog = _schema_catalog(isolated=True)
    definition = _custom()
    raw = bytearray(_forge_v2(catalog, (definition,)))
    generation_offset = _definition_offset(catalog) + len(
        _definition_bytes(replace(definition, generations=()))
    )

    unknown_state = bytearray(raw)
    unknown_state[generation_offset + 12] = 99
    with pytest.raises(GrafxCorruptionDetected):
        Catalog.deserialize(_with_checksum(unknown_state))

    nonzero_reserved = bytearray(raw)
    nonzero_reserved[generation_offset + 13] = 1
    with pytest.raises(GrafxCorruptionDetected):
        Catalog.deserialize(_with_checksum(nonzero_reserved))


def test_v2_refuses_noncanonical_definition_and_generation_order() -> None:
    catalog = _schema_catalog(isolated=True)
    alpha = _custom(name="alpha", nonce=10)
    zulu = _custom(name="zulu", nonce=20)
    with pytest.raises(GrafxCorruptionDetected):
        Catalog.deserialize(_forge_v2(catalog, (zulu, alpha)))

    ordered = _custom(
        generations=(
            _generation(30, "stale"),
            _generation(40, "active"),
        )
    )
    encoded = _definition_bytes(ordered)
    reversed_generations = (
        encoded[: -2 * _GENERATION.size]
        + encoded[-_GENERATION.size :]
        + encoded[-2 * _GENERATION.size : -_GENERATION.size]
    )
    canonical = _forge_v2(catalog, (ordered,))
    prefix = canonical[: _definition_offset(catalog)]
    body = prefix + reversed_generations
    forged = body + _CHECKSUM.pack(crc32c(body))
    with pytest.raises(GrafxCorruptionDetected):
        Catalog.deserialize(forged)


def test_v2_refuses_casefolded_names_and_global_generation_nonce_collisions() -> None:
    catalog = _schema_catalog(isolated=True)
    with pytest.raises(GrafxCorruptionDetected):
        Catalog.deserialize(
            _forge_v2(
                catalog,
                (
                    _custom(name="By_Email", nonce=10),
                    _custom(name="by_email", nonce=20),
                ),
            )
        )

    with pytest.raises(GrafxCorruptionDetected):
        Catalog.deserialize(
            _forge_v2(
                catalog,
                (
                    _custom(name="by_email", nonce=50),
                    _custom(name="by_tenant", nonce=50),
                ),
            )
        )


@pytest.mark.parametrize(
    "definition",
    [
        CatalogIndexDefinition(
            name="foreign_table",
            table_id=99,
            table_name="Person",
            positions=(0,),
            visibility="exact",  # type: ignore[arg-type]
            generations=(_generation(51),),
        ),
        CatalogIndexDefinition(
            name="wrong_name",
            table_id=1,
            table_name="Company",
            positions=(0,),
            visibility="exact",  # type: ignore[arg-type]
            generations=(_generation(52),),
        ),
        CatalogIndexDefinition(
            name="bad_position",
            table_id=1,
            table_name="Person",
            positions=(99,),
            visibility="exact",  # type: ignore[arg-type]
            generations=(_generation(53),),
        ),
    ],
)
def test_v2_decode_classifies_invalid_table_cross_references_as_corruption(
    definition: CatalogIndexDefinition,
) -> None:
    with pytest.raises(GrafxCorruptionDetected):
        Catalog.deserialize(
            _forge_v2(
                _schema_catalog(),
                tuple(
                    sorted(
                        (definition, _identity()), key=lambda item: item.registry_key
                    )
                ),
            )
        )


def test_catalog_v2_accepts_only_capability_managed_exact_definitions() -> None:
    with pytest.raises(GrafxIndexError):
        replace(_custom(), visibility=IndexVisibility.PROXIMITY)
    with pytest.raises(GrafxIndexError):
        replace(_custom(), key_derivation="future_keys")
    with pytest.raises(GrafxConfigurationError):
        _schema_catalog(isolated=True).upgrade_index_catalog((_identity(),))


def test_add_and_replace_require_v2_and_preserve_logical_identity() -> None:
    legacy = _schema_catalog()
    with pytest.raises(GrafxConfigurationError):
        legacy.add_index_definition(_identity())

    catalog = _schema_catalog(isolated=True)
    catalog.upgrade_index_catalog()
    original = _custom()
    catalog.add_index_definition(original)
    assert catalog.index_definition("BY_EMAIL") == original

    replacement = replace(
        original,
        expected_cardinality=8192,
        generations=(
            original.generations[0].mark_stale(),
            _generation(12, "active", buckets=256),
        ),
    )
    catalog.replace_index_definition(replacement)
    assert catalog.index_definition("by_email") == replacement

    semantic_redefinition = replace(replacement, positions=(2,))
    with pytest.raises(GrafxConfigurationError):
        catalog.replace_index_definition(semantic_redefinition)


def test_catalog_equality_includes_v2_authority() -> None:
    legacy = _schema_catalog(isolated=True)
    upgraded = Catalog.deserialize(legacy.serialize())
    upgraded.upgrade_index_catalog()
    assert legacy != upgraded

    one = _schema_catalog(isolated=True)
    two = _schema_catalog(isolated=True)
    one.upgrade_index_catalog((_custom(),))
    two.upgrade_index_catalog((_custom(name="by_tenant", positions=(2,)),))
    assert one != two


def test_the_frozen_v1_version_gate_refuses_v2_before_decoding_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _schema_catalog()
    catalog.upgrade_index_catalog((_identity(),))
    raw = catalog.serialize()
    monkeypatch.setattr(catalog_module, "CATALOG_FORMAT_VERSION", 1)

    with pytest.raises(GrafxSchemaVersionMismatch):
        Catalog.deserialize(raw)
