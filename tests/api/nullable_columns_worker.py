"""Native schema-COMMIT process cuts on an isolated temporary database."""

import os
import sys

from okto_grafx import connect, Transaction
from okto_grafx.domain.model.schema import ColumnDef
from okto_grafx.domain.model.value import ValueType
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
                cut == "after_first_apply"
                and active is not None
                and active._context.state.value == "committed"
            ):
                native_apply(manager, images[:1])
                os._exit(74)
            if (
                cut == "before_apply"
                and active is not None
                and active._context.state.value == "committed"
            ):
                os._exit(73)
            return native_apply(manager, images)

        Transaction.commit = commit
        TransactionManager._apply_images = apply
        db.add_nullable_column("N", ColumnDef("extra", ValueType.STRING))


if __name__ == "__main__":
    main()
