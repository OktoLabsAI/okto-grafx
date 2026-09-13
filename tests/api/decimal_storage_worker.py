"""Crash without cleanup before or after the native decimal COMMIT boundary."""

import os
import sys

from okto_grafx import connect
from okto_grafx.domain.model.decimal_values import DecimalValue


def run(path: str, codec: str, phase: str) -> None:
    """Leave no process cleanup that could manufacture recovery evidence."""
    with connect(path, codec=codec) as db:
        tx = db.begin()
        tx.execute("CREATE (:N {id:1, val:$v})", {"v": {"amount": DecimalValue(-12345, 38, 4)}})
        if phase == "statement":
            os._exit(71)
        tx.commit()
        os._exit(72)


if __name__ == "__main__":
    run(*sys.argv[1:])
