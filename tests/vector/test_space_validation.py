"""The vector write door (SPEC-VEC FR-1, BR-5, AC-1, VTS-1).

Everything a vector write can be refused for is refused here, before the caller holds anything
to persist. Two properties are asserted for each refusal:

* the error is the typed one the taxonomy names, with a ``reason`` detail that says WHICH check
  answered -- so a test can tell a finiteness refusal from a range refusal even though both are
  ``GrafxVectorValidationError`` (amendment A62);
* nothing changed. The bytes of the device and the contents of the index are compared before and
  after, which is what AC-1 means by heap, log and index unchanged.

The order of the checks has a history in this build: a range check written first hides the fact
that nothing asked whether the component was a number, and a NaN then became persistable in a
float64 space with a green suite (amendment A34). Each check therefore has a case only it can
refuse, and those two cases are the first tests in this module.
"""

from __future__ import annotations

import hashlib

import pytest

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxSpaceRetired,
    GrafxVectorValidationError,
)
from okto_grafx.domain.model.value import FLOAT32_OVERFLOW_THRESHOLD, MAX_FLOAT32
from okto_grafx.domain.ports.vectormath import DistanceMetric
from okto_grafx.domain.vector.space import (
    NORMALIZED_NORM_TOLERANCE,
    round_to_storage_dtype,
    validate_components,
)

from .conftest import TransactionDouble, VectorFixture, unit


def _digest(database: VectorFixture) -> str:
    """Return a digest of every byte the device holds, so a refusal can be shown to be inert."""
    hasher = hashlib.sha256()
    for name in sorted(database.device.list_files()):
        hasher.update(name.encode("ascii"))
        for index in range(database.device.page_count(name)):
            hasher.update(database.device.read_page(name, index))
        hasher.update(database.device.read_log(name, 0, database.device.log_size(name)))
    return hasher.hexdigest()


# --- the two checks that can hide each other ----------------------------------------------


def test_a_nan_is_refused_in_a_float64_space_where_no_range_check_could_answer(
    database: VectorFixture,
) -> None:
    """The finiteness check is the only guard a float64 space reaches; A34 was exactly this."""
    space = database.create_space("doubles", 3, storage_dtype="float64")
    with pytest.raises(GrafxVectorValidationError) as failure:
        database.engine.validate_vector(space, (1.0, float("nan"), 3.0))
    assert failure.value.details["reason"] == "non_finite"
    assert failure.value.details["position"] == 1


def test_an_oversized_component_is_refused_in_a_float32_space_by_the_range_check_alone(
    database: VectorFixture,
) -> None:
    """A finite component a float32 page could only hold as an infinity reaches only this guard."""
    space = database.create_space("singles", 3, storage_dtype="float32")
    with pytest.raises(GrafxVectorValidationError) as failure:
        database.engine.validate_vector(space, (1.0, 1e300, 3.0))
    assert failure.value.details["reason"] == "float32_range"
    assert failure.value.details["limit"] == MAX_FLOAT32


def test_the_range_check_boundary_is_the_value_that_rounds_to_infinity(
    database: VectorFixture,
) -> None:
    """The threshold is where a double stops fitting, not where a float32 stops being exact."""
    space = database.create_space("edge", 1, storage_dtype="float32")
    assert database.engine.validate_vector(space, (MAX_FLOAT32,)) == (MAX_FLOAT32,)
    with pytest.raises(GrafxVectorValidationError):
        database.engine.validate_vector(space, (FLOAT32_OVERFLOW_THRESHOLD,))


@pytest.mark.parametrize(
    ("label", "component"),
    [("nan", float("nan")), ("inf", float("inf")), ("negative_inf", float("-inf"))],
)
@pytest.mark.parametrize("dtype", ["float32", "float64"])
def test_no_dtype_accepts_a_non_finite_component(
    database: VectorFixture, label: str, component: float, dtype: str
) -> None:
    """Every combination of the two guards refuses every shape of a non-finite number."""
    space = database.create_space(f"space_{dtype}_{label}", 2, storage_dtype=dtype)
    with pytest.raises(GrafxVectorValidationError) as failure:
        database.engine.validate_vector(space, (0.5, component))
    assert failure.value.details["reason"] == "non_finite"


# --- dimension --------------------------------------------------------------------------


@pytest.mark.parametrize("dimension", [2, 4])
def test_a_vector_of_the_wrong_dimension_is_refused(
    database: VectorFixture, dimension: int
) -> None:
    """The dimension of the space is the one a write must match, short or long."""
    space = database.create_space("sized", 3)
    with pytest.raises(GrafxVectorValidationError) as failure:
        database.engine.validate_vector(space, tuple(0.1 for _ in range(dimension)))
    assert failure.value.details["reason"] == "dimension_mismatch"
    assert failure.value.details["expected"] == 3
    assert failure.value.details["value"] == dimension


def test_something_that_is_not_a_sequence_of_numbers_is_refused(
    database: VectorFixture,
) -> None:
    """A string has a length and would otherwise pass the dimension check by accident."""
    space = database.create_space("texty", 3)
    with pytest.raises(GrafxVectorValidationError) as failure:
        database.engine.validate_vector(space, "abc")  # type: ignore[arg-type]
    assert failure.value.details["reason"] == "not_a_sequence"


def test_a_sequence_holding_something_that_is_not_a_number_is_refused(
    database: VectorFixture,
) -> None:
    """A conversion failure inside the sequence is a caller error, never a stray TypeError."""
    space = database.create_space("mixed", 2)
    with pytest.raises(GrafxVectorValidationError) as failure:
        database.engine.validate_vector(space, (1.0, object()))  # type: ignore[arg-type]
    assert failure.value.details["reason"] == "not_a_sequence"


# --- storage dtype ------------------------------------------------------------------------


def test_a_float32_space_returns_the_components_a_page_will_read_back(
    database: VectorFixture,
) -> None:
    """The validated tuple is the STORED form, so the index and the heap rank the same number."""
    space = database.create_space("rounded", 2, storage_dtype="float32")
    stored = database.engine.validate_vector(space, (0.1, 0.2))
    assert stored == (round_to_storage_dtype(0.1, "float32"), round_to_storage_dtype(0.2, "float32"))
    assert stored != (0.1, 0.2)


def test_a_float64_space_returns_the_components_untouched(database: VectorFixture) -> None:
    """Double storage holds exactly what the caller supplied, so nothing is rounded."""
    space = database.create_space("exact", 2, storage_dtype="float64")
    assert database.engine.validate_vector(space, (0.1, 0.2)) == (0.1, 0.2)


# --- normalized declaration ---------------------------------------------------------------


def test_a_space_that_declares_normalized_vectors_refuses_one_that_is_not(
    database: VectorFixture,
) -> None:
    """The declaration is checked because a dot-product ranking cannot notice it is false."""
    space = database.create_space("units", 3, normalized=True, storage_dtype="float64")
    with pytest.raises(GrafxVectorValidationError) as failure:
        database.engine.validate_vector(space, (1.0, 1.0, 1.0))
    assert failure.value.details["reason"] == "not_normalized"
    assert failure.value.details["tolerance"] == NORMALIZED_NORM_TOLERANCE


def test_a_unit_vector_passes_a_normalized_space_in_both_storage_dtypes(
    database: VectorFixture,
) -> None:
    """The tolerance clears the drift float32 rounding introduces, which is why it is not zero."""
    for dtype in ("float32", "float64"):
        space = database.create_space(f"unit_{dtype}", 16, normalized=True, storage_dtype=dtype)
        values = unit(tuple(float(index + 1) for index in range(16)))
        assert database.engine.validate_vector(space, values)


def test_a_space_that_does_not_declare_normalized_vectors_accepts_any_length(
    database: VectorFixture,
) -> None:
    """The check is a consequence of the declaration, never a rule of its own."""
    space = database.create_space("free", 3, normalized=False)
    assert database.engine.validate_vector(space, (5.0, 0.0, 0.0)) == (5.0, 0.0, 0.0)


def test_the_normalized_tolerance_is_the_literal_the_module_documents() -> None:
    """The constant is pinned by its value, never by an expression that slides with it (A68)."""
    assert NORMALIZED_NORM_TOLERANCE == 1e-6


# --- retirement ---------------------------------------------------------------------------


def test_a_retired_space_refuses_a_write_that_would_otherwise_be_valid(
    database: VectorFixture,
) -> None:
    """Retirement closes the write door and nothing else (FR-3, AC-4)."""
    space = database.create_space("old", 3)
    assert database.engine.validate_vector(space, (1.0, 2.0, 3.0))
    database.engine.retire_space("old")
    retired = database.engine.space("old")
    with pytest.raises(GrafxSpaceRetired) as failure:
        database.engine.validate_vector(retired, (1.0, 2.0, 3.0))
    assert failure.value.code == "space_retired"
    assert failure.value.details["space"] == "old"


def test_the_retirement_refusal_is_not_the_validation_refusal(
    database: VectorFixture,
) -> None:
    """A retired space refuses a valid vector and an invalid one for different reasons."""
    database.create_space("closing", 3)
    database.engine.retire_space("closing")
    retired = database.engine.space("closing")
    with pytest.raises(GrafxSpaceRetired):
        database.engine.validate_vector(retired, (1.0, 2.0, 3.0))
    with pytest.raises(GrafxSpaceRetired):
        database.engine.validate_vector(retired, (float("nan"), 2.0, 3.0))


def test_validate_vector_refuses_something_that_is_not_a_space(
    database: VectorFixture,
) -> None:
    """A caller argument of the wrong type is a configuration error, never a corruption."""
    with pytest.raises(GrafxConfigurationError) as failure:
        database.engine.validate_vector("not a space", (1.0,))  # type: ignore[arg-type]
    assert failure.value.details["field"] == "space"


# --- nothing is persisted -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "reason"),
    [
        ((0.1, 0.2), "dimension_mismatch"),
        ((0.1, float("nan"), 0.3), "non_finite"),
        ((0.1, float("inf"), 0.3), "non_finite"),
        ((0.1, 1e300, 0.3), "float32_range"),
    ],
)
def test_a_refused_write_leaves_the_device_and_the_index_byte_identical(
    database: VectorFixture, values: tuple[float, ...], reason: str
) -> None:
    """AC-1: every refusal is inert, proved by a digest of the device taken on both sides."""
    space = database.create_space("guarded", 3, storage_dtype="float32")
    table = database.create_table("Chunk", "guarded")
    database.insert_row(table, 1, 0, space, (0.5, 0.5, 0.5), csn=10)
    before = _digest(database)
    entries_before = database.engine.index("guarded").walk()
    assert table.name == "Chunk"
    with pytest.raises(GrafxVectorValidationError) as failure:
        database.engine.validate_vector(space, values)
    assert failure.value.details["reason"] == reason
    assert _digest(database) == before
    assert database.engine.index("guarded").walk() == entries_before


def test_a_refused_stage_produces_no_log_record_at_all(database: VectorFixture) -> None:
    """The record IS the thing that would be persisted, so a refusal leaves nothing to persist."""
    space = database.create_space("staged", 3)
    table = database.create_table("Chunk", "staged")
    ref = database.insert_row(table, 1, 0, space, (0.5, 0.5, 0.5), csn=10)
    before = _digest(database)
    entries_before = database.engine.index("staged").walk()
    with pytest.raises(GrafxVectorValidationError):
        database.engine.stage_insert(
            "staged", 2, ref, (float("nan"), 0.0, 0.0), 11, TransactionDouble()
        )
    assert _digest(database) == before
    assert database.engine.index("staged").walk() == entries_before


def test_a_stage_into_a_retired_space_produces_no_record(database: VectorFixture) -> None:
    """BR-5 covers the retired space too: no record, so nothing reaches the log."""
    space = database.create_space("sunset", 3)
    table = database.create_table("Chunk", "sunset")
    ref = database.insert_row(table, 1, 0, space, (0.5, 0.5, 0.5), csn=10)
    database.engine.retire_space("sunset")
    entries_before = database.engine.index("sunset").walk()
    with pytest.raises(GrafxSpaceRetired):
        database.engine.stage_insert(
            "sunset", 2, ref, (0.1, 0.2, 0.3), 11, TransactionDouble()
        )
    assert database.engine.index("sunset").walk() == entries_before
    assert space.name == "sunset"


# --- the pure validator, without an engine in the way -------------------------------------


def test_the_validator_refuses_before_it_rounds(database: VectorFixture) -> None:
    """Rounding a NaN would produce a NaN; the refusal has to come first."""
    space = database.create_space("ordered", 2, storage_dtype="float32")
    with pytest.raises(GrafxVectorValidationError) as failure:
        validate_components(space, (float("nan"), 1e300))
    assert failure.value.details["position"] == 0
    assert failure.value.details["reason"] == "non_finite"


def test_the_metric_of_a_space_does_not_change_what_a_write_is_allowed_to_be(
    database: VectorFixture,
) -> None:
    """Validation is about storage, so the same vector passes under all three metrics."""
    for index, metric in enumerate(DistanceMetric):
        space = database.create_space(f"metric_{index}", 3, metric=metric)
        assert database.engine.validate_vector(space, (1.0, 2.0, 3.0)) == (1.0, 2.0, 3.0)


def test_staging_validates_against_the_space_and_not_merely_against_the_key_format(
    database: VectorFixture,
) -> None:
    """The staging door passes the vector through the write door, which knows the space.

    The key encoder refuses a component that is not a number, so a NaN never reaches the log
    whichever door it came through. What only the write door can refuse is a vector that is wrong
    FOR THIS SPACE: the wrong number of components, or a length of something other than one in a
    space that declares normalized vectors. Both are asserted here, because a staging door that
    skipped the write door would still refuse the NaN and would accept these.
    """
    space = database.create_space("units", 4, normalized=True, storage_dtype="float64")
    table = database.create_table("Chunk", "units")
    ref = database.insert_row(table, 1, 0, space, unit((1.0, 1.0, 1.0, 1.0)), csn=10)
    with pytest.raises(GrafxVectorValidationError) as failure:
        database.engine.stage_insert(
            "units", 2, ref, (1.0, 1.0, 1.0, 1.0), 11, TransactionDouble()
        )
    assert failure.value.details["reason"] == "not_normalized"
    with pytest.raises(GrafxVectorValidationError) as short:
        database.engine.stage_insert(
            "units", 2, ref, (1.0, 0.0, 0.0), 11, TransactionDouble()
        )
    assert short.value.details["reason"] == "dimension_mismatch"


def test_a_staged_vector_carries_the_components_a_page_will_read_back(
    database: VectorFixture,
) -> None:
    """The index and the heap must rank one number, so staging stores the rounded form."""
    space = database.create_space("rounded", 2, storage_dtype="float32")
    table = database.create_table("Chunk", "rounded")
    ref = database.insert_row(table, 1, 0, space, (0.1, 0.2), csn=10)
    index = database.engine.index("rounded")
    stored = (
        round_to_storage_dtype(0.1, "float32"),
        round_to_storage_dtype(0.2, "float32"),
    )
    assert index.walk()[0].key == index.key_for(stored)
    version = database.heap.read(ref)
    assert version.values[2].values == stored
