"""Hard process exits cover new native transitions, including partial physical application."""

import os
from pathlib import Path
import subprocess
import sys

import pytest

from okto_grafx import TemporalLimits, connect
from tests.api.test_system_time_queries import prepare, write


@pytest.mark.parametrize("operation", ["index", "indexed_update", "compact", "replace_text"])
@pytest.mark.parametrize("cut", ["before_commit", "before_apply", "after_current", "after_history_root", "after_history_chunk", "after_commit"])
def test_new_transitions_recover_twice(tmp_path, operation, cut):
    root = tmp_path / "db"
    with connect(root, page_size=512) as db:
        _, activation = prepare(db)
        if operation in ("indexed_update", "compact"):
            db.enable_system_history_index()
        if operation == "compact":
            for i in range(3):
                write(db, "MATCH (n:N {id:1}) SET n.value='" + str(i) * 3000 + "'")
            retained = write(db, "MATCH (n:N {id:1}) SET n.value='retained'")
            db.prune_system_history(retained, tables=("N",))
        if operation == "replace_text":
            db.create_text_index("text", "N", ("value",), bucket_count=4)
        db.checkpoint()
    worker = Path(__file__).with_name("system_history_worker.py")
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
    result = subprocess.run([sys.executable, str(worker), str(root), operation, cut],
                            env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode in (71, 72, 73, 74), result.stdout + result.stderr
    for attempt in range(2):
        with connect(root, page_size=512) as db:
            current = db.commit_history().entries[-1].identity
            indexed = db.system_as_of(current, tables=("N",))
            scan = db.system_as_of(current, tables=("N",), limits=TemporalLimits(access_path="scan"))
            assert indexed.rows == scan.rows and indexed.schemas == scan.schemas
            assert db.verify().clean
            if operation == "index":
                assert db._catalog.catalog.requires_capability("system_history_index_v1") == (cut != "before_commit")
            elif operation == "indexed_update":
                assert indexed.rows[0].values == (1, "first" if cut == "before_commit" else "changed" * 200)
            elif operation == "replace_text":
                result = db.search_text(index="text", query="first", return_positions=cut != "before_commit")
                assert len(result.hits) == 1
            elif operation == "compact":
                assert indexed.rows[0].values == (1, "retained")
                # A committed but untruncated tail is not a failed recovery. New
                # writes may reuse it only through the proved logical extent.
                if attempt == 0:
                    write(db, "MATCH (n:N {id:1}) SET n.value='retained'")
            db.checkpoint()
