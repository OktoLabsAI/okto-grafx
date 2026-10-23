"""RF12 functional guards for foreign checkpoint and bounded HASH diagnosis.

These tests establish semantics and refusal boundaries; they are not a timed
campaign and do not authorize checkpoint cadence, rehash or vacuum changes.
"""

import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from bench.harness import capacity_qualification as capacity
from bench.harness.campaign_fixtures import Scenario
from okto_grafx import connect
from okto_grafx.errors import GrafxQueryBudgetExceeded


_HELD_FOREIGN_WRITER = """
import json, sys
sys.path.insert(0, sys.argv[1])
from okto_grafx import connect
with connect(sys.argv[2], codec='pure', vector_math='pure', **json.loads(sys.argv[3])) as db:
    tx = db.begin('write')
    try:
        tx.execute("MATCH (n:P) SET n.note='after'")
        ack = tx.commit()
    except BaseException:
        if tx.active:
            tx.rollback()
        raise
    from pathlib import Path
    Path(sys.argv[4]).write_text(json.dumps({'durable': ack.durable, 'wrote': ack.wrote}))
    sys.stdin.read()
"""


@pytest.mark.parametrize("interval", [128, 512, 2048])
def test_foreign_commit_survives_checkpoint_with_retained_reader(tmp_path, interval):
    """A foreign ACK precedes the local checkpoint; the foreign pool stays alive.

    The stronger WAL-recycling and physical barrier race is covered by
    test_checkpoint_and_reclamation.py. This checks the RF12 configurations and
    read ownership without conflating manual checkpoint with auto cadence.
    """
    scenario = next(s for s in capacity.matrix(Scenario)
                    if s.checkpoint_interval_records == interval)
    from dataclasses import replace
    scenario = replace(scenario, nodes=4, write_rows=4, capacity_case="external")
    root = tmp_path / "db"
    expected_before = tuple((i, "before") for i in range(4))
    expected_after = tuple((i, "after") for i in range(4))
    source = Path(__file__).resolve().parents[2] / "src"
    acknowledgement = tmp_path / "foreign-ack.json"

    with connect(root, codec="pure", vector_math="pure",
                 **capacity.connect_options(scenario)) as checkpointer:
        capacity.build_fixture(checkpointer, scenario)
        reader = checkpointer.begin("read")
        foreign = None
        try:
            assert reader.execute(capacity.query_for(scenario)[0]).rows == expected_before
            foreign = subprocess.Popen(
                [sys.executable, "-E", "-P", "-B", "-c", _HELD_FOREIGN_WRITER,
                 str(source), str(root), json.dumps(capacity.connect_options(scenario)),
                 str(acknowledgement)],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                text=True,
            )
            deadline = time.monotonic() + 20
            while not acknowledgement.exists() and foreign.poll() is None and time.monotonic() < deadline:
                time.sleep(.05)
            assert acknowledgement.exists(), "foreign writer did not return a durable ACK"
            assert json.loads(acknowledgement.read_text()) == {"durable": True, "wrote": True}
            checkpointer.checkpoint()
            assert reader.execute(capacity.query_for(scenario)[0]).rows == expected_before
            reader.rollback()
            assert checkpointer.execute(capacity.query_for(scenario)[0]).rows == expected_after
        finally:
            if reader.active:
                reader.rollback()
            if foreign is not None:
                foreign.kill()
                foreign.wait(timeout=10)
                if foreign.stdin is not None:
                    foreign.stdin.close()
                if foreign.stderr is not None:
                    foreign.stderr.close()

    with connect(root, read_only=True, codec="pure", vector_math="pure",
                 **capacity.connect_options(scenario)) as reopened:
        assert reopened.execute(capacity.query_for(scenario)[0]).rows == expected_after
        assert reopened.verify("all").findings == ()


def test_hash_skew_diagnosis_is_bounded_read_only_and_cannot_authorize_rehash(tmp_path):
    root = tmp_path / "db"
    with connect(root, page_size=512, codec="pure", vector_math="pure") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE D(id INT64, v STRING, PRIMARY KEY(id))")
            tx.execute("UNWIND $ids AS id CREATE (:D {id:id,v:'same'})",
                       {"ids": list(range(40))})
        db.create_index("v", "D", ("v",), bucket_count=1)
        before = capacity._image(db)
        report = db.index_distribution(
            "v", max_pages=1000, max_entries=1000, max_memory_bytes=1 << 20,
        )
        assert report.entries == 40
        assert report.overflow_pages > 0
        assert report.dominant_key_fraction == 1.0
        assert report.recommendation == "inspect_key_skew"
        assert "same" not in repr(report)
        assert capacity._image(db) == before
        for limit in ({"max_pages": 1}, {"max_entries": 1},
                      {"max_memory_bytes": 1}):
            with pytest.raises(GrafxQueryBudgetExceeded):
                db.index_distribution("v", **limit)
            assert capacity._image(db) == before
        assert db.verify("all").findings == ()
        db.checkpoint()

    with connect(root, read_only=True, page_size=512,
                 codec="pure", vector_math="pure") as reopened:
        # Read-only operations may register a transient reader lease. Compare
        # persisted data/index/WAL bytes, not that coordination record.
        def durable_image():
            return tuple(entry for entry in capacity._image(reopened)
                         if not entry[0].startswith("control/readers/"))

        before = durable_image()
        assert reopened.index_distribution("v", max_pages=1000, max_entries=1000,
                                           max_memory_bytes=1 << 20) == report
        for limit in ({"max_pages": 1}, {"max_entries": 1},
                      {"max_memory_bytes": 1}):
            with pytest.raises(GrafxQueryBudgetExceeded):
                reopened.index_distribution("v", **limit)
            assert durable_image() == before
        assert reopened.verify("all").findings == ()

    decision = capacity.decision_template(None, objective="review one HASH index")
    assert decision["decision"] == "new_research"
    assert decision["template_only"] is True
    assert decision["change_defaults"] is False
    assert decision["authorize_vacuum"] is False
