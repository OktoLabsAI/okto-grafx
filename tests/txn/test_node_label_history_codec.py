"""Independent label-history framing, retention and native catalog authority."""

from dataclasses import replace
import struct

import pytest

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxCorruptionDetected
from okto_grafx.domain.model.catalog import Catalog, _encode_table
from okto_grafx.domain.model.node_labels import encode_node_labels
from okto_grafx.domain.model.schema import ColumnDef, TableDef, encode_tuple
from okto_grafx.domain.model.value import ValueType
from okto_grafx.engine.system_history_store import HistoryChange, _decode_change, validate_history_model


def node():
    return TableDef(1, "N", "node", (ColumnDef("id", ValueType.INT64),), extra_node_labels=("B", "λ"))


@pytest.mark.parametrize("labels", [None, (), ("N",), ("B", "N"), ("λ",)])
@pytest.mark.parametrize("operation", [1, 2])
def test_exact_native_label_history_frame(labels, operation):
    table = node()
    change = HistoryChange(table, 7, operation, (42,), node_labels=labels)
    schema = _encode_table(table) + b"\0\0GXHM03" + struct.pack("<BH", 8 if labels is not None else 0, 0)
    schema += encode_node_labels(("B", "λ"))
    payload = (b"" if labels is None else encode_node_labels(labels)) + encode_tuple(table, (42,))
    expected = struct.pack("<BQII", operation, 7, len(schema), len(payload)) + schema + payload
    assert change.encode() == expected
    assert _decode_change(expected) == change


@pytest.mark.parametrize("labels", [[], ["N"], ("N", "B"), ("Missing",), ("",)])
def test_history_never_normalizes_or_grants_unadmitted_membership(labels):
    with pytest.raises(GrafxConfigurationError):
        HistoryChange(node(), 7, 1, (42,), node_labels=labels).encode()


@pytest.mark.parametrize("operation", [3, 4, 5, 6])
def test_delete_schema_and_redacted_history_never_embed_label_payload(operation):
    with pytest.raises(GrafxConfigurationError):
        HistoryChange(node(), 0 if operation == 4 else 7, operation, (), node_labels=()).encode()


def test_explicit_empty_membership_roundtrips_without_extra_candidates():
    table = replace(node(), extra_node_labels=())
    original = HistoryChange(table, 7, 1, (42,), node_labels=())
    assert b"GXHM03" in original.encode()
    assert _decode_change(original.encode()) == original
    implicit = replace(original, node_labels=None)
    assert b"GXHM03" not in implicit.encode()
    assert _decode_change(implicit.encode()) == implicit


@pytest.mark.parametrize("labels", [(), ("N",), ("B", "N")])
def test_redacted_labels_keep_framing_without_retaining_their_payload(labels):
    table = node() if "B" in labels else replace(node(), extra_node_labels=())
    original = HistoryChange(table, 7, 1, (42,), node_labels=labels)
    redacted = replace(original, operation=5, values=(), node_labels=None, redacted_node_labels=True,
                       redacted_bytes=len(encode_tuple(table, (42,))) + len(encode_node_labels(labels)))
    assert len(redacted.encode()) == len(original.encode())
    decoded = _decode_change(redacted.encode())
    assert decoded == redacted and decoded.node_labels is None
    assert decoded.redacted_node_labels is True
    assert _decode_change(replace(redacted, redacted_bytes=0).encode()).redacted_node_labels is True


@pytest.mark.parametrize("operation", [1, 2, 3, 4])
def test_only_redacted_operations_may_claim_erased_membership(operation):
    with pytest.raises(GrafxConfigurationError):
        HistoryChange(node(), 0 if operation == 4 else 7, operation,
                      (42,) if operation in (1,2) else (), redacted_node_labels=True).encode()


def test_history_membership_needs_native_catalog_capability_and_admission():
    catalog = Catalog()
    catalog.upgrade_index_catalog(())
    table = replace(node(), extra_node_labels=())
    catalog.add_table(table)
    empty = HistoryChange(table, 7, 1, (42,), node_labels=())
    with pytest.raises(GrafxCorruptionDetected):
        validate_history_model(empty, catalog)
    catalog.extend_node_labels(1, ("B", "λ"))
    validate_history_model(empty, catalog)
    validate_history_model(HistoryChange(node(), 7, 1, (42,), node_labels=("B",)), catalog)
    forged = replace(node(), extra_node_labels=("Unauthorized",))
    with pytest.raises(GrafxCorruptionDetected):
        validate_history_model(HistoryChange(forged, 7, 1, (42,), node_labels=("Unauthorized",)), catalog)


@pytest.mark.parametrize("mutation", ["marker", "flag", "prefix", "trailing", "relationship"])
def test_self_consistent_lengths_do_not_authorize_malformed_label_frames(mutation):
    raw = bytearray(HistoryChange(node(), 7, 1, (42,), node_labels=("B", "N")).encode())
    model = raw.index(b"GXHM03")
    if mutation == "marker":
        raw[model:model+6] = b"GXHM02"
    elif mutation == "flag":
        raw[model+6] = 0
    elif mutation == "prefix":
        raw[raw.rindex(b"GXL1")] = ord("X")
    elif mutation == "trailing":
        raw.append(0)
        struct.pack_into("<I", raw, 13, struct.unpack_from("<I", raw, 13)[0]+1)
    else:
        edge = TableDef(2,"R","rel",(),from_table="N",to_table="N")
        with pytest.raises(GrafxConfigurationError):
            HistoryChange(edge, 7, 1, (1,1), node_labels=()).encode()
        return
    with pytest.raises(GrafxCorruptionDetected):
        _decode_change(bytes(raw))
