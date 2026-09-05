"""Unlocked participant descriptor reuse across repeated transaction statements."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from okto_grafx import connect
from okto_grafx.adapters import coordination_local
from okto_grafx.domain.errors import GrafxWriteConflict


_SEEK = "MATCH (p:Person) WHERE p.id = $id RETURN p.id"


def _seed(database: object) -> None:
    with database.begin("write") as transaction:
        transaction.execute(
            "CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))"
        )
    with database.begin("write") as transaction:
        transaction.execute("CREATE (:Person {id: 1, name: 'Ada'})")


def test_repeated_execute_reuses_only_the_unlocked_participant_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repeated-execute-descriptor"
    with connect(root, checkpoint_interval_records=10_000) as database:
        _seed(database)
        coordinator = database._transactions._coordinator
        participant = database._transactions._participant_section_name
        participant_suffix = f"{participant}.lock"
        real_open = os.open
        real_acquire = coordination_local._acquire_os_lock
        real_release = coordination_local._release_os_lock
        participant_descriptors: set[int] = set()
        opened = 0
        acquired = 0
        released = 0

        def counted_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal opened
            descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
            if str(path).endswith(participant_suffix):
                opened += 1
                participant_descriptors.add(descriptor)
            return descriptor

        def counted_acquire(descriptor: int) -> None:
            nonlocal acquired
            if descriptor in participant_descriptors:
                acquired += 1
            real_acquire(descriptor)

        def counted_release(descriptor: int) -> None:
            nonlocal released
            if descriptor in participant_descriptors:
                released += 1
            real_release(descriptor)

        transaction = database.begin("read")
        with monkeypatch.context() as patch:
            patch.setattr(coordination_local.os, "open", counted_open)
            patch.setattr(coordination_local, "_acquire_os_lock", counted_acquire)
            patch.setattr(coordination_local, "_release_os_lock", counted_release)
            for _ in range(4):
                assert transaction.execute(_SEEK, {"id": 1}).rows == ((1,),)
            transaction.commit()

        # The first statement installs the scope after taking its canonical handle. The second
        # opens the descriptor that it parks; statements three/four and the manager commit reuse
        # it. The public schema settlement happens after the manager has drained the transaction
        # scope, so that independent terminal section correctly opens once more.
        assert opened == 3
        assert acquired == 6
        assert released == 6
        assert database._transactions._transaction_descriptor_scopes == {}
        assert coordinator._descriptor_scopes == {}


def test_idle_transaction_descriptor_never_excludes_another_thread(
    tmp_path: Path,
) -> None:
    root = tmp_path / "idle-descriptor-interleaving"
    with connect(root, checkpoint_interval_records=10_000) as database:
        _seed(database)
        transaction = database.begin("read")
        coordinator = database._transactions._coordinator
        participant = database._transactions._participant_section_name

        for _ in range(4):
            assert transaction.execute(_SEEK, {"id": 1}).rows == ((1,),)
            admitted: list[bool] = []

            def contend() -> None:
                with coordinator.exclusive(participant, timeout=1.0):
                    admitted.append(True)

            worker = threading.Thread(target=contend, name="between-statements")
            worker.start()
            worker.join(timeout=5.0)
            assert worker.is_alive() is False
            assert admitted == [True]

        transaction.rollback()
        assert database._transactions._transaction_descriptor_scopes == {}
        assert coordinator._descriptor_scopes == {}


def test_database_close_drains_an_idle_transaction_descriptor(
    tmp_path: Path,
) -> None:
    root = tmp_path / "close-idle-descriptor"
    database = connect(root, checkpoint_interval_records=10_000)
    _seed(database)
    transaction = database.begin("read")
    coordinator = database._transactions._coordinator

    for _ in range(3):
        assert transaction.execute(_SEEK, {"id": 1}).rows == ((1,),)
    assert transaction.txn_id in database._transactions._transaction_descriptor_scopes

    database.close()

    assert transaction.active is False
    assert database._transactions._transaction_descriptor_scopes == {}
    assert coordinator._descriptor_scopes == {}


def test_occ_refusal_keeps_the_scope_until_retry_retires_the_predecessor(
    tmp_path: Path,
) -> None:
    root = tmp_path / "descriptor-occ-retry"
    with connect(root, checkpoint_interval_records=10_000) as database:
        _seed(database)
        winner = database.begin("write")
        loser = database.begin("write")
        for _ in range(3):
            winner.execute("MATCH (p:Person {id: 1}) SET p.name = 'winner'")
            loser.execute("MATCH (p:Person {id: 1}) SET p.name = 'loser'")

        winner.commit()
        assert winner.txn_id not in database._transactions._transaction_descriptor_scopes

        with pytest.raises(GrafxWriteConflict):
            loser.commit()
        assert loser.active
        assert loser.txn_id in database._transactions._transaction_descriptor_scopes

        successor = database.retry(loser)
        assert loser.active is False
        assert loser.txn_id not in database._transactions._transaction_descriptor_scopes
        assert successor.txn_id not in database._transactions._transaction_descriptor_scopes
        successor.rollback()
        assert database._transactions._coordinator._descriptor_scopes == {}


def test_thread_hop_uses_cold_handles_and_still_drains_the_original_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "descriptor-thread-hop"
    with connect(root, checkpoint_interval_records=10_000) as database:
        _seed(database)
        transaction = database.begin("read")
        assert transaction.execute(_SEEK, {"id": 1}).rows == ((1,),)
        assert transaction.execute(_SEEK, {"id": 1}).rows == ((1,),)

        participant = database._transactions._participant_section_name
        participant_suffix = f"{participant}.lock"
        real_open = os.open
        worker_opens = 0

        def counted_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal worker_opens
            descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
            if str(path).endswith(participant_suffix):
                worker_opens += 1
            return descriptor

        outcome: list[tuple[tuple[object, ...], ...]] = []

        def finish_elsewhere() -> None:
            outcome.append(transaction.execute(_SEEK, {"id": 1}).rows)
            transaction.rollback()

        with monkeypatch.context() as patch:
            patch.setattr(coordination_local.os, "open", counted_open)
            worker = threading.Thread(target=finish_elsewhere, name="transaction-hop")
            worker.start()
            worker.join(timeout=5.0)

        assert worker.is_alive() is False
        assert outcome == [((1,),)]
        # The parked descriptor is bound to the original thread. The worker opens for its
        # statement and rollback, then the schema settlement opens after rollback drained the
        # transaction scope; none can transfer or borrow the old fd.
        assert worker_opens == 3
        assert database._transactions._transaction_descriptor_scopes == {}
        assert database._transactions._coordinator._descriptor_scopes == {}


def test_memory_transactions_keep_the_descriptor_optimization_absent() -> None:
    with connect(":memory:") as database:
        _seed(database)
        with database.begin("read") as transaction:
            for _ in range(3):
                assert transaction.execute(_SEEK, {"id": 1}).rows == ((1,),)
            assert database._transactions._transaction_descriptor_scopes == {}
