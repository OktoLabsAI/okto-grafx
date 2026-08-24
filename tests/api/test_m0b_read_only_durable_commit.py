"""M0B invariant 1: a durable COMMIT above ``commit.state`` makes a read-only open refuse.

A crash after the log barrier and before the publication of ``commit.state`` leaves a commit
that IS durable (the log holds it, barriered) but that the published position does not cover.
A writable open recovers it. A read-only open may not write, so it may not recover either: it
must REFUSE, typed, rather than answer from a published position the log has already passed --
and it must leave every data byte as it found it (byte-identical), because a reader that
repairs is a writer.
"""

from __future__ import annotations

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxError
from okto_grafx.runtime.bootstrap import release_ports

from m0b_probe_support import crash_before_the_commit_state_publication, ids
from power_loss_support import data_digests

pytestmark = pytest.mark.timeout(300, method="thread")


@pytest.mark.xfail(
    strict=True,
    reason="M0B invariant 1 (base 8c88e9d): the read-only open OPENED over a log holding a durable "
    "COMMIT above commit.state and answered () from the published position; pending M0B.",
)
def test_read_only_refuses_when_the_log_holds_a_durable_commit_above_the_published_state() -> (
    None
):
    registry, bench, inner = crash_before_the_commit_state_publication(seed=1)
    before = data_digests(inner)
    try:
        read_only = connect(":memory:", registry=registry, read_only=True)
    except GrafxError as refused:
        outcome: tuple[str, object] = ("refused", refused.code)
    else:
        with read_only:
            outcome = ("opened", ids(read_only))
    after = data_digests(inner)
    release_ports(registry)
    changed = sorted(
        n for n in set(before) | set(after) if before.get(n) != after.get(n)
    )
    assert not changed, f"the read-only open changed data files: {changed}"
    assert outcome[0] == "refused", (
        f"read-only opened over a durable commit the published state does not cover: {outcome}"
    )
