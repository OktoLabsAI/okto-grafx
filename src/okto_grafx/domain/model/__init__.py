"""The domain model of Okto Grafx: values, schema, catalog and heap records.

This package answers what a stored thing is, never where it is kept. Values know how to encode
themselves, a table knows what a row of it must look like, the catalog knows which tables and
embedding spaces exist, and a record header knows when a version began and when it ended. The
placement of those bytes on a page belongs to okto_grafx.domain.page, and the reading and writing
of them belongs to the engine.
"""

from __future__ import annotations

from okto_grafx.domain.model.catalog import (
    CATALOG_FORMAT_VERSION,
    CATALOG_MAGIC,
    Catalog,
)
from okto_grafx.domain.model.errors import SchemaMismatchError
from okto_grafx.domain.model.record import (
    NO_PREVIOUS_VERSION,
    OVERFLOW_POINTER_SIZE,
    RECORD_FLAG_DELETED,
    RECORD_FLAG_HAS_OVERFLOW,
    RECORD_HEADER_SIZE,
    HeapVersion,
    RecordHeader,
    decode_overflow_pointer,
    encode_overflow_pointer,
)
from okto_grafx.domain.model.schema import (
    MAX_IDENTIFIER_LENGTH,
    SPACE_STATE_ACTIVE,
    SPACE_STATE_RETIRED,
    SPACE_STATES,
    STORAGE_DTYPES,
    TABLE_KINDS,
    ColumnDef,
    EmbeddingSpaceDef,
    TableDef,
    decode_tuple,
    encode_tuple,
    is_identifier,
)
from okto_grafx.domain.model.value import (
    INT64_MAX,
    INT64_MIN,
    MAX_STRING_LENGTH,
    MAX_VECTOR_DIMENSION,
    VECTOR_DTYPES,
    VECTOR_VALUE_TYPES,
    Timestamp,
    Uuid,
    Value,
    ValueType,
    VectorValue,
    decode_value,
    decode_values,
    encode_value,
    encode_values,
    value_type_of,
)

__all__ = [
    "CATALOG_FORMAT_VERSION",
    "CATALOG_MAGIC",
    "INT64_MAX",
    "INT64_MIN",
    "MAX_IDENTIFIER_LENGTH",
    "MAX_STRING_LENGTH",
    "MAX_VECTOR_DIMENSION",
    "NO_PREVIOUS_VERSION",
    "OVERFLOW_POINTER_SIZE",
    "RECORD_FLAG_DELETED",
    "RECORD_FLAG_HAS_OVERFLOW",
    "RECORD_HEADER_SIZE",
    "SPACE_STATES",
    "SPACE_STATE_ACTIVE",
    "SPACE_STATE_RETIRED",
    "STORAGE_DTYPES",
    "TABLE_KINDS",
    "VECTOR_DTYPES",
    "VECTOR_VALUE_TYPES",
    "Catalog",
    "ColumnDef",
    "EmbeddingSpaceDef",
    "HeapVersion",
    "RecordHeader",
    "SchemaMismatchError",
    "TableDef",
    "Timestamp",
    "Uuid",
    "Value",
    "ValueType",
    "VectorValue",
    "decode_overflow_pointer",
    "decode_tuple",
    "decode_value",
    "decode_values",
    "encode_overflow_pointer",
    "encode_tuple",
    "encode_value",
    "encode_values",
    "is_identifier",
    "value_type_of",
]
