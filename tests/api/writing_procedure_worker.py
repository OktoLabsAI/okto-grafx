"""Abrupt process-exit witness for native writing CALL before and after COMMIT."""

import os
import sys

from okto_grafx import connect
from okto_grafx.extensions import ExtensionRegistry, TabularProcedure


def run(path: str, codec: str, phase: str) -> None:
    """Stop without Python cleanup at the requested procedure/transaction boundary."""
    def write(writer, value):
        writer.execute("CREATE (:T {id:$id, value:$id})", {"id": value})
        if phase == "callback":
            os._exit(71)

    procedure = TabularProcedure("app.write", ("INT64",), (), write, mode="write",
                                  required_permissions=frozenset({"mutate"}))
    registry = ExtensionRegistry(trusted=True, procedures=(procedure,),
                                  procedure_permissions=frozenset({"mutate"}))
    with connect(path, codec=codec, extensions=registry) as db:
        with db.begin("write") as tx:
            tx.execute("CALL app.write(2)")
            if phase == "statement":
                os._exit(72)
        os._exit(73)


if __name__ == "__main__":
    run(*sys.argv[1:])
