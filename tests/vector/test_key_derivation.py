"""The vector index key is a pinned digest (CF-9, SPEC-VEC FR-6, amendments A56 and A68).

Three things about this key are FORMAT, and a stored index file means something different if any
of them changes: the name the derivation travels under, the size of a key, and the bytes the
digest produces for a given vector. All three are pinned by value here, because the consequence
of changing one silently is an index that opens and answers wrongly.

The derivation exists because the framework's default -- key equals the encoded column -- makes
the key as large as the value, and an entry is never split across a page. The fourth test states
that in numbers rather than leaving it as a memory.
"""

from __future__ import annotations

import hashlib

import pytest

from okto_grafx.domain.errors import GrafxIndexError
from okto_grafx.domain.index.definition import COLUMN_KEY_DERIVATION, IndexDefinition
from okto_grafx.domain.index.entry import INDEX_ENTRY_HEADER_SIZE
from okto_grafx.domain.index.visibility import IndexVisibility
from okto_grafx.domain.model.value import VectorValue, encode_value
from okto_grafx.domain.page.layout import PAGE_HEADER_SIZE, SLOT_ENTRY_SIZE
from okto_grafx.domain.vector.key import (
    VECTOR_DIGEST_DERIVATION,
    VECTOR_KEY_SIZE,
    VectorIndexDefinition,
    vector_digest,
)

from .conftest import SnapshotDouble, TransactionDouble, VectorFixture


def _definition() -> VectorIndexDefinition:
    """Return a vector index definition over the third column of one table."""
    return VectorIndexDefinition(
        name="vector_Chunk_space",
        table_id=1,
        table_name="Chunk",
        positions=(2,),
        visibility=IndexVisibility.PROXIMITY,
    )


# --- the format, pinned -------------------------------------------------------------------------


def test_the_derivation_name_is_the_literal_the_module_declares() -> None:
    """The name travels in the definition digest, so changing it invalidates every stored file."""
    assert VECTOR_DIGEST_DERIVATION == "vector_digest_v1"
    assert VECTOR_DIGEST_DERIVATION != COLUMN_KEY_DERIVATION


def test_a_key_is_thirty_two_bytes_whatever_the_dimension() -> None:
    """The whole point of the derivation: the key stops depending on the embedding model."""
    assert VECTOR_KEY_SIZE == 32
    for dimension in (1, 384, 4096, 16384):
        components = tuple(0.5 for _ in range(dimension))
        for dtype in ("float32", "float64"):
            key = vector_digest(VectorValue(components, 1, dtype))
            assert len(key) == VECTOR_KEY_SIZE, (dimension, dtype)


def test_the_digest_is_the_sha256_of_the_bytes_a_page_holds() -> None:
    """Pinned by value: a different input or a different algorithm is a different format."""
    value = VectorValue((0.5, 0.25), 1, "float64")
    assert vector_digest(value) == hashlib.sha256(encode_value(value)).digest()
    assert vector_digest(value).hex().startswith("f53880a4e22f3524")


def test_the_column_derivation_would_have_capped_the_dimension_by_page_size() -> None:
    """The measurement the derivation exists for, kept as a test rather than as a memory.

    An entry occupies one slot on one page and is never split, so a key equal to the encoded
    vector caps the storable dimension at a function of ``page_size``. A digest fits every page
    size the contract allows, with room to spare.
    """
    encoded = len(encode_value(VectorValue(tuple(0.5 for _ in range(384)), 1, "float64")))
    assert encoded == 3081
    for page_size, fits in ((512, False), (4096, True), (8192, True)):
        budget = page_size - PAGE_HEADER_SIZE - SLOT_ENTRY_SIZE - INDEX_ENTRY_HEADER_SIZE
        assert (encoded <= budget) is fits, page_size
        assert VECTOR_KEY_SIZE < budget, page_size


# --- the derivation is declared, and the framework consumes the declaration -----------------------


def test_the_definition_declares_its_derivation() -> None:
    """A definition that named a derivation and did not implement it would key silently wrong."""
    definition = _definition()
    assert definition.key_derivation == VECTOR_DIGEST_DERIVATION
    assert isinstance(definition, IndexDefinition)


def test_the_definition_keys_the_vector_column_of_a_row() -> None:
    """The framework's verifier re-derives a key this way, so the two must agree exactly."""
    vector = VectorValue((1.0, 2.0), 3, "float32")
    assert _definition().key_for((7, 0, vector)) == vector_digest(vector)


def test_a_row_too_short_for_the_keyed_column_is_refused() -> None:
    """A row that cannot supply the column is a caller error, never an IndexError."""
    with pytest.raises(GrafxIndexError) as failure:
        _definition().key_for((1, 2))
    assert failure.value.details["field"] == "positions"


def test_a_column_that_is_not_a_vector_is_refused() -> None:
    """The digest covers an embedding; anything else is a definition pointed at a wrong column."""
    with pytest.raises(GrafxIndexError) as failure:
        _definition().key_for((1, 2, "not a vector"))
    assert failure.value.details["field"] == "key"


def test_the_derivation_travels_in_the_definition_digest() -> None:
    """A file written under one derivation must never be opened under another."""
    theirs = IndexDefinition(
        name="vector_Chunk_space",
        table_id=1,
        table_name="Chunk",
        positions=(2,),
        visibility=IndexVisibility.PROXIMITY,
    )
    assert _definition().digest() != theirs.digest()


# --- the drift detector the digest preserves ------------------------------------------------------


def test_a_changed_vector_digests_differently() -> None:
    """The detector the column derivation gave for free, kept at constant key size."""
    first = VectorValue((1.0, 2.0), 1, "float64")
    assert vector_digest(first) != vector_digest(VectorValue((1.0, 2.5), 1, "float64"))
    assert vector_digest(first) != vector_digest(VectorValue((1.0, 2.0), 2, "float64"))
    assert vector_digest(first) != vector_digest(VectorValue((1.0, 2.0, 0.0), 1, "float64"))


def test_a_vector_that_drifted_from_its_row_is_reported_by_the_registry(
    database: VectorFixture,
) -> None:
    """End to end: the framework re-derives the key and names the entry that no longer matches."""
    space = database.create_space("space", 3)
    table = database.create_table("Chunk", "space")
    ref = database.insert_row(table, 1, 0, space, (1.0, 0.0, 0.0), csn=10)
    assert database.registry.verify() == ()
    index = database.engine.index("space")
    txn = TransactionDouble()
    index.stage_insert(txn, index.key_for((9.0, 9.0, 9.0)), ref, 20)
    index.commit(txn, 20)
    findings = database.registry.verify()
    assert findings
    assert any("divergence" in finding.kind for finding in findings)
    assert table.name == "Chunk" and space.name == "space"


# --- the shape check that replaced the decode door (LESSONS L14) -----------------------------------


def test_a_hand_built_key_of_the_wrong_size_is_refused_before_a_record_exists(
    database: VectorFixture,
) -> None:
    """Built by hand, from raw bytes, exactly as the name says (LESSONS L14).

    The old key WAS the encoded vector, so a caller could assemble one carrying a NaN and it
    reached a loggable record because validation sat only on the encoder. The digest removes the
    door rather than guarding it -- no components travel in a key any more -- and what is left to
    check is the shape, which is checked before a record exists.
    """
    space = database.create_space("space", 3)
    table = database.create_table("Chunk", "space")
    ref = database.insert_row(table, 1, 0, space, (1.0, 0.0, 0.0), csn=10)
    index = database.engine.index("space")
    before = len(index.walk())
    txn = TransactionDouble()
    for forged in (b"", b"\x00" * 31, b"\xff" * 33, bytes(range(16))):
        with pytest.raises(GrafxIndexError) as failure:
            index.stage_insert(txn, forged, ref, 20)
        assert failure.value.details["field"] == "key"
        assert failure.value.details["expected"] == VECTOR_KEY_SIZE
    assert txn.records == []
    assert len(index.walk()) == before
    assert space.name == "space" and table.name == "Chunk"


def test_a_hand_built_key_of_the_right_size_is_caught_by_the_drift_detector(
    database: VectorFixture,
) -> None:
    """A digest cannot be checked for meaning, so the honest boundary is stated as a test.

    Thirty-two arbitrary bytes are indistinguishable from a real digest at staging time. What
    catches them is the detector: the key matches no row, so the walk reports the entry rather
    than the index answering as though it were healthy.
    """
    space = database.create_space("space", 3)
    table = database.create_table("Chunk", "space")
    ref = database.insert_row(table, 1, 0, space, (1.0, 0.0, 0.0), csn=10)
    index = database.engine.index("space")
    txn = TransactionDouble()
    index.stage_insert(txn, bytes(range(32)), ref, 20)
    index.commit(txn, 20)
    assert database.registry.verify()
    result = database.engine.search(
        space="space", query=(1.0, 0.0, 0.0), k=1, snapshot=SnapshotDouble(1000)
    )
    assert result.achieved_k >= 1
    assert space.name == "space" and table.name == "Chunk"


def test_the_tombstone_door_checks_the_key_shape_as_well(
    database: VectorFixture,
) -> None:
    """Both staging doors take a key from the caller, so both check it (amendment A66).

    A guard on ``stage_insert`` alone says nothing about ``stage_delete``: they are two paths
    that maintain one invariant, and the battery found the second one uncovered by reverting it
    and watching the suite stay green.
    """
    space = database.create_space("space", 3)
    table = database.create_table("Chunk", "space")
    ref = database.insert_row(table, 1, 0, space, (1.0, 0.0, 0.0), csn=10)
    index = database.engine.index("space")
    before = len(index.walk())
    txn = TransactionDouble()
    for forged in (b"", b"\x00" * 31, b"\xff" * 33):
        with pytest.raises(GrafxIndexError) as failure:
            index.stage_delete(txn, forged, ref, 20)
        assert failure.value.details["field"] == "key"
        assert failure.value.details["expected"] == VECTOR_KEY_SIZE
    assert txn.records == []
    assert len(index.walk()) == before
    assert space.name == "space" and table.name == "Chunk"
