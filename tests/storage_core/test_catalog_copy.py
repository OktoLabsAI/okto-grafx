"""A working copy of the catalog is structural: shared immutable definitions, private dictionaries.

DDL used to obtain the copy every statement works on by decoding the catalog's own serialized
bytes.  That validated nothing the installers had not already validated and cost
O(tables x columns) per statement -- O(statements^2) over one schema transaction.  These tests
pin what the copy must still guarantee: the same state, the same bytes, isolation of every
sanctioned mutation in both directions, and the historical round trip for subclasses.
"""

from __future__ import annotations

import pytest

from okto_grafx.domain.model import catalog as catalog_module
from okto_grafx.domain.model.catalog import Catalog

from .test_catalog import space, table
from .test_catalog_v2 import _custom, _identity, _schema_catalog


def _populated() -> Catalog:
    catalog = Catalog()
    catalog.add_space(space())
    catalog.add_table(table())
    return catalog


def test_copy_holds_the_same_state_and_shares_the_validated_image() -> None:
    original = _populated()
    image = original.serialize()

    clone = original.copy()

    assert clone is not original
    assert type(clone) is Catalog
    assert clone == original
    assert clone.serialize() is image
    assert Catalog.deserialize(image) == clone
    assert clone.table("Person") is original.table("Person")
    assert clone.space("minilm") is original.space("minilm")
    assert clone.tables() == original.tables()
    assert clone.spaces() == original.spaces()
    assert clone.next_table_id() == original.next_table_id()
    assert clone.next_space_id() == original.next_space_id()


def test_copy_costs_no_encoding_or_decoding(monkeypatch: pytest.MonkeyPatch) -> None:
    original = _populated()
    original.serialize()
    encodes = 0
    decodes = 0
    original_encode = catalog_module._encode_table
    original_decode = catalog_module._decode_table

    def counted_encode(*args: object, **kwargs: object) -> bytes:
        nonlocal encodes
        encodes += 1
        return original_encode(*args, **kwargs)

    def counted_decode(*args: object, **kwargs: object) -> object:
        nonlocal decodes
        decodes += 1
        return original_decode(*args, **kwargs)

    monkeypatch.setattr(catalog_module, "_encode_table", counted_encode)
    monkeypatch.setattr(catalog_module, "_decode_table", counted_decode)

    clone = original.copy()

    assert clone == original
    assert (encodes, decodes) == (0, 0)
    Catalog.deserialize(original.serialize())
    assert (encodes, decodes) == (0, 1)


def test_mutating_either_side_never_reaches_the_other() -> None:
    original = _populated()
    image = original.serialize()
    clone = original.copy()

    clone.add_table(table("Company", 2))
    clone.add_space(space("other", 2))
    clone.retire_space("minilm")

    assert not original.has_table("Company")
    assert not original.has_space("other")
    assert original.space("minilm") == space()
    assert original.serialize() is image
    assert clone.has_table("Company")
    assert clone.has_space("other")
    assert clone.space("minilm") != space()
    assert clone.serialize() != image
    assert clone != original

    original.add_table(table("Later", 3))

    assert not clone.has_table("Later")
    assert clone.next_table_id() == 3


def test_index_authority_installed_on_the_copy_stays_on_the_copy() -> None:
    original = _schema_catalog()
    original.upgrade_index_catalog((_identity(),))
    image = original.serialize()
    clone = original.copy()

    clone.add_index_definition(_custom())

    assert clone.has_index_definition("by_email")
    assert not original.has_index_definition("by_email")
    assert clone.index_definition("rid_t_00000001") is original.index_definition(
        "rid_t_00000001"
    )
    assert original.serialize() is image
    assert clone.serialize() != image
    assert original.format_version == clone.format_version
    assert original.required_capabilities() == clone.required_capabilities()
    assert Catalog.deserialize(clone.serialize()) == clone


def test_copy_taken_before_any_image_serializes_afresh_and_identically() -> None:
    original = _populated()
    clone = original.copy()

    assert clone._serialized_memo is None  # noqa: SLF001
    assert clone.serialize() == original.serialize()
    assert clone.serialize() is not original.serialize()


def test_a_subclass_keeps_the_serialized_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Observing(Catalog):
        pass

    observing = _Observing()
    observing.add_table(table())
    round_trips = 0
    original_deserialize = Catalog.deserialize.__func__

    def counted_deserialize(cls: type[Catalog], raw: bytes) -> Catalog:
        nonlocal round_trips
        round_trips += 1
        return original_deserialize(cls, raw)

    monkeypatch.setattr(Catalog, "deserialize", classmethod(counted_deserialize))

    clone = observing.copy()

    assert round_trips == 1
    assert type(clone) is Catalog
    assert clone == observing
    assert clone.table("Person") == table()
