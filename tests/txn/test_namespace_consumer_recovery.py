"""Physical metadata ownership survives COMMIT boundaries without sibling effects."""

import subprocess
import sys

import pytest

from okto_grafx import connect
from okto_grafx.migrations import SchemaMigration, migrate_schema


WORKER = r'''
import os, sys
from okto_grafx import connect
from okto_grafx.engine.database import Transaction
from okto_grafx.engine.txn_manager import TransactionManager
from okto_grafx.migrations import SchemaMigration, migrate_schema

path, codec, operation, cut = sys.argv[1:]
with connect(path, codec=codec) as db:
    original_commit = Transaction.commit
    original_apply = TransactionManager._apply_images
    active = None
    def commit(tx):
        global active
        active = tx
        if cut == 'before_commit':
            os._exit(71)
        return original_commit(tx)
    def apply(manager, images):
        if active is not None and active._context.state.value == 'committed':
            os._exit(73)
        return original_apply(manager, images)
    Transaction.commit = commit
    TransactionManager._apply_images = apply
    if operation == 'view':
        db.views.create('both', query='MATCH(n:R)-[e:R]->(m:R) RETURN n.id,e.body,m.id')
    else:
        migrate_schema(db, (SchemaMigration(1, ('CREATE NODE TABLE Created(id INT64,PRIMARY KEY(id))',)),),
                       namespace='app')
raise AssertionError('cut not reached')
'''


@pytest.mark.parametrize("codec", ["pure", "numpy"])
@pytest.mark.parametrize("operation", ["view", "migration"])
@pytest.mark.parametrize("cut,exit_code,committed", [("before_commit", 71, False), ("before_apply", 73, True)])
def test_namespace_consumers_recover_atomic_owned_metadata(tmp_path, codec, operation, cut, exit_code, committed):
    path = tmp_path / "db"
    with connect(path, codec=codec) as db:
        db.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE R(id INT64,PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE R(FROM R TO R,body STRING)")
            tx.execute("CREATE(a:R {id:1})-[:R {body:'edge'}]->(a)")
            for name in ("_grafx_views_v1", "_grafx_migrations_app"):
                tx.execute(f"CREATE REL TABLE {name}(FROM R TO R,body STRING)")
                tx.execute(f"MATCH(n:R) CREATE(n)-[:{name} {{body:'unowned-sibling'}}]->(n)")
        db.views.prepare()
        db.checkpoint()
    child = subprocess.run([sys.executable, "-c", WORKER, str(path), codec, operation, cut],
                           capture_output=True, text=True, timeout=90)
    assert child.returncode == exit_code, child.stderr
    plan = (SchemaMigration(1, ("CREATE NODE TABLE Created(id INT64,PRIMARY KEY(id))",)),)
    with connect(path, codec=codec) as db:
        if operation == "view":
            saved = db.views.get("both")
            assert (saved is not None) is committed
            if not committed:
                saved = db.views.create("both", query="MATCH(n:R)-[e:R]->(m:R) RETURN n.id,e.body,m.id")
            assert len(saved.dependencies) == 2
            assert db.views.execute("both").rows == ((1, "edge", 1),)
        else:
            assert db.catalog.catalog.has_table("Created", kind="node") is committed
            preview = migrate_schema(db, plan, namespace="app", dry_run=True)
            assert preview.previously_applied == ((1,) if committed else ())
            assert migrate_schema(db, plan, namespace="app").applied == (() if committed else (1,))
        for name in ("_grafx_views_v1", "_grafx_migrations_app"):
            assert db.execute(f"MATCH(:R)-[e:{name}]->(:R) RETURN e.body").rows == (("unowned-sibling",),)
        assert db.verify("all").findings == ()
        db.checkpoint()
    for _ in range(2):
        with connect(path, codec=codec, read_only=True) as db:
            if operation == "view":
                assert db.views.execute("both").rows == ((1, "edge", 1),)
            else:
                assert migrate_schema(db, plan, namespace="app", dry_run=True).previously_applied == (1,)
            assert db.verify("all").findings == ()
