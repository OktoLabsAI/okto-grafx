"""Several processes opening the SAME fresh path at once must agree on one identity (P0.3).

The first open creates the identity page, the catalog and the heap under no lease and no
section: nothing coordinates two processes that both find the path empty. What must hold is
what a user of an embedded database expects from two services starting at once: every open
either succeeds with the SAME database UUID or refuses with a typed error, the path ends up
holding ONE database, and whatever each successful participant wrote is there afterwards.
This is a probe as much as a regression: its outcomes are reported per participant.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from okto_grafx import connect

CHILD = Path(__file__).resolve().parent / "first_open_child.py"
PARTICIPANTS: int = 3
CHILD_BUDGET: float = 120.0


def _run_participants(root: Path, markers: Path) -> list[dict[str, Any]]:
    """Start every participant, release them together, and return what each one reported."""
    go = markers / "go"
    running = []
    for slot in range(1, PARTICIPANTS + 1):
        ready = markers / f"ready-{slot}"
        running.append(
            subprocess.Popen(
                [
                    sys.executable,
                    str(CHILD),
                    str(root),
                    str(slot),
                    str(ready),
                    str(go),
                    "60",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )
    deadline = time.monotonic() + 60.0
    while not all(
        (markers / f"ready-{slot}").exists() for slot in range(1, PARTICIPANTS + 1)
    ):
        assert time.monotonic() < deadline, "a participant never became ready"
        time.sleep(0.01)
    go.write_text("go", encoding="utf-8")
    reports: list[dict[str, Any]] = []
    for child in running:
        out, err = child.communicate(timeout=CHILD_BUDGET)
        assert out.strip(), f"a participant produced no report; stderr={err[-800:]}"
        reports.append(json.loads(out.strip().splitlines()[-1]))
    return reports


@pytest.mark.multiprocess
@pytest.mark.timeout(300, method="thread")
def test_concurrent_first_opens_agree_on_one_identity(tmp_path: Path) -> None:
    root = tmp_path / "db"
    markers = tmp_path / "markers"
    markers.mkdir()
    reports = _run_participants(root, markers)

    died = [
        report for report in reports if report["outcome"] not in ("opened", "refused")
    ]
    assert not died, reports
    opened = [report for report in reports if report["outcome"] == "opened"]
    assert opened, reports  # at least one participant must be handed the database
    identities = {report["uuid"] for report in opened}
    assert len(identities) == 1, {"identities": identities, "reports": reports}

    with connect(root) as database:
        assert str(database.identity.database_uuid) in identities, reports
        assert database.verify("all").findings == (), reports
        tables = {table.name for table in database.catalog.catalog.tables()}
        for report in opened:
            if report["detail"] == "wrote its own table":
                assert f"Marker{report['slot']}" in tables, {
                    "tables": tables,
                    "reports": reports,
                }
