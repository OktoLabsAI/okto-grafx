"""Abrupt-exit oracle for procedure schema and data before/after durable COMMIT."""

import os
import sys

from okto_grafx import connect
from okto_grafx.extensions import ExtensionRegistry, TabularProcedure


def run(path: str, codec: str, phase: str, index: str = "no") -> None:
    """Exit without Python cleanup at the selected transaction boundary."""
    def callback(writer):
        writer.schema("CREATE NODE TABLE New (id INT64, PRIMARY KEY(id))")
        writer.execute("CREATE (:New {id:7})")
        if index == "yes":
            writer.execute("MATCH (n:Existing {id:1}) SET n.value=20")
            writer.schema("CREATE INDEX by_value FOR (n:Existing) ON (n.value) OPTIONS bucket_count=8")
            writer.execute("CREATE (:Existing {id:2,value:30})")
        if phase == "callback":
            os._exit(71)
    procedure = TabularProcedure("app.schema", (), (), callback, mode="write", schema_write=True,
                                 required_permissions=frozenset({"schema"}))
    extensions = ExtensionRegistry(trusted=True, procedures=(procedure,), procedure_permissions=frozenset({"schema"}))
    with connect(path, codec=codec, extensions=extensions) as db:
        with db.begin("write") as tx:
            tx.execute("CALL app.schema()")
            if phase == "statement":
                os._exit(72)
        os._exit(73)


if __name__ == "__main__":
    run(*sys.argv[1:])
