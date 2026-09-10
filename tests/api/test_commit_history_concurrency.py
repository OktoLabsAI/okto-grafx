"""Qualified history ordering with independent processes and an older reader."""

import multiprocessing

import pytest

from okto_grafx import CommitMetadata, connect
from okto_grafx.errors import GrafxWriteConflict


def writer(root, base, ready, start, output):
    try:
        with connect(root, page_size=512, wal_segment_bytes=4096) as db:
            ready.put(base)
            if not start.wait(30):
                raise RuntimeError("start barrier timed out")
            tokens = []
            for i in range(3):
                transaction = db.begin(metadata=CommitMetadata(actor=str(base), attributes={"item": i}))
                for _attempt in range(20):
                    transaction.execute("CREATE (:P {id:$id})", {"id": base + i})
                    try:
                        report = transaction.commit()
                        tokens.append(report.csn)
                        break
                    except GrafxWriteConflict:
                        transaction = db.retry(transaction)
                else:
                    transaction.rollback()
                    raise RuntimeError("retry budget exhausted")
            output.put((base, tokens, None))
    except BaseException as failure:
        output.put((base, [], repr(failure)))


@pytest.mark.multiprocess
@pytest.mark.timeout(120)
def test_multiprocess_metadata_order_uniqueness_and_old_snapshot(tmp_path):
    root = str(tmp_path / "db")
    with connect(root, page_size=512, wal_segment_bytes=4096) as db:
        db.ensure_identity_indexes()
        db.enable_commit_history()
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE P(id INT64, PRIMARY KEY(id))")
        with db.begin("read") as reader:
            initial = reader.commit_history()
            context = multiprocessing.get_context("spawn")
            ready, output, start = context.Queue(), context.Queue(), context.Event()
            processes = [context.Process(target=writer, args=(root, base, ready, start, output))
                         for base in (100, 200)]
            try:
                for process in processes:
                    process.start()
                assert {ready.get(timeout=30), ready.get(timeout=30)} == {100, 200}
                start.set()
                reports = [output.get(timeout=40), output.get(timeout=40)]
                assert all(error is None for _base, _tokens, error in reports), reports
                for process in processes:
                    process.join(timeout=10)
                    assert process.exitcode == 0
                assert reader.commit_history() == initial
                entries = db.commit_history().entries
                records = [entry for entry in entries if entry.metadata is not None]
                sequences = [entry.identity.sequence for entry in records]
                assert len(sequences) == len(set(sequences)) == 6
                assert sequences == sorted(sequences)
                assert set(sequences) == {seq for _base, tokens, _error in reports for seq in tokens}
                assert {entry.metadata.actor for entry in records} == {"100", "200"}
                assert all(left.timing.ordered_at.micros < right.timing.ordered_at.micros
                           for left, right in zip(entries, entries[1:]))
            finally:
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                    if process.pid is not None:
                        process.join(timeout=5)
                ready.close()
                output.close()
        db.checkpoint()
    with connect(root, page_size=512, wal_segment_bytes=4096) as db:
        assert db.verify().clean
        assert db.execute("MATCH (p:P) RETURN count(p)").rows == ((6,),)
