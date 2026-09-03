"""Atomic streaming bulk writes through the public transaction facade."""

from __future__ import annotations

from collections.abc import Callable, ItemsView, Iterator, Mapping
from pathlib import Path

import pytest

from okto_grafx import ExecuteManyReport, connect
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxPlanError,
    GrafxTransactionBudgetExceeded,
    GrafxTransactionStateError,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.model.schema import encode_tuple
from okto_grafx.engine.query_engine import QueryEngine


_INSERT = "CREATE (:Person {id: $id, name: $name})"


class _ObservedParameters(Mapping[str, object]):
    def __init__(self, database: object, values: Mapping[str, object]) -> None:
        self._database = database
        self._values = dict(values)
        self.observations: list[bool] = []

    def __getitem__(self, name: str) -> object:
        return self._values[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def items(self) -> ItemsView[str, object]:
        self.observations.append(self._database._metrics.page_access_active)
        return self._values.items()


def _install_schema(database: object) -> None:
    with database.begin("write") as transaction:
        transaction.execute(
            "CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))"
        )


def _rows(database: object) -> tuple[tuple[object, ...], ...]:
    return database.execute("MATCH (p:Person) RETURN p.id, p.name ORDER BY p.id").rows


def test_executemany_matches_repeated_execute_and_parses_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parameters = (
        {"id": 3, "name": "Grace"},
        {"id": 1, "name": "Ada"},
        {"id": 2, "name": "Edsger"},
    )
    with connect(":memory:") as bulk, connect(":memory:") as repeated:
        _install_schema(bulk)
        _install_schema(repeated)

        parses = 0
        original_parse = QueryEngine.parse

        def counted_parse(engine: QueryEngine, text: str) -> object:
            nonlocal parses
            parses += 1
            return original_parse(engine, text)

        with bulk.begin("write") as transaction:
            with monkeypatch.context() as patch:
                patch.setattr(QueryEngine, "parse", counted_parse)
                report = transaction.executemany(_INSERT, iter(parameters))

        expected_statistics: dict[str, int] = {}
        with repeated.begin("write") as transaction:
            for item in parameters:
                result = transaction.execute(_INSERT, item)
                for name, count in result.statistics.items():
                    expected_statistics[name] = expected_statistics.get(name, 0) + count

        assert parses == 1
        assert report == ExecuteManyReport(
            statements=len(parameters), statistics=expected_statistics
        )
        assert not hasattr(report, "rows")
        assert not hasattr(report, "results")
        assert _rows(bulk) == _rows(repeated)


def test_generator_is_consumed_in_order_and_canonicalized_item_by_item() -> None:
    with connect(":memory:") as database:
        _install_schema(database)
        reused = {"id": 0, "name": ""}
        yielded: list[int] = []

        def parameter_sets() -> Iterator[Mapping[str, object]]:
            for identity, name in ((2, "second"), (1, "first"), (3, "third")):
                reused["id"] = identity
                reused["name"] = name
                yielded.append(identity)
                yield reused

        with database.begin("write") as transaction:
            report = transaction.executemany(_INSERT, parameter_sets())

        assert yielded == [2, 1, 3]
        assert report.statements == 3
        assert _rows(database) == (
            (1, "first"),
            (2, "second"),
            (3, "third"),
        )


def test_committed_batch_survives_cold_reopen_and_verifies_clean(
    tmp_path: Path,
) -> None:
    root = tmp_path / "durable-batch"
    with connect(root) as database:
        _install_schema(database)
        with database.begin("write") as transaction:
            transaction.executemany(
                _INSERT,
                (
                    {"id": 1, "name": "Ada"},
                    {"id": 2, "name": "Grace"},
                ),
            )
        assert database.verify("all").findings == ()

    with connect(root) as reopened:
        assert _rows(reopened) == ((1, "Ada"), (2, "Grace"))
        assert reopened.verify("all").findings == ()


def test_mid_batch_parameter_failure_discards_the_prefix_before_later_commit() -> None:
    with connect(":memory:") as database:
        _install_schema(database)
        transaction = database.begin("write")
        transaction.execute(_INSERT, {"id": 0, "name": "before"})

        with pytest.raises(GrafxPlanError) as raised:
            transaction.executemany(
                _INSERT,
                (
                    {"id": 1, "name": "batch-prefix"},
                    {"id": 2},
                    {"id": 3, "name": "never-consumed"},
                ),
            )

        assert raised.value.details["batch_index"] == 1
        transaction.execute(_INSERT, {"id": 1, "name": "after"})
        transaction.commit()
        assert _rows(database) == ((0, "before"), (1, "after"))


def test_mid_batch_transaction_budget_refusal_leaves_no_partial_rows() -> None:
    with connect(":memory:", max_transaction_rows=2) as database:
        _install_schema(database)
        transaction = database.begin("write")

        with pytest.raises(GrafxTransactionBudgetExceeded) as raised:
            transaction.executemany(
                _INSERT,
                (
                    {"id": 1, "name": "one"},
                    {"id": 2, "name": "two"},
                    {"id": 3, "name": "over-budget"},
                ),
            )

        assert raised.value.details["field"] == "max_transaction_rows"
        assert raised.value.details["batch_index"] == 2
        transaction.execute(_INSERT, {"id": 9, "name": "accepted"})
        transaction.commit()
        assert _rows(database) == ((9, "accepted"),)


def test_iterator_failure_is_typed_and_cannot_publish_a_successful_prefix() -> None:
    with connect(":memory:") as database:
        _install_schema(database)
        transaction = database.begin("write")
        transaction.execute(_INSERT, {"id": 0, "name": "before"})

        def parameter_sets() -> Iterator[Mapping[str, object]]:
            yield {"id": 1, "name": "prefix"}
            raise RuntimeError("injected iterator failure")

        with pytest.raises(GrafxConfigurationError) as raised:
            transaction.executemany(_INSERT, parameter_sets())

        assert isinstance(raised.value.__cause__, RuntimeError)
        assert raised.value.details["field"] == "executemany"
        transaction.execute(_INSERT, {"id": 9, "name": "after"})
        transaction.commit()
        assert _rows(database) == ((0, "before"), (9, "after"))


def test_mid_batch_byte_budget_refusal_restores_the_exact_batch_mark(
    tmp_path: Path,
) -> None:
    root = tmp_path / "byte-budget"
    with connect(root) as probe:
        _install_schema(probe)
        table = probe._catalog.catalog.table("Person")
        one_row_bytes = len(encode_tuple(table, (1, "one")))

    with connect(root, max_transaction_bytes=one_row_bytes) as database:
        transaction = database.begin("write")
        with pytest.raises(GrafxTransactionBudgetExceeded) as raised:
            transaction.executemany(
                _INSERT,
                (
                    {"id": 1, "name": "one"},
                    {"id": 2, "name": "two"},
                ),
            )

        assert raised.value.details["field"] == "max_transaction_bytes"
        assert raised.value.details["batch_index"] == 1
        assert transaction._context.row_intents == []
        assert transaction._context._staged_payload_bytes == 0
        assert transaction.active
        transaction.rollback()


@pytest.mark.parametrize(
    "statement",
    (
        "MATCH (p:Person) RETURN p.id",
        "CREATE NODE TABLE Other(id INT64)",
        "CREATE (p:Person {id: $id, name: $name}) RETURN p.id",
    ),
)
def test_unsafe_batch_shapes_are_refused_before_the_iterable_is_pulled(
    statement: str,
) -> None:
    with connect(":memory:") as database:
        _install_schema(database)
        pulled = False

        def parameter_sets() -> Iterator[Mapping[str, object]]:
            nonlocal pulled
            pulled = True
            yield {"id": 1, "name": "not-run"}

        transaction = database.begin("write")
        with pytest.raises(GrafxUnsupportedOperation):
            transaction.executemany(statement, parameter_sets())
        assert not pulled
        transaction.rollback()


def test_empty_batch_still_requires_write_mode() -> None:
    with connect(":memory:") as database:
        _install_schema(database)
        writer = database.begin("write")
        assert writer.executemany(_INSERT, ()) == ExecuteManyReport(0, {})
        assert writer.commit().wrote is False

        transaction = database.begin("read")
        with pytest.raises(GrafxTransactionStateError):
            transaction.executemany(_INSERT, ())
        transaction.rollback()


def test_generator_callbacks_run_outside_page_access_and_cannot_reenter_transaction() -> (
    None
):
    with connect(":memory:") as database:
        _install_schema(database)
        transaction = database.begin("write")
        page_access_observations: list[bool] = []
        refusals: dict[str, GrafxTransactionStateError] = {}
        first = _ObservedParameters(database, {"id": 1, "name": "one"})
        second = _ObservedParameters(database, {"id": 2, "name": "two"})

        def parameter_sets() -> Iterator[Mapping[str, object]]:
            page_access_observations.append(database._metrics.page_access_active)
            yield first
            page_access_observations.append(database._metrics.page_access_active)
            attempts: tuple[tuple[str, Callable[[], object]], ...] = (
                (
                    "execute",
                    lambda: transaction.execute("MATCH (p:Person) RETURN p.id"),
                ),
                ("scan_rows_v1", lambda: transaction.scan_rows_v1("Person", limit=1)),
                ("commit", transaction.commit),
                ("rollback", transaction.rollback),
                ("retry", lambda: database.retry(transaction)),
                ("executemany", lambda: transaction.executemany(_INSERT, ())),
            )
            for name, attempt in attempts:
                try:
                    attempt()
                except GrafxTransactionStateError as failure:
                    refusals[name] = failure
            yield second

        report = transaction.executemany(_INSERT, parameter_sets())
        assert page_access_observations == [False, False]
        assert first.observations == [False]
        assert second.observations == [False]
        assert set(refusals) == {
            "commit",
            "execute",
            "executemany",
            "retry",
            "rollback",
            "scan_rows_v1",
        }
        assert all(
            failure.details["active_operation"] == "executemany"
            for failure in refusals.values()
        )
        assert transaction.active
        transaction.commit()
        assert report.statements == 2
        assert _rows(database) == ((1, "one"), (2, "two"))


def test_reentrant_close_aborts_the_batch_without_publishing_its_prefix(
    tmp_path: Path,
) -> None:
    root = tmp_path / "closed-during-batch"
    database = connect(root)
    _install_schema(database)
    transaction = database.begin("write")

    def parameter_sets() -> Iterator[Mapping[str, object]]:
        yield {"id": 1, "name": "prefix"}
        database.close()
        yield {"id": 2, "name": "after-close"}

    with pytest.raises(GrafxTransactionStateError):
        transaction.executemany(_INSERT, parameter_sets())

    with connect(root) as reopened:
        assert _rows(reopened) == ()


def test_bulk_write_can_follow_schema_staged_in_the_same_transaction() -> None:
    with connect(":memory:") as database:
        transaction = database.begin("write")
        transaction.execute(
            "CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))"
        )
        transaction.executemany(
            _INSERT,
            ({"id": 1, "name": "same-transaction-schema"},),
        )
        transaction.commit()

        assert _rows(database) == ((1, "same-transaction-schema"),)


def test_failed_batch_preserves_schema_staged_before_its_savepoint() -> None:
    with connect(":memory:") as database:
        transaction = database.begin("write")
        transaction.execute(
            "CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))"
        )

        with pytest.raises(GrafxPlanError):
            transaction.executemany(
                _INSERT,
                (
                    {"id": 1, "name": "discarded-prefix"},
                    {"id": 2},
                ),
            )
        transaction.commit()

        assert _rows(database) == ()
