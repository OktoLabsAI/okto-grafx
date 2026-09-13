"""Logical relationship types with separately qualified physical endpoint tables.

This metadata does not turn a table-local record id into a global identity. Each
member retains its table id, endpoints, heap and indexes. The catalog validates
the references before installing or decoding the definition.
"""

from __future__ import annotations

from dataclasses import dataclass

from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.model.schema import is_identifier


RELATIONSHIP_TYPES_CAPABILITY = "relationship_types_v1"


@dataclass(frozen=True, slots=True)
class RelationshipTypeDef:
    """One logical name and its nonempty, canonically ordered physical members."""

    name: str
    table_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        """Reject ambiguous, mutable or noncanonical identity metadata."""
        if type(self.name) is not str or not is_identifier(self.name):
            raise GrafxConfigurationError("Invalid relationship type name.", field="relationship_type")
        if (type(self.table_ids) is not tuple or not self.table_ids
                or any(type(key) is not int or not 0 < key <= 0xFFFFFFFF for key in self.table_ids)
                or any(left >= right for left, right in zip(self.table_ids, self.table_ids[1:]))):
            raise GrafxConfigurationError(
                "Relationship members need distinct ascending table ids.", field="relationship_members",
            )


__all__ = [
    'RELATIONSHIP_TYPES_CAPABILITY',
    'RelationshipTypeDef',
]
