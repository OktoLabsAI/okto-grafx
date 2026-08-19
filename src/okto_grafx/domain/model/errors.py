"""The typed condition the domain model raises when data and schema disagree.

CONTRACT.md section 2 fixes the taxonomy of errors that leave the engine. The condition declared
here is the storage-core answer to "these values are not what this table declares": wrong arity,
wrong column type, a null in a column that forbids one, or a Python object with no encoding at
all. It derives from GrafxError, so it carries a code, a retry flag and machine-readable details
like every other failure of the engine, and no untyped exception can escape through it
(CONTRACT.md section 11 item 5).

Keeping it here rather than in the public taxonomy is deliberate: the layers above translate it
into whatever their own surface promises, and a caller of the public API never has to know that
the heap validates tuples before it encodes them.
"""

from __future__ import annotations

from okto_grafx.domain.errors import GrafxError

__all__ = ["SchemaMismatchError"]


class SchemaMismatchError(GrafxError):
    """A tuple does not match the schema of its table, and the engine refuses to guess.

    The details name the table, the column and both the declared and the observed type, because
    a silent coercion is exactly the failure this error exists to prevent.
    """

    code: str = "schema_mismatch"
    retryable: bool = False
