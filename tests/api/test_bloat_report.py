"""Read-only heap-bloat census at the existing recyclable horizon."""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_grafx import Database, connect
from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.public_views import BloatReport, TableBloatReport


def _device_image(database: Database) -> tuple[tuple[str, bytes], ...]:
    """Capture every logical storage file so an observation cannot hide an in-place write."""
    device = database._storage
    return tuple(
        (name, device.read_log(name, 0, device.file_size(name)))
        for name in device.list_files()
    )


def _seed_two_tables(database: Database) -> None:
    with database.begin("write") as schema:
        schema.execute("CREATE NODE TABLE A(id INT64, note STRING, PRIMARY KEY(id))")
        schema.execute("CREATE NODE TABLE B(id INT64, note STRING, PRIMARY KEY(id))")
    with database.begin("write") as rows:
        rows.execute("CREATE (:A {id: 1, note: 'a'})")
        rows.execute("CREATE (:B {id: 2, note: 'b'})")
    with database.begin("write") as updates:
        updates.execute("MATCH (a:A {id: 1}) SET a.note = 'aa'")
        updates.execute("MATCH (b:B {id: 2}) SET b.note = 'bb'")
    database.checkpoint()


def test_bloat_is_idempotent_read_only_aggregated_and_table_filterable() -> None:
    with connect(":memory:") as database:
        _seed_two_tables(database)
        before = _device_image(database)

        first = database.maintenance.bloat()
        second = database.maintenance.bloat()
        only_a = database.maintenance.bloat("A")

        assert first == second
        assert _device_image(database) == before
        assert type(first) is BloatReport
        assert tuple(item.table for item in first.tables) == ("A", "B")
        assert all(type(item) is TableBloatReport for item in first.tables)
        assert first.stored_versions == sum(item.stored_versions for item in first.tables)
        assert first.stored_versions == 4
        assert first.ended_versions == 2
        assert first.vacuum_safety_established is False
        assert first.horizon_eligible_versions == 2
        assert first.horizon_retained_versions == 0
        assert (
            first.horizon_eligible_versions + first.horizon_retained_versions
            == first.ended_versions
        )
        assert first.stored_versions - first.ended_versions == 2
        assert first.horizon_eligible_slot_bytes == sum(
            item.horizon_eligible_slot_bytes for item in first.tables
        )
        assert first.horizon_eligible_slot_bytes > 0
        assert only_a.tables == (first.tables[0],)
        assert only_a.stored_versions == 2
        assert only_a.ended_versions == 1
        assert only_a.horizon_eligible_versions == 1

        with pytest.raises(GrafxConfigurationError) as raised:
            database.maintenance.bloat("missing")
        assert raised.value.details["field"] == "table"


def test_bloat_uses_the_reader_pinned_nonpruning_horizon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with connect(":memory:") as database:
        with database.begin("write") as schema:
            schema.execute("CREATE NODE TABLE P(id INT64, note STRING, PRIMARY KEY(id))")
        with database.begin("write") as seed:
            seed.execute("CREATE (:P {id: 1, note: 'before'})")

        old_reader = database.begin("read")
        try:
            with database.begin("write") as update:
                update.execute("MATCH (p:P {id: 1}) SET p.note = 'after'")
            database.checkpoint()

            def pruning_horizon_forbidden(_coordinator: object) -> None:
                raise AssertionError("a read-only bloat census must not prune reader records")

            monkeypatch.setattr(
                type(database._transactions._coordinator),
                "reader_horizon",
                pruning_horizon_forbidden,
            )

            report = database.maintenance.bloat("P")
            assert report.recyclable_horizon_lsn == old_reader.snapshot.read_lsn
            assert report.ended_versions == 1
            assert report.vacuum_safety_established is False
            assert report.horizon_eligible_versions == 0
            assert report.horizon_retained_versions == 1
            assert (
                report.horizon_eligible_versions + report.horizon_retained_versions
                == report.ended_versions
            )
            assert report.horizon_eligible_slot_bytes == 0
            assert report.horizon_retained_slot_bytes > 0
        finally:
            old_reader.rollback()


def test_bloat_reads_headers_without_decoding_payload_or_following_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    large = "x" * 9_000
    with connect(":memory:") as database:
        with database.begin("write") as schema:
            schema.execute("CREATE NODE TABLE P(id INT64, note STRING, PRIMARY KEY(id))")
        with database.begin("write") as seed:
            seed.execute("CREATE (:P {id: 1, note: $note})", {"note": large})
        with database.begin("write") as update:
            update.execute(
                "MATCH (p:P {id: 1}) SET p.note = $note",
                {"note": large + "y"},
            )
        database.checkpoint()

        def payload_forbidden(*_args: object, **_kwargs: object) -> bytes:
            raise AssertionError("the bloat census must not decode or follow payload")

        monkeypatch.setattr(HeapStore, "_payload_of", payload_forbidden)
        report = database.maintenance.bloat("P")

        assert report.stored_versions == 2
        assert report.overflow_versions == 2
        assert report.horizon_eligible_versions == 1
        assert report.horizon_eligible_overflow_versions == 1
        assert 0 < report.horizon_eligible_slot_bytes < len(large)


def test_bloat_remains_available_on_a_read_only_database(tmp_path: Path) -> None:
    root = tmp_path / "database"
    with connect(root) as writer:
        with writer.begin("write") as schema:
            schema.execute("CREATE NODE TABLE P(id INT64, note STRING, PRIMARY KEY(id))")
        with writer.begin("write") as seed:
            seed.execute("CREATE (:P {id: 1, note: 'before'})")
        with writer.begin("write") as update:
            update.execute("MATCH (p:P {id: 1}) SET p.note = 'after'")
        writer.checkpoint()

    with connect(root, read_only=True) as reader:
        before = _device_image(reader)
        report = reader.maintenance.bloat("P")

        assert report.ended_versions == 1
        assert report.vacuum_safety_established is False
        assert report.horizon_eligible_versions == 1
        assert _device_image(reader) == before
