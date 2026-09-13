"""Real-process crash cuts for native DECIMAL transfer and history publication."""

import os
from pathlib import Path
import sys


def import_cut(root: Path, cut: str) -> None:
    """Lose the process after a durable private batch or destination promotion."""
    import okto_grafx.transfer as transfer
    import okto_grafx.transfer_resume as resume
    if cut == "after_wal_barrier":
        original = transfer._stage_rows
        calls = 0

        def stage(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                from okto_grafx.engine.wal_manager import WalManager
                barrier = WalManager.barrier

                def stop(manager):
                    barrier(manager)
                    os._exit(73)

                WalManager.barrier = stop
            return original(*args, **kwargs)

        transfer._stage_rows = stage
    elif cut == "after_promotion":
        original = resume._promote

        def promote(*args):
            original(*args)
            os._exit(73)

        resume._promote = promote
    else:
        raise AssertionError(cut)
    transfer.import_graph(root / "artifact", root / "target", resume_directory=root / "work",
                          limits=transfer.TransferLimits(batch_rows=1))
    raise AssertionError("Import crash cut not reached")


def history_cut(root: Path, cut: str, codec: str, model: str) -> None:
    """Stop before COMMIT or after its proof but before applying data/history pages."""
    from okto_grafx import connect
    from okto_grafx.engine.database import Transaction
    from okto_grafx.engine.txn_manager import TransactionManager
    with connect(root / "source", codec=codec) as db:
        active = None
        commit, apply = Transaction.commit, TransactionManager._apply_images

        def stop_commit(tx):
            nonlocal active
            active = tx
            if cut == "before_commit":
                os._exit(71)
            assert cut == "before_apply"
            return commit(tx)

        def stop_apply(manager, images):
            if active is not None and active._context.state.value == "committed":
                os._exit(73)
            return apply(manager, images)

        Transaction.commit, TransactionManager._apply_images = stop_commit, stop_apply
        with db.begin() as tx:
            tx.execute("MATCH(n)-[r]->() SET n.v1=decimal('9.00',12,4),r.v1=decimal('8.00',12,4)" if model == "typed"
                       else "MATCH(n)-[r]->() SET n.bag=decimal('9.00',12,4),r.bag=decimal('8.00',12,4)")
    raise AssertionError("History crash cut not reached")


if __name__ == "__main__":
    action, directory, phase, *extra = sys.argv[1:]
    if action == "import":
        import_cut(Path(directory), phase)
    elif action == "history":
        history_cut(Path(directory), phase, *extra)
    else:
        raise AssertionError(action)
