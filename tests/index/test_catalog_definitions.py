"""Focused contract tests for catalog-owned logical index generations."""

from __future__ import annotations

from dataclasses import replace

import pytest

from okto_grafx.domain.errors import GrafxIndexError
from okto_grafx.domain.index.catalog import (
    IDENTITY_SECONDARY_INDEXES_V1_CAPABILITY,
    CatalogIndexDefinition,
    IndexGenerationDescriptor,
    IndexGenerationState,
)
from okto_grafx.domain.index.definition import RECORD_ID_KEY_DERIVATION
from okto_grafx.domain.index.visibility import IndexVisibility


def generation(
    nonce: int,
    state: IndexGenerationState | str = IndexGenerationState.STALE,
    *,
    buckets: int = 64,
) -> IndexGenerationDescriptor:
    """Build one concise generation value for these tests."""

    return IndexGenerationDescriptor(nonce, buckets, state)  # type: ignore[arg-type]


def exact_definition(
    *generations: IndexGenerationDescriptor,
    name: str = "by_email",
) -> CatalogIndexDefinition:
    """Build an ordinary exact logical definition."""

    return CatalogIndexDefinition(
        name=name,
        table_id=7,
        table_name="Person",
        positions=(2,),
        visibility=IndexVisibility.EXACT,
        expected_cardinality=4096,
        generations=tuple(generations),
    )


def test_the_capability_spelling_and_canonical_generation_path_are_pinned() -> None:
    assert IDENTITY_SECONDARY_INDEXES_V1_CAPABILITY == ("identity_secondary_indexes_v1")
    assert generation(1).file == "index/g_0000000000000001.idx"
    assert generation(0xFFFFFFFFFFFFFFFF).file == "index/g_ffffffffffffffff.idx"


@pytest.mark.parametrize("nonce", [0, -1, 2**64, True, 1.0, None])
def test_a_generation_nonce_must_be_non_zero_u64(nonce: object) -> None:
    with pytest.raises(GrafxIndexError) as refused:
        IndexGenerationDescriptor(nonce, 64, "active")  # type: ignore[arg-type]

    assert refused.value.details["field"] == "artifact_nonce"


@pytest.mark.parametrize("bucket_count", [0, 4097, True, 1.0, None])
def test_a_generation_bucket_count_uses_the_bounded_index_domain(
    bucket_count: object,
) -> None:
    with pytest.raises(GrafxIndexError) as refused:
        IndexGenerationDescriptor(1, bucket_count, "active")  # type: ignore[arg-type]

    assert refused.value.details["field"] == "bucket_count"


def test_generation_state_is_parsed_strictly_and_controls_eligibility() -> None:
    active = generation(1, "active")

    assert active.state is IndexGenerationState.ACTIVE
    assert active.planner_eligible
    assert active.activate() is active
    assert active.mark_stale() == generation(1, "stale")
    assert not active.mark_stale().planner_eligible

    with pytest.raises(GrafxIndexError) as unknown:
        generation(2, "ACTIVE")
    assert unknown.value.details["field"] == "state"

    with pytest.raises(GrafxIndexError) as resurrected:
        generation(3, "stale").activate()
    assert resurrected.value.details["field"] == "state"
    assert resurrected.value.details["value"] == "stale"


def test_a_regular_definition_is_case_safe_and_materializes_runtime_fields() -> None:
    active = generation(11, "active", buckets=256)
    definition = exact_definition(active, name="By_Email")

    assert definition.registry_key == "by_email"
    assert definition.visibility is IndexVisibility.EXACT
    assert definition.active_generation() is active
    assert definition.building_generation() is None

    runtime = definition.runtime_definition()
    assert runtime.name == "By_Email"
    assert runtime.table_id == 7
    assert runtime.table_name == "Person"
    assert runtime.positions == (2,)
    assert runtime.bucket_count == 256
    assert runtime.artifact_nonce == 11
    assert runtime.file == "index/g_000000000000000b.idx"


def test_a_shadow_can_be_materialized_without_becoming_planner_eligible() -> None:
    active = generation(10, "active")
    building = generation(20, "building", buckets=128)
    definition = exact_definition(active, building)

    assert definition.building_generation() is building
    assert definition.runtime_definition(building).bucket_count == 128
    assert definition.active_generation() is active

    foreign = generation(30, "building", buckets=128)
    with pytest.raises(GrafxIndexError) as refused:
        definition.runtime_definition(foreign)
    assert refused.value.details["field"] == "generation"


def test_adding_and_activating_a_shadow_is_pure_and_retires_the_old_active() -> None:
    original = exact_definition(generation(10, "active"))
    with_shadow = original.with_generation(generation(5, "building", buckets=128))

    assert tuple(item.artifact_nonce for item in original.generations) == (10,)
    assert tuple(item.artifact_nonce for item in with_shadow.generations) == (5, 10)

    activated = with_shadow.activate_generation(5)
    assert activated.active_generation() == generation(5, "active", buckets=128)
    assert activated.generation(10).state is IndexGenerationState.STALE
    assert with_shadow.generation(10).state is IndexGenerationState.ACTIVE

    with pytest.raises(GrafxIndexError) as duplicate:
        activated.with_generation(generation(5))
    assert duplicate.value.details["field"] == "artifact_nonce"


def test_marking_a_generation_stale_is_pure_and_idempotent() -> None:
    definition = exact_definition(generation(4, "active"))

    stale = definition.mark_generation_stale(4)
    assert stale.active_generation() is None
    assert stale.generation(4).state is IndexGenerationState.STALE
    assert stale.mark_generation_stale(4) is stale
    assert definition.active_generation() is not None

    with pytest.raises(GrafxIndexError) as missing:
        definition.mark_generation_stale(99)
    assert missing.value.details["field"] == "artifact_nonce"


def test_an_identity_definition_pins_exact_empty_system_managed_semantics() -> None:
    definition = CatalogIndexDefinition(
        name="rid_t_00000007",
        table_id=7,
        table_name="Person",
        positions=(),
        visibility="exact",  # type: ignore[arg-type]
        key_derivation=RECORD_ID_KEY_DERIVATION,
        automatic=True,
        generations=(generation(8, "active"),),
    )

    runtime = definition.runtime_definition()
    assert runtime.positions == ()
    assert runtime.key_derivation == RECORD_ID_KEY_DERIVATION
    assert runtime.artifact_nonce == 8


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"positions": (0,)}, "positions"),
        ({"visibility": "proximity"}, "visibility"),
        ({"automatic": False}, "automatic"),
        ({"name": "rid_t_00000008"}, "name"),
    ],
)
def test_identity_definitions_refuse_any_semantic_alias(
    change: dict[str, object], field: str
) -> None:
    fields: dict[str, object] = {
        "name": "rid_t_00000007",
        "table_id": 7,
        "table_name": "Person",
        "positions": (),
        "visibility": "exact",
        "key_derivation": RECORD_ID_KEY_DERIVATION,
        "automatic": True,
    }
    fields.update(change)

    with pytest.raises(GrafxIndexError) as refused:
        CatalogIndexDefinition(**fields)  # type: ignore[arg-type]
    assert refused.value.details["field"] == field


def test_the_identity_namespace_is_reserved_case_insensitively() -> None:
    with pytest.raises(GrafxIndexError) as refused:
        exact_definition(name="RID_T_user")

    assert refused.value.details["field"] == "name"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "bad/name"),
        ("table_id", 0),
        ("table_id", True),
        ("table_id", 2**32),
        ("table_name", "bad/name"),
        ("positions", []),
        ("positions", (True,)),
        ("positions", (-1,)),
        ("positions", (2**32,)),
        ("positions", (1, 1)),
        ("key_derivation", "bad/name"),
        ("automatic", 1),
        ("expected_cardinality", 0),
        ("expected_cardinality", True),
        ("expected_cardinality", 2**64),
        ("generations", []),
    ],
)
def test_logical_definition_fields_are_strongly_validated(
    field: str, value: object
) -> None:
    definition = exact_definition(generation(1, "active"))

    with pytest.raises(GrafxIndexError) as refused:
        replace(definition, **{field: value})
    assert refused.value.details["field"] == field


def test_expected_cardinality_accepts_the_supported_eager_directory_domain() -> None:
    assert (
        replace(exact_definition(), expected_cardinality=None).expected_cardinality
        is None
    )
    assert replace(exact_definition(), expected_cardinality=1).expected_cardinality == 1
    assert (
        replace(exact_definition(), expected_cardinality=262_144).expected_cardinality
        == 262_144
    )
    with pytest.raises(GrafxIndexError) as refused:
        replace(exact_definition(), expected_cardinality=262_145)
    assert refused.value.details["field"] == "expected_cardinality"


@pytest.mark.parametrize(
    "generations",
    [
        (generation(2), generation(1)),
        (generation(1), generation(1)),
        (generation(1, "active"), generation(2, "active")),
        (generation(1, "building"), generation(2, "building")),
        (generation(1), object()),
    ],
)
def test_generation_sets_are_canonical_and_have_one_live_role_each(
    generations: tuple[object, ...],
) -> None:
    with pytest.raises(GrafxIndexError) as refused:
        replace(exact_definition(), generations=generations)

    assert refused.value.details["field"] == "generations"


def test_a_non_identity_derivation_needs_at_least_one_position() -> None:
    with pytest.raises(GrafxIndexError) as refused:
        replace(exact_definition(), positions=())

    assert refused.value.details["field"] == "positions"


@pytest.mark.parametrize("name", ["g_0000000000000001", "G_DEADBEEFDEADBEEF"])
def test_physical_generation_names_are_reserved_from_the_logical_namespace(
    name: str,
) -> None:
    with pytest.raises(GrafxIndexError) as refused:
        exact_definition(name=name)

    assert refused.value.details["field"] == "name"


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"visibility": "proximity"}, "visibility"),
        ({"key_derivation": "vector_digest_v1"}, "key_derivation"),
        ({"key_derivation": "future_derivation_v1"}, "key_derivation"),
    ],
)
def test_p2_id_catalog_definitions_are_exact_and_have_a_supported_derivation(
    change: dict[str, object], field: str
) -> None:
    with pytest.raises(GrafxIndexError) as refused:
        replace(exact_definition(), **change)

    assert refused.value.details["field"] == field


def test_runtime_materialization_refuses_a_definition_without_an_active_generation() -> (
    None
):
    definition = exact_definition(generation(1, "stale"))

    with pytest.raises(GrafxIndexError) as refused:
        definition.runtime_definition()

    assert refused.value.details["field"] == "generations"
