"""Consumer smoke used unchanged against each installed distribution artefact."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import assert_type

import okto_grafx
from okto_grafx import Database, QueryResult, connect


def _require_installed_import() -> None:
    """Refuse a checkout import: the smoke must exercise the installed artefact."""
    package_file = Path(okto_grafx.__file__).resolve()
    environment = Path(sys.prefix).resolve()
    checkout_src = Path(__file__).resolve().parents[2] / "src"
    if not package_file.is_relative_to(environment):
        raise RuntimeError(
            f"okto_grafx was imported from {package_file}, outside {environment}"
        )
    if package_file.is_relative_to(checkout_src):
        raise RuntimeError(
            f"okto_grafx was imported from the checkout source tree: {package_file}"
        )


def main() -> None:
    """Exercise the supported package facade with no development dependencies."""
    _require_installed_import()
    with connect(":memory:") as database:
        assert_type(database, Database)
        with database.begin("write") as transaction:
            transaction.execute(
                "CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))"
            )
            transaction.execute("CREATE (:Person {id: 1, name: 'Ada'})")
        result = database.execute(
            "MATCH (p:Person) WHERE p.id = 1 RETURN p.name"
        )
        assert_type(result, QueryResult)
        if result.rows != (("Ada",),):
            raise RuntimeError(f"installed consumer returned unexpected rows: {result.rows!r}")


if __name__ == "__main__":
    main()
