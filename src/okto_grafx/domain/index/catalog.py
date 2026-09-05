"""Durable catalog authority for logical indexes and their physical generations.

An :class:`~okto_grafx.domain.index.definition.IndexDefinition` describes the bytes one
physical index file means.  Catalog format 2 needs one level above that: a stable logical name
may own several immutable physical generations while exactly one of them is eligible for new
statements.  The values in this module express that authority without reading or writing storage.

Generation paths are never supplied by a caller.  They are derived from a non-zero unsigned
64-bit nonce, which keeps catalog bytes portable and prevents a persisted path from escaping the
index directory.  State changes return new frozen values; publishing one remains the catalog/WAL
layer's responsibility.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum

from okto_grafx.domain.errors import GrafxIndexError
from okto_grafx.domain.index.definition import (
    COLUMN_KEY_DERIVATION,
    RECORD_ID_KEY_DERIVATION,
    IndexDefinition,
    index_generation_file,
    require_index_name,
)
from okto_grafx.domain.index.keys import (
    MAX_EXPECTED_CARDINALITY,
    validate_bucket_count,
)
from okto_grafx.domain.index.visibility import IndexVisibility
from okto_grafx.domain.model.schema import is_identifier

__all__ = [
    "IDENTITY_SECONDARY_INDEXES_V1_CAPABILITY",
    "CatalogIndexDefinition",
    "IndexGenerationDescriptor",
    "IndexGenerationState",
    "identity_index_name",
]

IDENTITY_SECONDARY_INDEXES_V1_CAPABILITY: str = "identity_secondary_indexes_v1"
"""Required catalog-v2 capability introduced by P2-ID v1."""

_MAX_U32: int = 0xFFFFFFFF
_MAX_U64: int = 0xFFFFFFFFFFFFFFFF
_IDENTITY_INDEX_PREFIX: str = "rid_t_"
_HEX_DIGITS: frozenset[str] = frozenset("0123456789abcdefABCDEF")


def _is_generation_logical_name(name: str) -> bool:
    """Return whether a logical name is reserved for a physical generation artifact."""

    return (
        len(name) == 18
        and name[:2] in ("g_", "G_")
        and all(character in _HEX_DIGITS for character in name[2:])
    )


def identity_index_name(table_id: object) -> str:
    """Return the sole reserved logical identity-index name for one catalog table id."""

    if (
        isinstance(table_id, bool)
        or not isinstance(table_id, int)
        or not 1 <= table_id <= _MAX_U32
    ):
        raise GrafxIndexError(
            "An identity index name needs a table_id between 1 and 4294967295.",
            field="table_id",
            value=repr(table_id),
        )
    return f"{_IDENTITY_INDEX_PREFIX}{table_id:08x}"


class IndexGenerationState(str, Enum):
    """The publication state of one immutable physical index generation."""

    BUILDING = "building"
    ACTIVE = "active"
    STALE = "stale"

    @classmethod
    def parse(cls, value: object) -> IndexGenerationState:
        """Return a generation state, refusing unknown or differently-cased spellings."""

        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            for candidate in cls:
                if candidate.value == value:
                    return candidate
        allowed = ", ".join(repr(candidate.value) for candidate in cls)
        raise GrafxIndexError(
            f"An index generation state must be one of {allowed}; got {value!r}.",
            field="state",
            value=repr(value),
        )


@dataclass(frozen=True, slots=True)
class IndexGenerationDescriptor:
    """Catalog identity and lifecycle state of one physical index artifact."""

    artifact_nonce: int
    bucket_count: int
    state: IndexGenerationState

    def __post_init__(self) -> None:
        """Refuse a descriptor that cannot be encoded or name its canonical artifact."""

        if (
            isinstance(self.artifact_nonce, bool)
            or not isinstance(self.artifact_nonce, int)
            or not 1 <= self.artifact_nonce <= _MAX_U64
        ):
            raise GrafxIndexError(
                "An index generation needs a non-zero unsigned 64-bit artifact nonce.",
                field="artifact_nonce",
                value=repr(self.artifact_nonce),
            )
        object.__setattr__(
            self, "bucket_count", validate_bucket_count(self.bucket_count)
        )
        object.__setattr__(self, "state", IndexGenerationState.parse(self.state))

    @property
    def file(self) -> str:
        """Return the sole physical filename this generation nonce is allowed to name."""

        return index_generation_file(self.artifact_nonce)

    @property
    def planner_eligible(self) -> bool:
        """Say whether catalog state permits freshness checks to consider this generation."""

        return self.state is IndexGenerationState.ACTIVE

    def activate(self) -> IndexGenerationDescriptor:
        """Return this building generation as active; never resurrect a stale artifact."""

        if self.state is IndexGenerationState.ACTIVE:
            return self
        if self.state is IndexGenerationState.STALE:
            raise GrafxIndexError(
                "A stale index generation cannot become active again; build a new generation.",
                field="state",
                value=self.state.value,
                artifact_nonce=self.artifact_nonce,
            )
        return replace(self, state=IndexGenerationState.ACTIVE)

    def mark_stale(self) -> IndexGenerationDescriptor:
        """Return this generation as ineligible, preserving an already-stale value."""

        if self.state is IndexGenerationState.STALE:
            return self
        return replace(self, state=IndexGenerationState.STALE)


@dataclass(frozen=True, slots=True)
class CatalogIndexDefinition:
    """Persisted logical index definition and its immutable physical generations.

    ``bucket_count`` and ``artifact_nonce`` live on each generation because changing either
    creates a different physical meaning.  The remaining fields identify the logical access
    path and survive a growth-only rehash.
    """

    name: str
    table_id: int
    table_name: str
    positions: tuple[int, ...]
    visibility: IndexVisibility
    key_derivation: str = COLUMN_KEY_DERIVATION
    automatic: bool = False
    expected_cardinality: int | None = None
    generations: tuple[IndexGenerationDescriptor, ...] = ()
    # One private slot per logical definition: the runtime definition last materialised, keyed
    # by the identity of the generation descriptor it was built from.  It is not a field of the
    # value (never compared, printed, replaced or persisted) and it never outlives this object:
    # every generation change builds a new logical definition through ``replace``.
    _runtime: tuple[IndexGenerationDescriptor, IndexDefinition] | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        """Validate the complete, canonically ordered logical authority."""

        require_index_name(self.name)
        if _is_generation_logical_name(self.name):
            raise GrafxIndexError(
                "Logical index names matching g_<16 hex digits> are reserved for physical "
                "generation artifacts.",
                field="name",
                value=self.name,
                index=self.name,
            )
        if (
            isinstance(self.table_id, bool)
            or not isinstance(self.table_id, int)
            or not 1 <= self.table_id <= _MAX_U32
        ):
            raise GrafxIndexError(
                "A catalog index needs a table_id between 1 and 4294967295.",
                field="table_id",
                value=repr(self.table_id),
                index=self.name,
            )
        if not is_identifier(self.table_name):
            raise GrafxIndexError(
                f"A catalog index must name its table; got {self.table_name!r}.",
                field="table_name",
                value=repr(self.table_name),
                index=self.name,
            )
        if not isinstance(self.positions, tuple):
            raise GrafxIndexError(
                "Catalog index positions must be a tuple in declared key order.",
                field="positions",
                value=repr(self.positions),
                index=self.name,
            )
        seen_positions: set[int] = set()
        for position in self.positions:
            if (
                isinstance(position, bool)
                or not isinstance(position, int)
                or not 0 <= position <= _MAX_U32
            ):
                raise GrafxIndexError(
                    "Every catalog index position must be an unsigned 32-bit integer.",
                    field="positions",
                    value=repr(position),
                    index=self.name,
                )
            if position in seen_positions:
                raise GrafxIndexError(
                    f"Catalog index {self.name!r} names position {position} twice.",
                    field="positions",
                    value=position,
                    index=self.name,
                )
            seen_positions.add(position)
        if not is_identifier(self.key_derivation):
            raise GrafxIndexError(
                "A catalog index key derivation must be an ASCII identifier.",
                field="key_derivation",
                value=repr(self.key_derivation),
                index=self.name,
            )
        if not isinstance(self.automatic, bool):
            raise GrafxIndexError(
                "A catalog index automatic flag must be a bool.",
                field="automatic",
                value=repr(self.automatic),
                index=self.name,
            )
        if self.expected_cardinality is not None and (
            isinstance(self.expected_cardinality, bool)
            or not isinstance(self.expected_cardinality, int)
            or not 1 <= self.expected_cardinality <= MAX_EXPECTED_CARDINALITY
        ):
            raise GrafxIndexError(
                "An expected cardinality must fit the eager hash-directory limit "
                f"1..{MAX_EXPECTED_CARDINALITY}, or be None.",
                field="expected_cardinality",
                value=repr(self.expected_cardinality),
                index=self.name,
            )

        visibility = IndexVisibility.parse(self.visibility)
        object.__setattr__(self, "visibility", visibility)
        if visibility is not IndexVisibility.EXACT:
            raise GrafxIndexError(
                "Catalog-v2 managed indexes are exact access paths; proximity/vector "
                "indexes remain derived from the table schema in P2-ID v1.",
                field="visibility",
                value=visibility.value,
                index=self.name,
            )
        if self.key_derivation not in {
            COLUMN_KEY_DERIVATION,
            RECORD_ID_KEY_DERIVATION,
        }:
            raise GrafxIndexError(
                "P2-ID v1 persists only the columns and record_id_u64_v1 key derivations.",
                field="key_derivation",
                value=self.key_derivation,
                index=self.name,
            )
        self._validate_key_contract(visibility)
        self._validate_generations()

    def _validate_key_contract(self, visibility: IndexVisibility) -> None:
        """Enforce the reserved RecordId derivation and namespace as one indivisible rule."""

        is_identity = self.key_derivation == RECORD_ID_KEY_DERIVATION
        occupies_identity_namespace = self.registry_key.startswith(
            _IDENTITY_INDEX_PREFIX
        )
        expected_identity_name = identity_index_name(self.table_id)
        if is_identity:
            if self.positions:
                raise GrafxIndexError(
                    "A RecordId index keys row identity, so its positions tuple must be empty.",
                    field="positions",
                    value=repr(self.positions),
                    index=self.name,
                )
            if visibility is not IndexVisibility.EXACT:
                raise GrafxIndexError(
                    "A RecordId index must use exact visibility.",
                    field="visibility",
                    value=visibility.value,
                    index=self.name,
                )
            if not self.automatic:
                raise GrafxIndexError(
                    "A RecordId index is a system-managed automatic index.",
                    field="automatic",
                    value=False,
                    index=self.name,
                )
            if self.name != expected_identity_name:
                raise GrafxIndexError(
                    f"The identity index for table {self.table_id} must be named "
                    f"{expected_identity_name!r}.",
                    field="name",
                    value=self.name,
                    index=self.name,
                )
            return
        if not self.positions:
            raise GrafxIndexError(
                "Only the RecordId derivation may have no column positions.",
                field="positions",
                value=repr(self.positions),
                index=self.name,
            )
        if occupies_identity_namespace:
            raise GrafxIndexError(
                "The rid_t_ logical-name namespace is reserved for automatic RecordId indexes.",
                field="name",
                value=self.name,
                index=self.name,
            )

    def _validate_generations(self) -> None:
        """Require typed, nonce-ordered generations and unambiguous publication state."""

        if not isinstance(self.generations, tuple):
            raise GrafxIndexError(
                "Catalog index generations must be a tuple in ascending nonce order.",
                field="generations",
                value=repr(self.generations),
                index=self.name,
            )
        previous_nonce = 0
        active_count = 0
        building_count = 0
        for generation in self.generations:
            if not isinstance(generation, IndexGenerationDescriptor):
                raise GrafxIndexError(
                    "A catalog index generation must be an IndexGenerationDescriptor.",
                    field="generations",
                    value=type(generation).__name__,
                    index=self.name,
                )
            if generation.artifact_nonce <= previous_nonce:
                raise GrafxIndexError(
                    "Catalog index generations must have unique nonces in ascending order.",
                    field="generations",
                    value=generation.artifact_nonce,
                    index=self.name,
                )
            previous_nonce = generation.artifact_nonce
            active_count += generation.state is IndexGenerationState.ACTIVE
            building_count += generation.state is IndexGenerationState.BUILDING
        if active_count > 1:
            raise GrafxIndexError(
                "A logical index may have at most one active generation.",
                field="generations",
                state=IndexGenerationState.ACTIVE.value,
                count=active_count,
                index=self.name,
            )
        if building_count > 1:
            raise GrafxIndexError(
                "A logical index may have at most one building generation.",
                field="generations",
                state=IndexGenerationState.BUILDING.value,
                count=building_count,
                index=self.name,
            )

    @property
    def registry_key(self) -> str:
        """Return the ASCII case-insensitive identity used for collision checks and ordering."""

        return self.name.lower()

    def active_generation(self) -> IndexGenerationDescriptor | None:
        """Return the sole planner-eligible generation, if one is published."""

        return next(
            (
                generation
                for generation in self.generations
                if generation.state is IndexGenerationState.ACTIVE
            ),
            None,
        )

    def building_generation(self) -> IndexGenerationDescriptor | None:
        """Return the sole in-progress shadow generation, if one is recorded."""

        return next(
            (
                generation
                for generation in self.generations
                if generation.state is IndexGenerationState.BUILDING
            ),
            None,
        )

    def generation(self, artifact_nonce: object) -> IndexGenerationDescriptor:
        """Return a generation by durable identity, with a typed refusal on a miss."""

        if isinstance(artifact_nonce, bool) or not isinstance(artifact_nonce, int):
            raise GrafxIndexError(
                "A generation lookup needs an integer artifact nonce.",
                field="artifact_nonce",
                value=repr(artifact_nonce),
                index=self.name,
            )
        for generation in self.generations:
            if generation.artifact_nonce == artifact_nonce:
                return generation
        raise GrafxIndexError(
            f"Index {self.name!r} has no generation with nonce {artifact_nonce!r}.",
            field="artifact_nonce",
            value=repr(artifact_nonce),
            index=self.name,
        )

    def with_generation(
        self, generation: IndexGenerationDescriptor
    ) -> CatalogIndexDefinition:
        """Return this logical index with one new, uniquely nonced generation."""

        if not isinstance(generation, IndexGenerationDescriptor):
            raise GrafxIndexError(
                "A catalog index generation must be an IndexGenerationDescriptor.",
                field="generation",
                value=type(generation).__name__,
                index=self.name,
            )
        if any(
            current.artifact_nonce == generation.artifact_nonce
            for current in self.generations
        ):
            raise GrafxIndexError(
                f"Index {self.name!r} already has generation {generation.artifact_nonce}.",
                field="artifact_nonce",
                value=generation.artifact_nonce,
                index=self.name,
            )
        generations = tuple(
            sorted(
                (*self.generations, generation), key=lambda item: item.artifact_nonce
            )
        )
        return replace(self, generations=generations)

    def activate_generation(self, artifact_nonce: object) -> CatalogIndexDefinition:
        """Atomically select a building shadow and retire the previous active generation."""

        selected = self.generation(artifact_nonce)
        if selected.state is IndexGenerationState.ACTIVE:
            return self
        activated = selected.activate()
        generations = tuple(
            activated
            if current.artifact_nonce == selected.artifact_nonce
            else current.mark_stale()
            if current.state is IndexGenerationState.ACTIVE
            else current
            for current in self.generations
        )
        return replace(self, generations=generations)

    def mark_generation_stale(self, artifact_nonce: object) -> CatalogIndexDefinition:
        """Return this logical index with the selected generation made ineligible."""

        selected = self.generation(artifact_nonce)
        if selected.state is IndexGenerationState.STALE:
            return self
        generations = tuple(
            current.mark_stale()
            if current.artifact_nonce == selected.artifact_nonce
            else current
            for current in self.generations
        )
        return replace(self, generations=generations)

    def runtime_definition(
        self, generation: IndexGenerationDescriptor | None = None
    ) -> IndexDefinition:
        """Materialize physical runtime fields for one catalog-owned generation.

        With no explicit generation the active one is selected.  Passing a descriptor is useful
        to build or verify a shadow, but it must be the exact value owned by this logical
        definition; a foreign descriptor with a colliding nonce is refused.
        """

        selected = self.active_generation() if generation is None else generation
        if selected is None:
            raise GrafxIndexError(
                f"Index {self.name!r} has no active generation.",
                field="generations",
                index=self.name,
            )
        if (
            not isinstance(selected, IndexGenerationDescriptor)
            or selected not in self.generations
        ):
            raise GrafxIndexError(
                "A runtime definition needs a generation owned by this logical index.",
                field="generation",
                value=repr(selected),
                index=self.name,
            )
        # The ownership refusal above runs on every call.  Only the construction -- and the
        # full IndexDefinition validation it repeats -- is reused, and only for the very same
        # descriptor object: an equal but distinct descriptor rebuilds, a foreign one refused.
        cached = self._runtime
        if cached is not None and cached[0] is selected:
            return cached[1]
        definition = self._build_runtime_definition(selected)
        object.__setattr__(self, "_runtime", (selected, definition))
        return definition

    def _build_runtime_definition(
        self, selected: IndexGenerationDescriptor
    ) -> IndexDefinition:
        """Construct -- and fully validate -- the runtime definition of one owned generation."""
        return IndexDefinition(
            name=self.name,
            table_id=self.table_id,
            table_name=self.table_name,
            positions=self.positions,
            visibility=self.visibility,
            bucket_count=selected.bucket_count,
            key_derivation=self.key_derivation,
            artifact_nonce=selected.artifact_nonce,
        )
