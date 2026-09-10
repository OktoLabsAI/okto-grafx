"""Attached catalogs: explicit permissions, independent begins, identity and cleanup."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
import shutil

import pytest

from okto_grafx import CatalogPathPolicy, CatalogSession, Database, connect
from okto_grafx.errors import (
    GrafxConfigurationError,
    GrafxError,
    GrafxTransactionStateError,
)


@pytest.fixture
def stores(tmp_path):
    databases = [connect(tmp_path / name) for name in ("a", "b")]
    for i, db in enumerate(databases):
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE (:N {id:$id})", {"id": i})
    yield databases, CatalogPathPolicy((str(tmp_path),))
    for db in databases:
        db.close()


def test_pinned_native_transactions_and_borrowed_ownership(stores):
    (a, b), policy = stores
    with CatalogSession(a, owned=False, policy=policy, read_only=False) as session:
        session.attach_handle(b, alias="Second", owned=False, read_only=False)
        with session.begin("write") as first:
            session.use("SECOND")
            with session.begin("write") as second:
                first.execute("CREATE (:N {id:10})")
                second.execute("CREATE (:N {id:20})")
                with pytest.raises(GrafxTransactionStateError):
                    session.detach("second")
                with pytest.raises(GrafxTransactionStateError):
                    session.close()
        assert [i.alias for i in session.catalogs()] == ["main", "second"]
        session.detach("second")
        with session.begin() as tx:
            assert tx.execute("MATCH (n:N) RETURN n.id ORDER BY n.id").rows == (
                (0,),
                (10,),
            )
    assert not a.closed and not b.closed
    assert b.execute("MATCH (n:N) RETURN n.id ORDER BY n.id").rows == ((1,), (20,))
    with pytest.raises(GrafxTransactionStateError):
        session.begin()


def test_permissions_and_default_read_transaction(stores):
    (a, b), policy = stores
    with CatalogSession(a, owned=False, policy=policy) as session:
        session.attach_handle(b, alias="other", owned=False)
        for catalog in ("main", "other"):
            with pytest.raises(GrafxTransactionStateError):
                session.begin("write", catalog=catalog)
            with session.begin(catalog=catalog) as tx:
                assert tx.mode == "read"
                with pytest.raises(GrafxError):
                    tx.execute("CREATE (:N {id:200})")


@pytest.mark.parametrize(
    "alias", ["main", "MAIN", "1bad", "é", "../a", "", "a" * 65, None]
)
def test_bad_aliases(stores, alias):
    (a, b), policy = stores
    with CatalogSession(a, owned=False, policy=policy) as session:
        with pytest.raises(GrafxConfigurationError):
            session.attach_handle(b, alias=alias, owned=True)
        assert not b.closed


def test_duplicate_handle_alias_and_bound(stores):
    (a, b), policy = stores
    with CatalogSession(a, owned=False, policy=policy) as session:
        with pytest.raises(GrafxConfigurationError):
            session.attach_handle(a, alias="duplicate", owned=True)
        session.attach_handle(b, alias="B", owned=False)
        with pytest.raises(GrafxConfigurationError):
            session.attach_handle(b, alias="b", owned=False)
    bounded = CatalogPathPolicy(
        policy.allowed_roots, max_catalogs=1, max_active_transactions=1
    )
    with CatalogSession(a, owned=False, policy=bounded) as session:
        with pytest.raises(GrafxConfigurationError):
            session.attach_handle(b, alias="b", owned=False)
        with session.begin():
            with pytest.raises(GrafxConfigurationError):
                session.begin()
        with session.begin():
            pass


def test_open_owned_read_only_does_not_change_store_bytes(stores):
    (a, b), policy = stores
    a.checkpoint()
    b.checkpoint()
    a.close()
    b.close()
    from pathlib import Path

    before = {
        str(p): p.read_bytes()
        for base in (a.path, b.path)
        for p in Path(base).rglob("*")
        if p.is_file()
    }
    with CatalogSession.open(a.path, policy=policy) as session:
        session.attach(b.path, alias="b")
        with session.begin(catalog="b") as tx:
            assert tx.execute("MATCH (n:N) RETURN n.id").rows == ((1,),)
        assert all(i.owned and i.read_only for i in session.catalogs())
    after = {
        str(p): p.read_bytes()
        for base in (a.path, b.path)
        for p in Path(base).rglob("*")
        if p.is_file()
    }
    assert {p: after.get(p) for p in before} == before
    # Native RO transactions can leave empty OS coordination lock files. They
    # are not recovery/graph writes; no other new bytes or files are permitted.
    import re

    for path in after.keys() - before.keys():
        assert Path(path).parent.name == "control"
        assert re.fullmatch(r"txn-[0-9a-f]+\.lock", Path(path).name)
        assert after[path] == b""


def test_clone_uuid_refused_without_closing_borrowed_handle(stores, tmp_path):
    (a, _), policy = stores
    a.checkpoint()
    a.close()
    clone_path = tmp_path / "clone"
    shutil.copytree(a.path, clone_path)
    with (
        connect(a.path, read_only=True) as original,
        connect(clone_path, read_only=True) as clone,
    ):
        with CatalogSession(original, owned=False, policy=policy) as session:
            with pytest.raises(GrafxConfigurationError):
                session.attach_handle(clone, alias="clone", owned=True)
            assert not clone.closed


def test_failed_native_begin_releases_reservation(stores, monkeypatch):
    (a, _), policy = stores
    with CatalogSession(a, owned=False, policy=policy) as session:

        def fail(*args, **kwargs):
            raise RuntimeError("begin fault")

        monkeypatch.setattr(Database, "begin", fail)
        with pytest.raises(RuntimeError, match="begin fault"):
            session.begin()
        assert session.catalogs()[0].active_transactions == 0


def test_one_blocked_begin_does_not_serialize_other_catalog(stores, monkeypatch):
    (a, b), policy = stores
    entered, release = Event(), Event()
    native_begin = Database.begin

    def gated(db, *args, **kwargs):
        if db is a:
            entered.set()
            assert release.wait(10)
        return native_begin(db, *args, **kwargs)

    with CatalogSession(a, owned=False, policy=policy) as session:
        session.attach_handle(b, alias="b", owned=False)
        monkeypatch.setattr(Database, "begin", gated)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(session.begin)
            try:
                assert entered.wait(5)
                with pytest.raises(GrafxTransactionStateError):
                    session.close()
                second = pool.submit(session.begin, catalog="b").result(timeout=5)
                second.rollback()
            finally:
                release.set()
            first.result(timeout=5).rollback()


def test_cleanup_attempts_all_and_preserves_first_failure(stores, monkeypatch):
    (a, b), policy = stores
    session = CatalogSession(a, owned=True, policy=policy)
    session.attach_handle(b, alias="b", owned=True)
    seen = []
    native_close = Database.close

    def close(db):
        seen.append(db)
        native_close(db)
        raise RuntimeError("first" if db is a else "second")

    with monkeypatch.context() as patch:
        patch.setattr(Database, "close", close)
        with pytest.raises(RuntimeError, match="first") as caught:
            session.close()
        assert "Additional catalog cleanup failure" in caught.value.__notes__[0]
        assert seen == [a, b]
        session.close()


def test_no_implicit_creation_and_denied_path(tmp_path):
    policy = CatalogPathPolicy((str(tmp_path),))
    with pytest.raises(GrafxConfigurationError):
        CatalogSession.open(tmp_path / "missing", policy=policy, read_only=False)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(GrafxConfigurationError):
        CatalogSession.open(empty, policy=policy, read_only=False)
    assert list(empty.iterdir()) == []
    with pytest.raises(GrafxConfigurationError):
        policy.resolve(tmp_path.parent)


def test_closed_handle_refused(stores):
    (a, _), policy = stores
    session = CatalogSession(a, owned=False, policy=policy)
    a.close()
    with pytest.raises(GrafxTransactionStateError):
        session.begin()
    session.close()


def test_pending_attach_is_bounded_and_close_refuses(stores, monkeypatch):
    import okto_grafx.catalogs as module

    (a, b), policy = stores
    bounded = CatalogPathPolicy(policy.allowed_roots, max_catalogs=2)
    entered, release = Event(), Event()

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        raise RuntimeError("open fault")

    with CatalogSession(a, owned=False, policy=bounded) as session:
        monkeypatch.setattr(module, "connect", delayed)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(session.attach, b.path, alias="b")
            try:
                assert entered.wait(5)
                with pytest.raises(GrafxConfigurationError):
                    session.attach_handle(b, alias="c", owned=False)
                with pytest.raises(GrafxTransactionStateError):
                    session.close()
                with session.begin() as tx:
                    assert tx.mode == "read"
            finally:
                release.set()
            with pytest.raises(RuntimeError, match="open fault"):
                pending.result(timeout=5)
        session.attach_handle(b, alias="b", owned=False)


def test_attach_capture_failure_releases_acquired_handle(stores, monkeypatch):
    import okto_grafx.catalogs as module

    (a, b), policy = stores
    session = CatalogSession(a, owned=False, policy=policy)
    native_capture = CatalogSession._capture

    def fail_capture(self, database, **kwargs):
        if database is b:
            raise RuntimeError("capture fault")
        return native_capture(self, database, **kwargs)

    monkeypatch.setattr(module, "connect", lambda *args, **kwargs: b)
    monkeypatch.setattr(CatalogSession, "_capture", fail_capture)
    with pytest.raises(RuntimeError, match="capture fault"):
        session.attach(b.path, alias="b")
    assert b.closed
    assert len(session.catalogs()) == 1
    session.close()


def test_in_use_default_detach_and_failed_close_preserve_registry(stores):
    (a, b), policy = stores
    with CatalogSession(a, owned=False, policy=policy) as session:
        session.attach_handle(b, alias="b", owned=False)
        session.use("b")
        with session.begin():
            with pytest.raises(GrafxTransactionStateError):
                session.detach("b")
            with pytest.raises(GrafxTransactionStateError):
                session.close()
            assert len(session.catalogs()) == 2
        session.detach("b")
        with session.begin() as tx:
            assert tx.execute("MATCH (n:N) RETURN n.id").rows == ((0,),)


def test_directory_identity_change_refused_before_native_begin(stores, monkeypatch):
    import okto_grafx.catalogs as module

    (a, _), policy = stores
    with CatalogSession(a, owned=False, policy=policy) as session:
        monkeypatch.setattr(module, "_directory_identity", lambda path: (-1, -1))
        with pytest.raises(GrafxConfigurationError):
            session.begin()
        assert session.catalogs()[0].active_transactions == 0


def test_uncheckpointed_read_only_open_refuses_without_recovery(stores):
    (a, _), policy = stores
    a.close()
    from okto_grafx.errors import GrafxUnsupportedOperation

    with pytest.raises(GrafxUnsupportedOperation):
        CatalogSession.open(a.path, policy=policy)


def test_body_exception_survives_close_refusal(stores):
    (a, _), policy = stores
    session = CatalogSession(a, owned=False, policy=policy)
    with pytest.raises(ValueError, match="body") as caught:
        with session:
            tx = session.begin()
            raise ValueError("body")
    assert "CatalogSession cleanup failed" in caught.value.__notes__[0]
    tx.rollback()
    session.close()


@pytest.mark.parametrize(
    "option",
    [
        {"allowed_roots": ()},
        {"max_catalogs": True},
        {"max_catalogs": 0},
        {"max_active_transactions": 4097},
    ],
)
def test_policy_bounds(tmp_path, option):
    values = dict(allowed_roots=(str(tmp_path),))
    values.update(option)
    with pytest.raises(GrafxConfigurationError):
        CatalogPathPolicy(**values)


def test_two_processes_commit_to_explicit_catalogs(stores):
    import os
    from pathlib import Path
    import subprocess
    import sys

    (a, b), policy = stores
    script = """
import sys
from okto_grafx import connect, CatalogSession, CatalogPathPolicy
root, a_path, b_path, alias, value = sys.argv[1:]
with connect(a_path) as a, connect(b_path) as b:
    with CatalogSession(a, owned=False, policy=CatalogPathPolicy((root,)), read_only=False) as s:
        s.attach_handle(b, alias="b", owned=False, read_only=False)
        with s.begin("write", catalog=alias) as tx:
            tx.execute("CREATE (:N {id:$id})", {"id": int(value)})
"""
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))

    def run(alias, value):
        return subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                policy.allowed_roots[0],
                a.path,
                b.path,
                alias,
                str(value),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda args: run(*args), (("main", 11), ("b", 22))))
    assert all(result.returncode == 0 for result in results), [
        r.stderr for r in results
    ]
    assert a.execute("MATCH (n:N) RETURN n.id ORDER BY n.id").rows == ((0,), (11,))
    assert b.execute("MATCH (n:N) RETURN n.id ORDER BY n.id").rows == ((1,), (22,))


def test_pending_detach_cleanup_remains_bounded(stores, monkeypatch):
    (a, b), policy = stores
    policy = CatalogPathPolicy(policy.allowed_roots, max_catalogs=2)
    entered, release = Event(), Event()
    native_close = Database.close

    def slow_close(db):
        if db is b:
            entered.set()
            assert release.wait(10)
        native_close(db)

    with CatalogSession(a, owned=False, policy=policy) as session:
        session.attach_handle(b, alias="b", owned=True)
        with monkeypatch.context() as patch:
            patch.setattr(Database, "close", slow_close)
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(session.detach, "b")
                try:
                    assert entered.wait(5)
                    with pytest.raises(GrafxConfigurationError):
                        session.attach_handle(b, alias="other", owned=False)
                    with pytest.raises(GrafxTransactionStateError):
                        session.close()
                    with session.begin():
                        pass
                finally:
                    release.set()
                pending.result(timeout=5)
        assert b.closed
        assert len(session.catalogs()) == 1
