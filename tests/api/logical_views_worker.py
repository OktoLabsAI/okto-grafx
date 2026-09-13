"""Process-death cuts for native view-definition replacement on a temporary store."""

import os
import sys

from okto_grafx import connect, Transaction
from okto_grafx.engine.txn_manager import TransactionManager


def main():
    path, cut = sys.argv[1:]
    with connect(path) as db:
        native_commit = Transaction.commit
        native_apply = TransactionManager._apply_images
        active = None

        def commit(tx):
            nonlocal active
            active = tx
            if cut == "before_commit":
                os._exit(71)
            result = native_commit(tx)
            if cut == "after_commit":
                os._exit(72)
            return result

        def apply(manager, images):
            if (
                cut == "before_apply"
                and active is not None
                and active._context.state.value == "committed"
            ):
                os._exit(73)
            return native_apply(manager, images)

        Transaction.commit = commit
        TransactionManager._apply_images = apply
        db.views.create("value", query="RETURN 2 AS n", replace=True)


if __name__ == "__main__":
    main()
