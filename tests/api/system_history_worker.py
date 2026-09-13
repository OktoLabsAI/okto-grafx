"""Deliberate native process termination against isolated temporal fixtures."""

import os
import sys

from okto_grafx import Transaction, connect
from okto_grafx.engine.txn_manager import TransactionManager


def main():
    path, operation, cut, *codec = sys.argv[1:]
    with connect(path, page_size=512, codec=codec[0] if codec else "pure") as db:
        original_commit = Transaction.commit
        original_apply = TransactionManager._apply_images
        active = None

        def commit(tx):
            nonlocal active
            active = tx
            if cut == "before_commit":
                os._exit(71)
            result = original_commit(tx)
            if cut == "after_commit":
                os._exit(72)
            return result

        def apply(manager, images):
            if active is not None and active._context.state.value == "committed":
                if cut == "before_apply":
                    os._exit(73)
                if cut in {"after_current", "after_history_root", "after_history_chunk"}:
                    if cut == "after_current":
                        selected = [image for image in images if image[0] not in {"system-history.dat"}]
                    elif cut == "after_history_root":
                        selected = [image for image in images if image[0] == "system-history.dat" and image[1] == 0]
                    else:
                        selected = [image for image in images if image[0] == "system-history.dat" and image[1] > 0][:1]
                    original_apply(manager, selected)
                    os._exit(74)
            return original_apply(manager, images)

        Transaction.commit = commit
        TransactionManager._apply_images = apply
        if operation == "node_labels":
            with db.begin("write") as tx:
                tx.execute("MATCH(n:N {id:1}) REMOVE n:N SET n:Audit, n.value=$value", {"value":"changed" * 200})
        elif operation == "implicit_labels":
            with db.begin("write") as tx:
                tx.execute("CREATE(a:A {v:'text'})-[:R]->(b:B {v:2})")
        elif operation == "activate":
            db.enable_system_history(("N",))
        elif operation == "index":
            db.enable_system_history_index()
        elif operation == "compact":
            db.compact_system_history(confirm_quiescent=True)
        elif operation == "replace_text":
            from okto_grafx import TextIndexOptions
            db.replace_text_index("text", options=TextIndexOptions(positions=True))
        elif operation == "prune":
            from okto_grafx import CommitId
            boundary = CommitId(db.identity.database_uuid, db._transactions.published_state().last_committed_lsn)
            db.prune_system_history(boundary, tables=("N",))
        elif operation == "flex_update":
            with db.begin() as tx:
                tx.execute("MATCH()-[r:R]->() SET r.v=$value", {"value":"changed"*200})
        else:
            with db.begin() as tx:
                tx.execute("MATCH (n:N {id:1}) SET n.value = $value", {"value": "changed" * 200})


if __name__ == "__main__":
    main()
