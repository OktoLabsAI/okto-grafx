"""Native entity scalar signatures, independent of execution bindings."""

from okto_grafx.domain.errors import GrafxPlanError
from okto_grafx.domain.model.value import ValueType

ENTITY_SCALARS = frozenset({"PROPERTIES", "LABELS", "TYPE"})


def entity_scalar_type(name: str, value_type: ValueType | None, *,
                       entity_kind: str | None = None, phase: str = "planning") -> ValueType:
    """Reject proven bad types; heterogeneous runtime values remain deferred."""
    allowed = {"node", "relationship"} if name == "PROPERTIES" else (
        {"node"} if name == "LABELS" else {"relationship"})
    if entity_kind is not None and entity_kind not in allowed:
        raise entity_scalar_error(name, phase)
    if entity_kind is None and value_type not in (
        None, ValueType.NULL, *((ValueType.MAP,) if name == "PROPERTIES" else ()),
    ):
        raise entity_scalar_error(name, phase)
    return {"PROPERTIES": ValueType.MAP, "LABELS": ValueType.LIST, "TYPE": ValueType.STRING}[name]


def entity_scalar_error(name: str, phase: str) -> GrafxPlanError:
    """Provide native phase evidence without rendering a possibly hostile value."""
    return GrafxPlanError(
        "The entity function does not accept this argument type.", field="function",
        value=name, reason="entity_function_argument_type", query_phase=phase,
    )
