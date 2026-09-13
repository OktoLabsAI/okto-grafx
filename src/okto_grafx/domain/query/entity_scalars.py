"""Native entity scalar signatures, independent of execution bindings."""

from __future__ import annotations

from okto_grafx.domain.errors import GrafxPlanError
from okto_grafx.domain.model.value import ValueType

ENTITY_SCALARS = frozenset({"PROPERTIES", "KEYS", "LABELS", "TYPE", "STARTNODE", "ENDNODE"})


def entity_scalar_type(name: str, value_type: ValueType | None, *,
                       entity_kind: str | None = None, phase: str = "planning") -> ValueType | None:
    """Reject proven bad types; heterogeneous runtime values remain deferred."""
    allowed = {"node", "relationship"} if name in {"PROPERTIES", "KEYS"} else (
        {"node"} if name == "LABELS" else {"relationship"})
    if entity_kind is not None and entity_kind not in allowed:
        raise entity_scalar_error(name, phase)
    if entity_kind is None and value_type not in (
        None, ValueType.NULL, *((ValueType.MAP,) if name in {"PROPERTIES", "KEYS"} else ()),
    ):
        raise entity_scalar_error(name, phase)
    return {"PROPERTIES": ValueType.MAP, "KEYS": ValueType.LIST,
            "LABELS": ValueType.LIST, "TYPE": ValueType.STRING,
            "STARTNODE": None, "ENDNODE": None}[name]


def entity_scalar_error(name: str, phase: str) -> GrafxPlanError:
    """Provide native phase evidence without rendering a possibly hostile value."""
    return GrafxPlanError(
        "The entity function does not accept this argument type.", field="function",
        value=name, reason="entity_function_argument_type", query_phase=phase,
    )


__all__ = [
    'ENTITY_SCALARS',
    'entity_scalar_type',
    'entity_scalar_error',
]
