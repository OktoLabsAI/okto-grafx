"""Isolated real-process cuts for format-4 resume; never used on application data."""

import os
from pathlib import Path
import sys

import okto_grafx.transfer as transfer
import okto_grafx.transfer_resume as resume


def main():
    root, cut, codec = sys.argv[1:]
    root = Path(root)
    native_connect = transfer.connect

    def connect(*args, **kwargs):
        kwargs["codec"] = "pure" if kwargs.get("read_only", False) else codec
        return native_connect(*args, **kwargs)

    transfer.connect = resume.connect = connect
    if cut in ("after_batch", "before_wal_barrier", "after_wal_barrier"):
        original = transfer._stage_rows
        calls = 0

        def stage(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2 and cut != "after_batch":
                from okto_grafx.engine.wal_manager import WalManager
                barrier = WalManager.barrier

                def stop(self):
                    if cut == "after_wal_barrier":
                        barrier(self)
                    os._exit(73)

                WalManager.barrier = stop
            result = original(*args, **kwargs)
            if calls == 2:
                os._exit(73)
            return result

        transfer._stage_rows = stage
    else:
        original = resume._promote

        def promote(*args):
            if cut == "after_promotion":
                original(*args)
            os._exit(73)

        resume._promote = promote
    transfer.import_graph(root / "artifact", root / "target", resume_directory=root / "work",
                          limits=transfer.TransferLimits(batch_rows=1))


if __name__ == "__main__":
    main()
