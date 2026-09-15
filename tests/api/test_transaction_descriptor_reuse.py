"""Unlocked participant descriptor reuse across repeated transaction statements.

TXN-1 -- parking one proved-unlocked descriptor of the participant lock file for the lifetime of
a transaction -- exists to avoid re-opening that file per statement. W-01 went one step further
for the DEFAULT composition: the participant section name carries this coordinator instance's own
``owner_id`` digest, so no other participant can ever contend for it, and the concrete coordinator
now serialises it in this process and opens no descriptor per entry at all. There is then nothing
left for TXN-1 to park there.

Both contracts are alive and both are tested here:

* every test that is ABOUT the descriptor scope runs on the ``undeclared_participant_section``
  fixture (``tests/api/conftest.py``), which makes the coordinator decline the private-section
  declaration exactly as a custom or older coordinator would. The counts below are therefore still the real counts of the
  path that opens and locks the file, unchanged from before W-01.
* :func:`test_the_declared_participant_section_opens_and_locks_nothing_per_statement` and its
  siblings assert what the default composition does instead: zero opens, zero advisory locks and
  no scope at all for the same workload.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from okto_grafx import Transaction, connect
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, undeclared_participant_section: None
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


def test_autocommit_reuses_one_descriptor_but_takes_every_real_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, undeclared_participant_section: None
) -> None:
    root = tmp_path / "autocommit-descriptor"
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

        with monkeypatch.context() as patch:
            patch.setattr(coordination_local.os, "open", counted_open)
            patch.setattr(coordination_local, "_acquire_os_lock", counted_acquire)
            patch.setattr(coordination_local, "_release_os_lock", counted_release)
            assert database.execute(_SEEK, {"id": 1}).rows == ((1,),)

        assert opened == 1
        assert acquired == 4
        assert released == 4
        assert database._transactions._transaction_descriptor_scopes == {}
        assert coordinator._descriptor_scopes == {}


def test_bounded_transaction_scope_revalidates_one_descriptor_for_its_whole_lifetime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, undeclared_participant_section: None
) -> None:
    root = tmp_path / "bounded-transaction-descriptor"
    with connect(root, checkpoint_interval_records=10_000) as database:
        _seed(database)
        coordinator = database._transactions._coordinator
        participant = database._transactions._participant_section_name
        participant_suffix = f"{participant}.lock"
        real_open = os.open
        real_names = type(coordinator)._descriptor_names
        opened = 0
        revalidated = 0

        def counted_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal opened
            descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
            if str(path).endswith(participant_suffix):
                opened += 1
            return descriptor

        def counted_names(descriptor: int, path: str) -> bool:
            nonlocal revalidated
            revalidated += 1
            return real_names(descriptor, path)

        with monkeypatch.context() as patch:
            patch.setattr(coordination_local.os, "open", counted_open)
            patch.setattr(
                type(coordinator), "_descriptor_names", staticmethod(counted_names)
            )
            with database.transaction("read") as transaction:
                assert transaction.execute(_SEEK, {"id": 1}).rows == ((1,),)

        # begin opens and parks the descriptor; statement, manager commit and public schema
        # settlement each revalidate it before taking the real operating-system lock.
        assert opened == 1
        assert revalidated == 3
        assert database._transactions._transaction_descriptor_scopes == {}
        assert coordinator._descriptor_scopes == {}


def test_autocommit_scope_cannot_suppress_a_process_control_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = KeyboardInterrupt("query interrupted")

    class SuppressingScope:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *_outcome: object) -> bool:
            return True

    def scope(_coordinator: object, _name: str) -> SuppressingScope:
        return SuppressingScope()

    def fail_execute(
        _transaction: Transaction, _text: str, _parameters: object = None
    ) -> object:
        raise failure

    with connect(":memory:") as database, monkeypatch.context() as patch:
        patch.setattr(
            coordination_local.LocalProcessCoordinator,
            "reuse_unlocked_section_descriptor",
            scope,
        )
        patch.setattr(Transaction, "execute", fail_execute)
        with pytest.raises(KeyboardInterrupt) as raised:
            database.execute("RETURN 1")

        assert raised.value is failure
        assert database._transactions.open_transactions == 0


def test_autocommit_scope_exit_failure_is_only_cleanup_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = KeyboardInterrupt("query interrupted")
    cleanup = SystemExit("scope exit interrupted")

    class FailingExitScope:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *_outcome: object) -> None:
            raise cleanup

    def scope(_coordinator: object, _name: str) -> FailingExitScope:
        return FailingExitScope()

    def fail_execute(
        _transaction: Transaction, _text: str, _parameters: object = None
    ) -> object:
        raise failure

    with connect(":memory:") as database, monkeypatch.context() as patch:
        patch.setattr(
            coordination_local.LocalProcessCoordinator,
            "reuse_unlocked_section_descriptor",
            scope,
        )
        patch.setattr(Transaction, "execute", fail_execute)
        with pytest.raises(KeyboardInterrupt) as raised:
            database.execute("RETURN 1")

        assert raised.value is failure
        assert any(
            "SystemExit" in note and "scope exit interrupted" in note
            for note in getattr(failure, "__notes__", ())
        )
        assert database._transactions.open_transactions == 0


def test_autocommit_rolls_back_a_commit_failure_that_left_the_context_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = KeyboardInterrupt("commit interrupted")

    def fail_commit(_transaction: Transaction) -> object:
        raise failure

    with connect(":memory:") as database, monkeypatch.context() as patch:
        patch.setattr(Transaction, "commit", fail_commit)
        with pytest.raises(KeyboardInterrupt) as raised:
            database.execute("RETURN 1")

        assert raised.value is failure
        assert database._transactions.open_transactions == 0
        assert database.closed is False


def test_idle_transaction_descriptor_never_excludes_another_thread(
    tmp_path: Path, undeclared_participant_section: None
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
    tmp_path: Path, undeclared_participant_section: None
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
    tmp_path: Path, undeclared_participant_section: None
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, undeclared_participant_section: None
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


def test_the_declared_participant_section_opens_and_locks_nothing_per_statement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W-01, default composition: the same workload costs zero opens and zero advisory locks.

    Compare with ``test_repeated_execute_reuses_only_the_unlocked_participant_descriptor``, which
    is the identical workload on a coordinator that declined the declaration and still pays 3
    opens and 6 lock/unlock pairs on the participant file.
    """
    root = tmp_path / "declared-participant-section"
    with connect(root, checkpoint_interval_records=10_000) as database:
        _seed(database)
        coordinator = database._transactions._coordinator
        participant = database._transactions._participant_section_name
        assert participant in coordinator._private_section_locks
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

        assert opened == 0
        assert acquired == 0
        assert released == 0
        # Nothing is opened per entry, so TXN-1 has nothing to park and installs no scope.
        assert database._transactions._transaction_descriptor_scopes == {}
        assert coordinator._descriptor_scopes == {}
        # The lock file itself is still there: the control inventory is unchanged.
        assert (root / "control" / participant_suffix).is_file()


def test_the_declared_participant_section_still_serialises_two_threads_of_one_database(
    tmp_path: Path,
) -> None:
    """The section a real database declares still excludes a second thread of that database."""
    root = tmp_path / "declared-participant-threads"
    with connect(root, checkpoint_interval_records=10_000) as database:
        _seed(database)
        coordinator = database._transactions._coordinator
        participant = database._transactions._participant_section_name
        assert participant in coordinator._private_section_locks

        admitted: list[str] = []
        holding = threading.Event()
        release = threading.Event()

        def hold() -> None:
            with coordinator.exclusive(participant, timeout=30.0):
                admitted.append("holder")
                holding.set()
                release.wait(timeout=30.0)

        def contend() -> None:
            with coordinator.exclusive(participant, timeout=30.0):
                admitted.append("contender")

        holder = threading.Thread(target=hold, name="participant-holder")
        holder.start()
        assert holding.wait(timeout=30.0)
        contender = threading.Thread(target=contend, name="participant-contender")
        contender.start()
        contender.join(timeout=0.2)
        assert contender.is_alive() is True
        assert admitted == ["holder"]
        release.set()
        holder.join(timeout=30.0)
        contender.join(timeout=30.0)
        assert admitted == ["holder", "contender"]


def test_two_databases_in_one_process_never_share_a_participant_section(
    tmp_path: Path,
) -> None:
    """Two handles over one directory get two names, so neither can wait on the other's."""
    root = tmp_path / "two-handles-one-directory"
    with connect(root, checkpoint_interval_records=10_000) as first:
        _seed(first)
        with connect(root, checkpoint_interval_records=10_000) as second:
            first_name = first._transactions._participant_section_name
            second_name = second._transactions._participant_section_name
            assert first_name != second_name
            assert (
                first._transactions._coordinator.owner_id()
                != second._transactions._coordinator.owner_id()
            )
            with first._transactions._coordinator.exclusive(first_name, timeout=1.0):
                # The other handle's own section is untouched, and so is its work.
                with second._transactions._coordinator.exclusive(
                    second_name, timeout=0.2
                ):
                    pass
                assert second.execute(_SEEK, {"id": 1}).rows == ((1,),)


def test_memory_transactions_keep_the_descriptor_optimization_absent() -> None:
    with connect(":memory:") as database:
        _seed(database)
        with database.begin("read") as transaction:
            for _ in range(3):
                assert transaction.execute(_SEEK, {"id": 1}).rows == ((1,),)
            assert database._transactions._transaction_descriptor_scopes == {}
