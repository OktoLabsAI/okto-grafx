"""M0B invariant 2: a catalog whose pages were lost after a DDL commit is repaired from the log.

The DDL's catalog pages are only flushed at commit; the log holds their WRITE_PAGE images. A
power loss that takes the pages back leaves a catalog that is behind (or logically inconsistent
with) the schema the log has committed. Recovery must replay those images BEFORE the catalog is
loaded, so the reopen presents the schema and every door works on it.
"""

from __future__ import annotations

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxError
from okto_grafx.runtime.bootstrap import release_ports

from m0b_probe_support import ids, insert, lose_catalog_pages_after_a_ddl
from power_loss_support import SEEDS

pytestmark = pytest.mark.timeout(300, method="thread")


def test_a_catalog_whose_pages_were_lost_after_a_ddl_commit_opens_with_the_schema_after_recovery() -> (
    None
):
    for seed in SEEDS:
        registry, bench, inner, produced = lose_catalog_pages_after_a_ddl(seed)
        if produced:
            break
        release_ports(registry)
    else:
        pytest.fail("no seed lost a catalog page while keeping the log")
    try:
        reopened = connect(":memory:", registry=registry)
    except GrafxError as refused:
        release_ports(registry)
        pytest.fail(
            f"the reopen refused instead of repairing the catalog from the log: {refused.code}"
        )
    with reopened:
        tables = sorted(table.name for table in reopened.catalog.catalog.tables())
        assert tables == ["Person"], tables
        assert reopened.verify("all").findings == ()
        insert(reopened, 1)
        assert ids(reopened) == (1,)
    release_ports(registry)
