"""Public selection syntax shared by name-qualified graph transport/history doors."""

from __future__ import annotations

from typing import Protocol

from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.model.schema import TableDef

TableSelector = str | tuple[str, str]


class _TableCatalog(Protocol):
    """Shared read contract of live catalogs and detached catalog observations."""

    def table(self, name: str, *, kind: str | None = None) -> TableDef:
        """Resolve one table by name and optional node or relationship kind."""
        ...

    def has_table(self, name: str, *, kind: str | None = None) -> bool:
        """Report whether a table exists in the optional kind-specific namespace."""
        ...


def table_selector_parts(selector: TableSelector) -> tuple[str | None, str]:
    """Detach either a unique physical name or an explicit (kind, name) pair."""
    if type(selector) is str and selector:
        return None, selector
    if (type(selector) is tuple and len(selector) == 2
            and type(selector[0]) is str and selector[0] in ("node", "rel")
            and type(selector[1]) is str and selector[1]):
        return selector
    raise GrafxConfigurationError("Select a physical name or a (node|rel, name) tuple.", field="tables")


def select_table(catalog: _TableCatalog, selector: TableSelector) -> TableDef:
    """Resolve against the caller's captured catalog; never choose the first match."""
    kind, name = table_selector_parts(selector)
    return catalog.table(name, kind=kind)


def validate_table_selections(selectors: object, *, allow_empty: bool = False) -> None:
    """Validate an immutable, bounded set of explicit table selectors."""
    if type(selectors) is not tuple or not selectors and not allow_empty:
        raise GrafxConfigurationError("Specify distinct table selections.", field="tables")
    for selector in selectors:
        table_selector_parts(selector)
    if len(set(selectors)) != len(selectors):
        raise GrafxConfigurationError("Specify distinct table selections.", field="tables")


def select_tables(catalog: _TableCatalog, selectors: tuple[TableSelector, ...]) -> tuple[TableDef, ...]:
    """Resolve selectors and refuse duplicate physical table identities."""
    selected = tuple(select_table(catalog, selector) for selector in selectors)
    if len({table.table_id for table in selected}) != len(selected):
        raise GrafxConfigurationError("Table selections resolve to duplicate identities.", field="tables")
    return selected


def public_table_selector(catalog: _TableCatalog, table: TableDef) -> TableSelector:
    """Return a kind-qualified selector only when the table name is ambiguous."""
    if catalog.has_table(table.name, kind="node") and catalog.has_table(table.name, kind="rel"):
        return table.kind, table.name
    return table.name


__all__ = [
    'TableSelector',
    'table_selector_parts',
    'select_table',
    'validate_table_selections',
    'select_tables',
    'public_table_selector',
]
