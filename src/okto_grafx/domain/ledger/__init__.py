"""The unapplied-work ledger: what recovery lost, why, and whether it can be put back.

CONTRACT.md section 6.6 freezes the entry format and SPEC-M1 FR-9 states what the ledger is for.
G8 states the invariant this package exists to make provable: **every discard leaves a trace**.
"""

from __future__ import annotations

from okto_grafx.domain.ledger.classification import (
    FORENSIC_REASONS,
    classify_failure,
    classify_record,
    refuses_recovery,
)
from okto_grafx.domain.ledger.entry import (
    DIGEST_LENGTH,
    LEDGER_ENTRY_HEADER_LENGTH,
    LEDGER_FORMAT_VERSION,
    LEDGER_MAGIC,
    MAX_LEDGER_PAYLOAD_BYTES,
    LedgerEntry,
    LedgerEntryType,
    LedgerOriginClass,
    LedgerReason,
    digest_of,
)
from okto_grafx.domain.ledger.payload import (
    MAX_ENVELOPE_HEADER_BYTES,
    LedgerPayload,
    decode_payload,
    encode_payload,
)
from okto_grafx.domain.ledger.textform import (
    MAX_TEXT_FORM_BYTES,
    decode_fields,
    encode_fields,
)

__all__ = [
    "DIGEST_LENGTH",
    "FORENSIC_REASONS",
    "LEDGER_ENTRY_HEADER_LENGTH",
    "LEDGER_FORMAT_VERSION",
    "LEDGER_MAGIC",
    "MAX_ENVELOPE_HEADER_BYTES",
    "MAX_LEDGER_PAYLOAD_BYTES",
    "MAX_TEXT_FORM_BYTES",
    "LedgerEntry",
    "LedgerEntryType",
    "LedgerOriginClass",
    "LedgerPayload",
    "LedgerReason",
    "classify_failure",
    "classify_record",
    "decode_fields",
    "decode_payload",
    "digest_of",
    "encode_fields",
    "encode_payload",
    "refuses_recovery",
]
