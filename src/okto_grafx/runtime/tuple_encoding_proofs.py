"""Weak-lifetime synchronized proof transport for one database participant."""

from __future__ import annotations

from threading import Lock
from weakref import WeakKeyDictionary

from okto_grafx.domain.model.schema import TupleEncodingProofs, _tuple_encoding_proof_protocol


def new_tuple_encoding_proofs() -> TupleEncodingProofs:
    """Allocate a private registry; encoding and heap I/O never run under its guard."""
    return _tuple_encoding_proof_protocol(WeakKeyDictionary(), Lock())
