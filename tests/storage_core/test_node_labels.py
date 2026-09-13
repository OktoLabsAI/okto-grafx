"""Independent byte oracles for versioned label membership; no query fixture adaptation."""

import struct

import pytest

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxCorruptionDetected
from okto_grafx.domain.model.node_labels import (
    MAX_NODE_LABEL_BYTES, MAX_NODE_LABEL_COUNT, decode_node_labels,
    encode_node_labels, normalize_node_labels, validate_node_labels,
)


def frame(*names):
    return b"GXL1" + struct.pack("<H", len(names)) + b"".join(struct.pack("<H", len(n)) + n for n in names)


@pytest.mark.parametrize("names", [(), ("A",), ("A", "B"), ("A", "a"), ("two words", "é", "漢"), ("\0", "\n")])
def test_exact_wire_and_following_tuple_offset(names):
    expected = frame(*(n.encode("utf-8") for n in names))
    assert encode_node_labels(names) == expected
    assert decode_node_labels(b"pre" + expected + b"property payload", 3) == (names, 3 + len(expected))


def test_owned_set_operations_are_idempotent_and_case_sensitive():
    incoming = ["B", "A", "B", "a"]
    result = normalize_node_labels(incoming)
    incoming.clear()
    assert result == ("A", "B", "a")
    assert validate_node_labels(result) is result
    assert normalize_node_labels([*result, *result]) == result


@pytest.mark.parametrize("bad", [None, "A", {"A"}, {"A": 1}, iter(["A"]), [1], [True], [""], ["\ud800"]])
def test_assignment_refuses_unknown_host_shapes_and_invalid_text(bad):
    with pytest.raises(GrafxConfigurationError):
        normalize_node_labels(bad)


@pytest.mark.parametrize("bad", [["A"], ("B", "A"), ("A", "A")])
def test_encoder_never_repairs_forged_noncanonical_state(bad):
    with pytest.raises(GrafxConfigurationError):
        encode_node_labels(bad)


@pytest.mark.parametrize("raw", [b"", b"GXL0\0\0", b"GXL1\xff\xff", frame(b""), frame(b"A", b"A"),
                                  frame(b"B", b"A"), frame(b"\xff"), frame(b"\xed\xa0\x80"),
                                  frame(b"A")[:-1], b"GXL1\x01\x00\xff\xffa"])
def test_malformed_stored_sets_are_corruption_not_repaired_values(raw):
    with pytest.raises(GrafxCorruptionDetected):
        decode_node_labels(raw)


@pytest.mark.parametrize("offset", [-1, True, 1.5, 999])
def test_bad_offsets_are_typed_refusals(offset):
    with pytest.raises(GrafxCorruptionDetected):
        decode_node_labels(frame(b"A"), offset)


def test_exact_total_byte_boundary_and_multibyte_budget():
    names = ("a" * (MAX_NODE_LABEL_BYTES - 8),)
    assert len(encode_node_labels(names)) == MAX_NODE_LABEL_BYTES
    assert decode_node_labels(encode_node_labels(names)) == (names, MAX_NODE_LABEL_BYTES)
    with pytest.raises(GrafxConfigurationError):
        normalize_node_labels([names[0] + "a"])
    with pytest.raises(GrafxConfigurationError):
        normalize_node_labels(["漢" * (MAX_NODE_LABEL_BYTES // 3)])
    with pytest.raises(GrafxConfigurationError):
        normalize_node_labels(["A"] * (MAX_NODE_LABEL_COUNT + 1))
    with pytest.raises(GrafxCorruptionDetected):
        decode_node_labels(frame(("漢" * (MAX_NODE_LABEL_BYTES // 3)).encode("utf-8")))
