"""Controlled subprocess cuts: invoked only by catalog-copy tests on temporary stores."""

import os
import sys

from okto_grafx import connect, Transaction
from okto_grafx.catalog_copy import capture_copy, copy_graph
from okto_grafx.engine.txn_manager import TransactionManager


def main():
    source_path, target_path, cut = sys.argv[1:]
    with connect(source_path, read_only=True) as source:
        with source.begin("read") as tx:
            package = capture_copy(tx, tables=("N", "R"))
    with connect(target_path) as target:
        native_commit = Transaction.commit
        native_apply = TransactionManager._apply_images
        active_copy = None

        def commit(tx):
            nonlocal active_copy
            active_copy = tx
            if cut == "before_commit":
                os._exit(71)
            result = native_commit(tx)
            if cut == "after_commit":
                os._exit(72)
            return result

        def apply(manager, images):
            # Native step 3.6 is reached only after the successful COMMIT barrier.
            if (
                cut == "before_heap_apply"
                and active_copy is not None
                and active_copy._context.state.value == "committed"
            ):
                os._exit(73)
            return native_apply(manager, images)

        Transaction.commit = commit
        TransactionManager._apply_images = apply
        copy_graph(package, target, idempotency_key="crash")


if __name__ == "__main__":
    main()
