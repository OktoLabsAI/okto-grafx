"""Database-owned provider selection survives nested callbacks, threads and reopen."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, get_ident

import pytest

from okto_grafx import connect
from okto_grafx.domain.page import checksum
from okto_grafx.runtime import bootstrap
from okto_grafx.runtime.checksum_scope import capture_checksum, checksum_scope


@pytest.fixture(autouse=True)
def preserve_standalone_selection(monkeypatch):
    monkeypatch.setattr(checksum, "_implementation_state", checksum._implementation_state)


def initialize(db):
    with db.begin() as tx:
        tx.execute("CREATE NODE TABLE P(id INT64, PRIMARY KEY(id))")
        tx.execute("CREATE (:P {id:1})")


def test_two_real_selectors_survive_ambient_install_and_reopen(tmp_path, monkeypatch):
    before = checksum._implementation_state
    with connect(tmp_path / "pure", checksum="pure") as pure:
        initialize(pure)
        with connect(tmp_path / "auto", checksum="auto") as auto:
            initialize(auto)
            assert checksum._implementation_state is before
            with auto._checksum_scope():
                automatic_name = checksum.crc32c_implementation()
            checksum.install_crc32c(checksum.crc32c_reference, name="standalone-test")
            original = checksum._current_implementation
            seen = []

            def observe():
                state = original()
                seen.append(state[1])
                return state

            monkeypatch.setattr(checksum, "_current_implementation", observe)
            for db, expected in ((pure, "pure"), (auto, automatic_name), (pure, "pure")):
                seen.clear()
                with db.begin() as tx:
                    tx.execute("CREATE (:P {id:2})") if db is auto else tx.execute("MATCH (p:P) RETURN p.id")
                db.checkpoint()
                assert db.verify().clean
                assert seen and set(seen) == {expected}
            assert checksum.crc32c_implementation() == "standalone-test"
    with connect(tmp_path / "pure", checksum="auto", read_only=True) as reopened:
        assert reopened.execute("MATCH (p:P) RETURN count(p)").rows == ((1,),)


def test_nested_and_threaded_database_calls_restore_the_owning_provider(tmp_path, monkeypatch):
    def install(config):
        checksum.install_crc32c(checksum.crc32c_reference, name=config.checksum)
    monkeypatch.setattr(bootstrap, "install_checksum", install)
    with connect(tmp_path / "a", checksum="pure") as a, connect(tmp_path / "b", checksum="auto") as b:
        initialize(a)
        initialize(b)
        original = checksum._current_implementation
        records = {}
        def observe():
            state = original()
            records.setdefault(get_ident(), []).append(state[1])
            return state
        monkeypatch.setattr(checksum, "_current_implementation", observe)
        barrier = Barrier(2)
        def run(db, name):
            barrier.wait(timeout=10)
            for i in range(3):
                with db.begin() as tx:
                    tx.execute("CREATE (:P {id:$id})", {"id": i + 2})
            assert set(records[get_ident()]) == {name}
        with ThreadPoolExecutor(2) as executor:
            first = executor.submit(run, a, "pure")
            second = executor.submit(run, b, "auto")
            first.result(timeout=20)
            second.result(timeout=20)
        with a._public_transition():
            assert checksum.crc32c_implementation() == "pure"
            assert b.execute("MATCH (p:P) RETURN count(p)").rows == ((4,),)
            assert checksum.crc32c_implementation() == "pure"
            with pytest.raises(RuntimeError):
                with b._public_transition():
                    assert checksum.crc32c_implementation() == "auto"
                    raise RuntimeError("nested host failure")
            assert checksum.crc32c_implementation() == "pure"


def test_failed_selection_and_scope_control_signals_do_not_leak():
    before = checksum._implementation_state
    def refuse():
        checksum.install_crc32c(checksum.crc32c_reference, name="temporary")
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        capture_checksum(refuse)
    assert checksum._implementation_state is before
    state = capture_checksum(lambda: checksum.install_crc32c(checksum.crc32c_reference, name="private"))
    with pytest.raises(SystemExit):
        with checksum_scope(state):
            assert checksum.crc32c_implementation() == "private"
            raise SystemExit
    assert checksum._implementation_state is before
    assert checksum.crc32c_implementation() == before[1]


@pytest.mark.parametrize("selector", ["native", "auto"])
def test_missing_native_provider_does_not_change_an_existing_handle(tmp_path, monkeypatch, selector):
    from okto_grafx.adapters import checksum_native
    from okto_grafx.errors import GrafxConfigurationError
    with connect(tmp_path / "existing", checksum="pure") as existing:
        initialize(existing)
        before = checksum._implementation_state
        def absent():
            raise ImportError("optional provider absent")
        monkeypatch.setattr(checksum_native, "NativeCrc32c", absent)
        if selector == "native":
            with pytest.raises(GrafxConfigurationError):
                connect(tmp_path / "new", checksum=selector)
            assert not (tmp_path / "new").exists()
        else:
            with connect(tmp_path / "new", checksum=selector) as fallback:
                with fallback._public_transition():
                    assert checksum.crc32c_implementation() == "pure"
        with existing._public_transition():
            assert checksum.crc32c_implementation() == "pure"
            checksum.install_crc32c(checksum.crc32c_reference, name="legacy-during-operation")
            assert checksum.crc32c_implementation() == "pure"
        assert existing.execute("MATCH (p:P) RETURN count(p)").rows == ((1,),)
        assert before != checksum._implementation_state


def test_custom_registry_uses_its_connection_selection_without_mutating_default(tmp_path):
    from okto_grafx import DatabaseConfig
    config = DatabaseConfig(path=str(tmp_path / "custom"), checksum="pure")
    before = checksum._implementation_state
    ports = bootstrap.build_default_registry(config)
    try:
        with bootstrap.open_database(config, registry=ports) as db:
            initialize(db)
            with connect(tmp_path / "other", checksum="auto"):
                assert db.execute("MATCH (p:P) RETURN count(p)").rows == ((1,),)
                with db._public_transition():
                    assert checksum.crc32c_implementation() == "pure"
            assert db.verify().clean
        assert checksum._implementation_state is before
    finally:
        bootstrap.release_ports(ports)


def test_independent_readers_and_writers_on_one_store_can_use_different_selectors(tmp_path):
    root = tmp_path / "shared"
    with connect(root, checksum="pure") as pure:
        initialize(pure)
        with pure.begin("read") as reader, connect(root, checksum="auto") as other:
            with other.begin() as tx:
                tx.execute("CREATE (:P {id:2})")
            assert reader.execute("MATCH (p:P) RETURN count(p)").rows == ((1,),)
            assert other.execute("MATCH (p:P) RETURN count(p)").rows == ((2,),)
        assert pure.execute("MATCH (p:P) RETURN count(p)").rows == ((2,),)
        pure.checkpoint()
        assert pure.verify().clean
