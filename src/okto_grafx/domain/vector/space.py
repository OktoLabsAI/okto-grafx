"""Embedding space rules: what a vector must be to be stored, and which space it may be compared in.

This module is the write door of the vector subsystem (SPEC-VEC FR-1, FR-2, FR-3, BR-1, BR-5).
Everything it decides, it decides before a byte reaches a page, an index or the log; nothing here
is ever consulted on a read, because a read that has to defend itself against a malformed vector
is a read whose write door failed.

The order of the checks is load-bearing
---------------------------------------
Finiteness is tested for EVERY space, before the storage dtype is looked at. That order is not
cosmetic: a range check written first refuses a large float32 component and, in doing so, hides
the fact that nothing has yet asked whether the component is a number at all -- which is exactly
how a NaN became persistable in a float64 space once already in this build (amendment A34). The
two refusals therefore carry different ``reason`` details, so a test can prove which one answered
(A62), and each has a state only it can refuse: a NaN in a float64 space reaches only the
finiteness check, and 1e300 in a float32 space reaches only the range check.

What "normalized" means here
----------------------------
``EmbeddingSpaceDef.normalized`` is a declaration by the caller about the vectors it will supply,
and this module holds the caller to it. The engine never rescales a vector -- generating or
altering an embedding is the caller's business at the caller's pace (D6, BR-4) -- so the only
honest options were to trust the flag or to check it. Trusting it produces silently wrong
rankings in a dot-product space, where the metric assumes unit length and has no way to notice
it is absent. Checking it costs one square root per write and turns a wrong answer into a typed
refusal, so the flag is checked.
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from math import fsum, isfinite, sqrt

from okto_grafx.domain.errors import (
    GrafxEmbeddingSpaceMismatch,
    GrafxSpaceRetired,
    GrafxVectorValidationError,
)
from okto_grafx.domain.model.schema import EmbeddingSpaceDef
from okto_grafx.domain.model.value import (
    FLOAT32_OVERFLOW_THRESHOLD,
    MAX_FLOAT32,
    VectorValue,
)

__all__ = [
    "NORMALIZED_NORM_TOLERANCE",
    "STORAGE_DTYPE_FLOAT32",
    "STORAGE_DTYPE_FLOAT64",
    "round_to_storage_dtype",
    "validate_components",
    "validate_query_components",
    "require_active",
    "require_space_identity",
    "vector_of",
]

NORMALIZED_NORM_TOLERANCE: float = 1e-6
"""How far the length of a stored vector may sit from one in a space declared normalized.

The bound has to clear two sources of drift and nothing more. Rounding a unit vector into
float32 storage perturbs each component by at most one part in 2**24, which moves the length by
about 6e-8 whatever the dimension, because the length is a weighted mean of the component
errors rather than a sum of them. A caller that normalized in float32 arithmetic to begin with
brings a similar amount. 1e-6 is above both by more than an order of magnitude and far below any
scaling mistake worth catching: a vector that is twice as long as it claims misses by 1.0.
"""

STORAGE_DTYPE_FLOAT32: str = "float32"
"""The default storage dtype: half the bytes per vector, no measurable cost to a cosine ranking."""

STORAGE_DTYPE_FLOAT64: str = "float64"
"""The opt-in storage dtype, for parity with a caller that already keeps its embeddings as doubles."""

_F32 = struct.Struct("<f")


def round_to_storage_dtype(component: float, storage_dtype: str) -> float:
    """Return the double a storage slot of that dtype would read back for this component.

    A search has to score the components a page holds, not the components a caller typed. Without
    this the index and the heap would rank the same row differently in a float32 space, which is
    a divergence between two copies of one number rather than an approximation.
    """
    if storage_dtype == STORAGE_DTYPE_FLOAT32:
        return float(_F32.unpack(_F32.pack(component))[0])
    return float(component)


def _refuse(message: str, **details: object) -> GrafxVectorValidationError:
    """Build the vector validation error, which is never a corruption (A11-revised)."""
    return GrafxVectorValidationError(message, **details)


def _components_of(values: object) -> tuple[float, ...]:
    """Return the argument as a tuple of floats, refusing anything that is not a vector."""
    if isinstance(values, VectorValue):
        return values.values
    if isinstance(values, (str, bytes, bytearray)):
        raise _refuse(
            f"A vector needs a sequence of numbers; got {type(values).__name__}.",
            field="values",
            reason="not_a_sequence",
            value=type(values).__name__,
        )
    try:
        return tuple(float(component) for component in values)  # type: ignore[union-attr]
    except (TypeError, ValueError) as failure:
        raise _refuse(
            f"A vector needs a sequence of numbers; got {type(values).__name__}.",
            field="values",
            reason="not_a_sequence",
            value=type(values).__name__,
        ) from failure


def validate_components(
    space: EmbeddingSpaceDef, values: Sequence[float] | VectorValue
) -> tuple[float, ...]:
    """Return the components as they will be stored, or refuse the write (FR-1, BR-5).

    The returned tuple is already rounded to the storage dtype of the space, so the caller holds
    exactly what a later read will produce and an index entry cannot drift from its heap row.

    Refusals, each with a ``reason`` detail naming which check answered:

    * ``not_a_sequence`` -- the argument is not a sequence of numbers;
    * ``dimension_mismatch`` -- the length differs from the dimension the space declares;
    * ``non_finite`` -- a component is a NaN or an infinity, in a space of ANY dtype;
    * ``float32_range`` -- a finite component that a float32 slot could only hold as an infinity;
    * ``not_normalized`` -- the space declares normalized vectors and this one is not.

    Nothing is persisted on any of them, because the caller has not yet been given anything to
    persist: this function returns the storable form or raises.
    """
    components = _components_of(values)
    dimension = len(components)
    if dimension != space.dimension:
        raise _refuse(
            f"Embedding space {space.name!r} stores vectors of {space.dimension} components; "
            f"got {dimension}.",
            field="dimension",
            reason="dimension_mismatch",
            space=space.name,
            expected=space.dimension,
            value=dimension,
        )
    single = space.storage_dtype == STORAGE_DTYPE_FLOAT32
    stored: list[float] = []
    for position, component in enumerate(components):
        if not isfinite(component):
            raise _refuse(
                f"A vector component must be a finite number; component {position} of this "
                f"vector for embedding space {space.name!r} is {component!r}.",
                field="values",
                reason="non_finite",
                space=space.name,
                position=position,
                value=repr(component),
            )
        if single and not -FLOAT32_OVERFLOW_THRESHOLD < component < FLOAT32_OVERFLOW_THRESHOLD:
            raise _refuse(
                f"Embedding space {space.name!r} stores float32 components, which hold at most "
                f"{MAX_FLOAT32!r} in magnitude; component {position} is {component!r} and would "
                f"become an infinity on the page.",
                field="values",
                reason="float32_range",
                space=space.name,
                position=position,
                value=repr(component),
                limit=MAX_FLOAT32,
            )
        stored.append(round_to_storage_dtype(component, space.storage_dtype))
    result = tuple(stored)
    if space.normalized:
        _require_unit_length(space, result)
    return result


def validate_query_components(
    space: EmbeddingSpaceDef, values: Sequence[float] | VectorValue
) -> tuple[float, ...]:
    """Return the components of a query vector, or refuse the search (FR-1, FR-2, BR-1).

    A query is compared, never stored, and the difference decides what is checked. The dimension
    and the finiteness of every component are checked, because neither a mismatched length nor a
    NaN can produce a meaningful ranking. Three things are deliberately NOT checked:

    * the query is not rounded to the storage dtype of the space, because it is not going onto a
      page; keeping it in double precision is the more faithful comparison and, since both
      regimes use the same query against the same stored components, it costs nothing in
      agreement between them;
    * the float32 range is not checked, because an oversized query component produces an
      infinite score, which the math port refuses in one place for every operation;
    * the normalized declaration is not enforced, because it is a statement about what the space
      STORES. A query that is not unit length scales every score by the same factor under dot
      and is divided out entirely under cosine, so it can change no ranking.

    A query that carries its own space identity is checked against the space being searched
    before anything else happens, which is where the mismatch of BR-1 is caught with no distance
    computed at all.
    """
    if isinstance(values, VectorValue):
        require_space_identity(space, values.space_ref, origin="query vector")
    components = _components_of(values)
    dimension = len(components)
    if dimension != space.dimension:
        raise _refuse(
            f"Embedding space {space.name!r} compares vectors of {space.dimension} components; "
            f"this query carries {dimension}.",
            field="dimension",
            reason="dimension_mismatch",
            space=space.name,
            expected=space.dimension,
            value=dimension,
        )
    for position, component in enumerate(components):
        if not isfinite(component):
            raise _refuse(
                f"A query component must be a finite number; component {position} of this query "
                f"for embedding space {space.name!r} is {component!r}.",
                field="values",
                reason="non_finite",
                space=space.name,
                position=position,
                value=repr(component),
            )
    return components


def _require_unit_length(space: EmbeddingSpaceDef, components: tuple[float, ...]) -> None:
    """Refuse a vector whose length is not one in a space that declares normalized vectors.

    The sum is guarded because every component being finite does not make their sum finite: a
    hundred components of ~1.34e154 each have a finite square, and the EXACT sum ``fsum`` computes
    passes the largest double. Unguarded, that leaves this door as a bare ``OverflowError`` --
    a non-``Grafx*`` escape from a call named in CONTRACT.md section 8.8, out of a function whose
    whole purpose is to refuse.

    A vector that cannot have its length computed is not of length one, so the refusal is the
    same one an ordinary wrong length gets. This is the sibling of the overflow already fixed in
    ``PureVectorMath.norm``; the two ``fsum`` sites in this component had to be swept together,
    and this one sits in the domain, so it does not vary with the math adapter and the pure-vs-
    numpy parity tests structurally could not see it.
    """
    try:
        length = sqrt(fsum(component * component for component in components))
    except (OverflowError, ValueError):
        raise _refuse(
            f"Embedding space {space.name!r} declares normalized vectors, and the length of this "
            f"one leaves the range of a double before it can be compared to 1.0, so it is not a "
            f"vector of length one. The engine never rescales a caller's embedding.",
            field="values",
            reason="not_normalized",
            space=space.name,
            value="overflow",
            tolerance=NORMALIZED_NORM_TOLERANCE,
        ) from None
    if abs(length - 1.0) > NORMALIZED_NORM_TOLERANCE:
        raise _refuse(
            f"Embedding space {space.name!r} declares normalized vectors, so a stored vector "
            f"must have a length of 1.0 within {NORMALIZED_NORM_TOLERANCE!r}; this one has a "
            f"length of {length!r}. The engine never rescales a caller's embedding.",
            field="values",
            reason="not_normalized",
            space=space.name,
            value=repr(length),
            tolerance=NORMALIZED_NORM_TOLERANCE,
        )


def require_active(space: EmbeddingSpaceDef) -> EmbeddingSpaceDef:
    """Return the space, refusing a write to one that has been retired (FR-3, BR-5).

    A retired space stays readable forever; only the write door closes, and it closes with the
    typed error the caller can act on by writing into the successor space instead.
    """
    if not space.is_active:
        raise GrafxSpaceRetired(
            f"Embedding space {space.name!r} is retired and no longer accepts writes; migrate "
            f"the vector into an active space.",
            field="state",
            space=space.name,
            value=space.state,
        )
    return space


def require_space_identity(
    space: EmbeddingSpaceDef, observed_space_ref: int, *, origin: str
) -> None:
    """Refuse a comparison that would cross two embedding spaces (FR-2, BR-1).

    The refusal happens before any distance is computed, and no partial ranking is ever handed
    back: two spaces produce coordinates in different geometries, so a number computed across
    them would be meaningless rather than merely imprecise.
    """
    if observed_space_ref != space.space_id:
        raise GrafxEmbeddingSpaceMismatch(
            f"This search declares embedding space {space.name!r} (id {space.space_id}), but the "
            f"{origin} belongs to embedding space id {observed_space_ref}; vectors of two spaces "
            f"are never comparable.",
            field="space_ref",
            origin=origin,
            space=space.name,
            expected=space.space_id,
            value=observed_space_ref,
        )


def vector_of(space: EmbeddingSpaceDef, components: tuple[float, ...]) -> VectorValue:
    """Return the storable value for these components in this space.

    The components are taken as already validated: this is the last step of a write, not another
    door, and duplicating the validation here would make the real door untestable (A67).
    """
    return VectorValue(
        values=components, space_ref=space.space_id, dtype=space.storage_dtype
    )
