"""The public catalog view is memoized by full identity and never survives a catalog change."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from okto_grafx import connect
from okto_grafx.adapters.storage_local import LocalStorageDevice
from okto_grafx.engine import database as database_module

P_DDL = "CREATE NODE TABLE P(id INT64, PRIMARY KEY(id))"
Q_DDL = "CREATE NODE TABLE Q(id INT64, PRIMARY KEY(id))"


@pytest.fixture
def view_builds(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Count every construction of the public catalog view inside Database."""
    original = database_module._catalog_view_from
    builds: list[str] = []

    def counting(store: Any, catalog: Any) -> Any:
        builds.append("build")
        return original(store, catalog)

    monkeypatch.setattr(database_module, "_catalog_view_from", counting)
    return builds


def test_repeated_catalog_reads_build_the_view_once_and_return_the_same_snapshot(
    tmp_path: Path, view_builds: list[str]
) -> None:
    # CQ-4/QW-6: the epoch proof under the participant section runs on EVERY access; what the
    # memo removes is only the reconstruction of the immutable view while the adopted image
    # (persisted bytes) and the live catalog object are the very same.
    with connect(tmp_path / "db", page_size=512) as database:
        with database.begin("write") as txn:
            txn.execute(P_DDL)
        view_builds.clear()

        first = database.catalog
        second = database.catalog
        third = database.catalog

        assert first.catalog.has_table("P")
        assert second is first
        assert third is first
        assert view_builds == ["build"]


def test_a_ddl_commit_invalidates_the_memo_and_the_next_view_sees_the_new_table(
    tmp_path: Path, view_builds: list[str]
) -> None:
    with connect(tmp_path / "db", page_size=512) as database:
        with database.begin("write") as txn:
            txn.execute(P_DDL)
        view_builds.clear()
        before = database.catalog
        assert not before.catalog.has_table("Q")

        with database.begin("write") as txn:
            txn.execute(Q_DDL)

        after = database.catalog
        assert after.catalog.has_table("Q")
        assert not before.catalog.has_table("Q")  # the old snapshot stays immutable
        assert after is not before
        assert view_builds.count("build") == 2
        # And the fresh generation memoizes again.
        assert database.catalog is after
        assert view_builds.count("build") == 2


def test_an_unsaved_live_mutation_never_answers_from_the_memo(tmp_path: Path) -> None:
    # The pathological door: mutating the LIVE catalog object in place changes neither
    # id(store._catalog) nor the persisted image bytes. A memo keyed on those two alone would
    # keep answering with the pre-mutation view; the clean-state guard (has_unsaved_changes,
    # D7's own door) must send the access back to a fresh construction instead.
    database = connect(tmp_path / "db", page_size=512)
    try:
        with database.begin("write") as txn:
            txn.execute(P_DDL)
        memoized = database.catalog
        assert memoized.catalog.has_table("P")

        store = database._catalog
        live = store.catalog
        base = live.table("P")
        live.add_table(
            replace(base, name="P_mutated_live", table_id=live.next_table_id())
        )
        assert store.has_unsaved_changes()

        fresh = database.catalog
        assert fresh.catalog.has_table("P_mutated_live")
        assert not memoized.catalog.has_table("P_mutated_live")
    finally:
        database.close()


def test_the_adopt_door_recovery_and_commits_share_drops_the_memo(
    tmp_path: Path, view_builds: list[str]
) -> None:
    # txn_manager (a commit that touched catalog.dat), recovery_manager and refresh() all end
    # in store.adopt(store.read_from_pages()). Adopting installs a new live object and a new
    # persisted image, so the memo must miss and the next access must rebuild.
    with connect(tmp_path / "db", page_size=512) as database:
        with database.begin("write") as txn:
            txn.execute(P_DDL)
        view_builds.clear()
        before = database.catalog
        assert view_builds == ["build"]

        store = database._catalog
        store.adopt(store.read_from_pages())

        after = database.catalog
        assert after is not before
        assert after.catalog.has_table("P")
        assert view_builds.count("build") == 2


def test_memoized_reads_write_no_page_and_still_prove_the_epoch_every_access(
    tmp_path: Path, view_builds: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    page_writes: list[tuple[str, int]] = []
    original_write = LocalStorageDevice.write_page

    def counting_write(
        self: LocalStorageDevice, file: str, page_index: int, data: bytes
    ) -> None:
        page_writes.append((file, page_index))
        return original_write(self, file, page_index, data)

    original_snapshot = database_module.Database._catalog_snapshot
    proofs: list[str] = []

    def counting_snapshot(self: Any) -> Any:
        proofs.append("snapshot")
        return original_snapshot(self)

    monkeypatch.setattr(LocalStorageDevice, "write_page", counting_write)
    monkeypatch.setattr(database_module.Database, "_catalog_snapshot", counting_snapshot)

    with connect(tmp_path / "db", page_size=512) as database:
        with database.begin("write") as txn:
            txn.execute(P_DDL)
        view_builds.clear()
        page_writes.clear()
        proofs.clear()

        for _ in range(5):
            assert database.catalog.catalog.has_table("P")

        assert proofs == ["snapshot"] * 5  # one epoch-proved observation per access
        assert view_builds == ["build"]  # one reconstruction for the five
        assert page_writes == []  # reading a view writes nothing


def test_a_reopened_database_rebuilds_its_view_from_its_own_bootstrap(
    tmp_path: Path, view_builds: list[str]
) -> None:
    root = tmp_path / "db"
    with connect(root, page_size=512) as database:
        with database.begin("write") as txn:
            txn.execute(P_DDL)
        assert database.catalog.catalog.has_table("P")
    view_builds.clear()
    with connect(root, page_size=512) as database:
        reopened = database.catalog
        assert reopened.catalog.has_table("P")
        assert view_builds.count("build") == 1
