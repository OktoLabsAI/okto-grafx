"""Adversarial regressions for speculative DDL artifact provenance.

Catalog OCC decides which schema becomes durable.  Index files, the process-local index
registry, the vector space map and the unindexed-table diagnostic are installed before that
decision, however, so a losing transaction may unwind only effects it still owns.  These tests
exercise that rule through the public database/transaction doors and require both the live
participant and a cold reopen to retain the winner's content.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxIndexError, GrafxWriteConflict


_OPTIONS = {"page_size": 512, "checkpoint_interval_records": 1_000_000}

_SAME_DEFINITION_CHILD = r'''
import sys
sys.path.insert(0, sys.argv[2])
from okto_grafx import connect

with connect(sys.argv[1], page_size=512, checkpoint_interval_records=1_000_000) as db:
    with db.begin("write") as txn:
        txn.execute("CREATE NODE TABLE Decision(id INT64, PRIMARY KEY(id))")
        txn.execute("CREATE (:Decision {id: 7})")
'''

_SAME_VECTOR_DEFINITION_CHILD = r'''
import sys
sys.path.insert(0, sys.argv[2])
from okto_grafx import connect

with connect(
    sys.argv[1],
    page_size=512,
    checkpoint_interval_records=1_000_000,
    vector_exact_scan_threshold=64,
) as db:
    with db.begin("write") as txn:
        txn.execute("CREATE VECTOR SPACE s {dimension: 2, metric: 'cosine'}")
        txn.execute(
            "CREATE NODE TABLE Decision("
            "id INT64, embedding VECTOR(s), PRIMARY KEY(id))"
        )
        txn.execute("CREATE (:Decision {id: 7, embedding: [1.0, 0.0]})")
'''

_DIFFERENT_PRIMARY_KEY_CHILD = r'''
import sys
sys.path.insert(0, sys.argv[2])
from okto_grafx import connect

with connect(sys.argv[1], page_size=512, checkpoint_interval_records=1_000_000) as db:
    with db.begin("write") as txn:
        txn.execute(
            "CREATE NODE TABLE Decision("
            "business_key STRING, id INT64, PRIMARY KEY(id))"
        )
        txn.execute("CREATE (:Decision {business_key: 'foreign', id: 7})")
'''

_SKIPPED_INDEX_CHILD = r'''
import sys
sys.path.insert(0, sys.argv[2])
from okto_grafx import connect

with connect(sys.argv[1], page_size=512, checkpoint_interval_records=1_000_000) as db:
    with db.begin("write") as txn:
        txn.execute("CREATE NODE TABLE Decision(id INT64, PRIMARY KEY(id))")
        txn.execute("CREATE (:Decision {id: 7})")
'''


def _operators(database: object, statement: str) -> set[str]:
    """Return public plan operator names without retaining engine collaborators."""

    seen: set[str] = set()
    stack = [database.explain(statement)]
    while stack:
        node = stack.pop()
        seen.add(type(node).__name__)
        children = getattr(node, "children", None)
        stack.extend(children() if callable(children) else (children or ()))
    return seen


def _source_root() -> str:
    """Return this worktree's ``src`` directory for isolated child imports."""

    return str(Path(connect.__code__.co_filename).resolve().parents[2])


def _settle_loser(loser: object) -> None:
    """Prove OCC refuses the stale catalog, then explicitly drive its rollback journal."""

    with pytest.raises(GrafxWriteConflict):
        loser.commit()
    assert loser.active
    loser.rollback()


def _assert_scalar_winner(database: object, identity: int = 7) -> None:
    """Assert scan, exact lookup, registry and verification all describe the winner."""

    assert database.storage.exists("index/pk_Decision.idx")
    indexes = tuple(database.indexes.indexes())
    assert tuple(index.name for index in indexes) == ("pk_Decision",)
    assert indexes[0].definition.positions == (0,)
    assert database.execute("MATCH (d:Decision) RETURN d.id").rows == ((identity,),)
    assert database.execute(
        f"MATCH (d:Decision) WHERE d.id = {identity} RETURN d.id"
    ).rows == ((identity,),)
    report = database.verify("all")
    assert report.clean is True
    assert report.findings == ()


def test_same_process_loser_cannot_remove_an_identical_winners_index(
    tmp_path: Path,
) -> None:
    """An adopted same-name object/file belongs to B after B's durable commit."""

    root = tmp_path / "same-process-identical"
    database = connect(root, **_OPTIONS)
    loser = database.begin("write")
    winner = database.begin("write")
    try:
        loser.execute("CREATE NODE TABLE Decision(id INT64, PRIMARY KEY(id))")
        winner.execute("CREATE NODE TABLE Decision(id INT64, PRIMARY KEY(id))")
        winner.execute("CREATE (:Decision {id: 7})")
        winner.commit()

        _settle_loser(loser)
        _assert_scalar_winner(database)
    finally:
        if winner.active:
            winner.rollback()
        if loser.active:
            loser.rollback()
        database.close()

    with connect(root, **_OPTIONS) as cold:
        _assert_scalar_winner(cold)


def test_identical_scalar_claims_release_the_registry_after_both_roll_back(
    tmp_path: Path,
) -> None:
    """The last identical adopter releases an unchanged artifact nobody published."""

    root = tmp_path / "same-process-identical-scalar-both-rollback"
    database = connect(root, **_OPTIONS)
    installer = database.begin("write")
    adopter = database.begin("write")
    try:
        for transaction in (installer, adopter):
            transaction.execute(
                "CREATE NODE TABLE Decision(id INT64, PRIMARY KEY(id))"
            )

        shared = database._indexes.index("pk_Decision")
        installer.rollback()
        assert database._indexes.index("pk_Decision") is shared

        adopter.rollback()
        with pytest.raises(GrafxIndexError):
            database._indexes.index("pk_Decision")
        assert database._indexes._artifact_claims == {}
        assert database._indexes.indexes() == ()
    finally:
        if adopter.active:
            adopter.rollback()
        if installer.active:
            installer.rollback()
        database.close()

    with connect(root, **_OPTIONS) as cold:
        assert cold.indexes.indexes() == ()
        with pytest.raises(GrafxIndexError):
            cold._indexes.index("pk_Decision")


def test_same_process_identical_vector_winner_keeps_its_adopted_space_map(
    tmp_path: Path,
) -> None:
    """The original map installer cannot detach an object another transaction committed."""

    root = tmp_path / "same-process-identical-vector-map"
    database = connect(root, vector_exact_scan_threshold=64, **_OPTIONS)
    loser = database.begin("write")
    winner = database.begin("write")
    try:
        for transaction in (loser, winner):
            transaction.execute(
                "CREATE VECTOR SPACE s {dimension: 2, metric: 'cosine'}"
            )
            transaction.execute(
                "CREATE NODE TABLE Decision("
                "id INT64, embedding VECTOR(s), PRIMARY KEY(id))"
            )

        # Both DDL statements deliberately adopted one process-local object.  Object identity
        # therefore cannot also mean exclusive rollback ownership: after the winner publishes,
        # the installer's journal is older than the durable adoption of that same object.
        assert database._vectors.index("s") is database._indexes.index(
            "vector_Decision_s"
        )
        winner.execute(
            "CREATE (:Decision {id: 7, embedding: [1.0, 0.0]})"
        )
        winner.commit()
        assert database.execute(
            "MATCH (d:Decision) WHERE "
            "similarity(d.embedding, $q, space => 's') > 0.5 RETURN d.id",
            {"q": [1.0, 0.0]},
        ).rows == ((7,),)

        _settle_loser(loser)

        assert database.vectors.index("s").name == "vector_Decision_s"
        assert database.execute(
            "MATCH (d:Decision) WHERE "
            "similarity(d.embedding, $q, space => 's') > 0.5 RETURN d.id",
            {"q": [1.0, 0.0]},
        ).rows == ((7,),)
        assert database.verify("all").findings == ()
    finally:
        if winner.active:
            winner.rollback()
        if loser.active:
            loser.rollback()
        database.close()

    with connect(root, vector_exact_scan_threshold=64, **_OPTIONS) as cold:
        assert cold.vectors.index("s").name == "vector_Decision_s"
        assert cold.execute(
            "MATCH (d:Decision) WHERE "
            "similarity(d.embedding, $q, space => 's') > 0.5 RETURN d.id",
            {"q": [1.0, 0.0]},
        ).rows == ((7,),)
        assert cold.verify("all").findings == ()


def test_identical_vector_adopter_keeps_the_map_when_installer_rolls_back_first(
    tmp_path: Path,
) -> None:
    """An active adopter's claim outlives the original map installer's rollback."""

    root = tmp_path / "same-process-identical-vector-active-adopter"
    database = connect(root, vector_exact_scan_threshold=64, **_OPTIONS)
    installer = database.begin("write")
    adopter = database.begin("write")
    try:
        for transaction in (installer, adopter):
            transaction.execute(
                "CREATE VECTOR SPACE s {dimension: 2, metric: 'cosine'}"
            )
            transaction.execute(
                "CREATE NODE TABLE Decision("
                "id INT64, embedding VECTOR(s), PRIMARY KEY(id))"
            )

        shared = database._vectors.index("s")
        assert shared is database._indexes.index("vector_Decision_s")

        # Nothing is durable yet.  The second transaction's live adoption claim, rather than a
        # committed-catalog predicate, is what must stop the original installer's unwind from
        # detaching the one map both transactions use.
        installer.rollback()
        assert database._vectors.index("s") is shared

        adopter.execute(
            "CREATE (:Decision {id: 7, embedding: [1.0, 0.0]})"
        )
        adopter.commit()

        assert database.vectors.index("s").name == "vector_Decision_s"
        assert database.execute(
            "MATCH (d:Decision) WHERE "
            "similarity(d.embedding, $q, space => 's') > 0.5 RETURN d.id",
            {"q": [1.0, 0.0]},
        ).rows == ((7,),)
        assert database.verify("all").findings == ()
    finally:
        if adopter.active:
            adopter.rollback()
        if installer.active:
            installer.rollback()
        database.close()

    with connect(root, vector_exact_scan_threshold=64, **_OPTIONS) as cold:
        assert cold.vectors.index("s").name == "vector_Decision_s"
        assert cold.execute(
            "MATCH (d:Decision) WHERE "
            "similarity(d.embedding, $q, space => 's') > 0.5 RETURN d.id",
            {"q": [1.0, 0.0]},
        ).rows == ((7,),)
        assert cold.verify("all").findings == ()


def test_identical_vector_claims_release_the_map_after_both_roll_back(
    tmp_path: Path,
) -> None:
    """The final identical adopter release must not restore its own attached object."""

    root = tmp_path / "same-process-identical-vector-both-rollback"
    database = connect(root, vector_exact_scan_threshold=64, **_OPTIONS)
    installer = database.begin("write")
    adopter = database.begin("write")
    try:
        for transaction in (installer, adopter):
            transaction.execute(
                "CREATE VECTOR SPACE s {dimension: 2, metric: 'cosine'}"
            )
            transaction.execute(
                "CREATE NODE TABLE Decision("
                "id INT64, embedding VECTOR(s), PRIMARY KEY(id))"
            )

        shared = database._vectors.index("s")
        installer.rollback()
        assert database._vectors.index("s") is shared

        adopter.rollback()
        with pytest.raises(GrafxIndexError):
            database._vectors.index("s")
        assert database._vectors._by_space == {}
        assert database._vectors._map_claims == {}
        assert database._vectors._durable_by_space == {}
        assert database._indexes._artifact_claims == {}
        assert database._indexes.indexes() == ()
    finally:
        if adopter.active:
            adopter.rollback()
        if installer.active:
            installer.rollback()
        database.close()

    with connect(root, vector_exact_scan_threshold=64, **_OPTIONS) as cold:
        assert cold.vectors.spaces() == ()
        assert cold.vectors.indexes() == ()
        with pytest.raises(GrafxIndexError):
            cold._vectors.index("s")


def test_cross_process_loser_cannot_remove_an_identical_winners_index(
    tmp_path: Path,
) -> None:
    """The ownership rule also holds when the adopting winner has another registry."""

    root = tmp_path / "cross-process-identical"
    source = _source_root()
    database = connect(root, **_OPTIONS)
    loser = database.begin("write")
    try:
        loser.execute("CREATE NODE TABLE Decision(id INT64, PRIMARY KEY(id))")
        child = subprocess.run(
            [sys.executable, "-c", _SAME_DEFINITION_CHILD, str(root), source],
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert child.returncode == 0, child.stderr[-2_000:]

        _settle_loser(loser)
        _assert_scalar_winner(database)
    finally:
        if loser.active:
            loser.rollback()
        database.close()

    with connect(root, **_OPTIONS) as cold:
        _assert_scalar_winner(cold)


def test_cross_process_identical_vector_winner_keeps_the_local_space_map(
    tmp_path: Path,
) -> None:
    """A foreign identical winner survives the stale local installer's unwind."""

    root = tmp_path / "cross-process-identical-vector-map"
    source = _source_root()
    database = connect(root, vector_exact_scan_threshold=64, **_OPTIONS)
    loser = database.begin("write")
    try:
        loser.execute("CREATE VECTOR SPACE s {dimension: 2, metric: 'cosine'}")
        loser.execute(
            "CREATE NODE TABLE Decision("
            "id INT64, embedding VECTOR(s), PRIMARY KEY(id))"
        )
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                _SAME_VECTOR_DEFINITION_CHILD,
                str(root),
                source,
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert child.returncode == 0, child.stderr[-2_000:]

        _settle_loser(loser)
        assert database.vectors.index("s").name == "vector_Decision_s"
        assert database.execute(
            "MATCH (d:Decision) WHERE "
            "similarity(d.embedding, $q, space => 's') > 0.5 RETURN d.id",
            {"q": [1.0, 0.0]},
        ).rows == ((7,),)
        assert database.verify("all").findings == ()
    finally:
        if loser.active:
            loser.rollback()
        database.close()

    with connect(root, vector_exact_scan_threshold=64, **_OPTIONS) as cold:
        assert cold.vectors.index("s").name == "vector_Decision_s"
        assert cold.execute(
            "MATCH (d:Decision) WHERE "
            "similarity(d.embedding, $q, space => 's') > 0.5 RETURN d.id",
            {"q": [1.0, 0.0]},
        ).rows == ((7,),)
        assert cold.verify("all").findings == ()


def test_same_identity_with_a_different_primary_key_never_exposes_the_loser(
    tmp_path: Path,
) -> None:
    """A table id/name pair is not enough provenance when key positions differ."""

    root = tmp_path / "different-primary-key"
    database = connect(root, **_OPTIONS)
    loser = database.begin("write")
    winner = database.begin("write")
    try:
        loser.execute(
            "CREATE NODE TABLE Decision(business_key STRING, id INT64, "
            "PRIMARY KEY(business_key))"
        )
        winner.execute(
            "CREATE NODE TABLE Decision(business_key STRING, id INT64, PRIMARY KEY(id))"
        )
        winner.execute("CREATE (:Decision {business_key: 'winner', id: 7})")
        winner.commit()

        # Before the loser's cleanup, a registry entry with position zero is still speculative.
        # It may be withheld, but it may never be published or selected for the durable table.
        assert all(
            index.definition.positions != (0,)
            for index in database.indexes.indexes()
            if index.name == "pk_Decision"
        )
        assert "IndexSeek" not in _operators(
            database,
            "MATCH (d:Decision) WHERE d.business_key = 'winner' RETURN d.id",
        )
        assert database.execute(
            "MATCH (d:Decision) WHERE d.id = 7 RETURN d.business_key, d.id"
        ).rows == (("winner", 7),)
        coexistence = database.verify("all")
        assert coexistence.clean is True
        assert coexistence.findings == ()

        _settle_loser(loser)
        assert all(
            index.definition.positions == (1,)
            for index in database.indexes.indexes()
            if index.name == "pk_Decision"
        )
        assert database.execute(
            "MATCH (d:Decision) RETURN d.business_key, d.id"
        ).rows == (("winner", 7),)
        assert database.verify("all").findings == ()
    finally:
        if winner.active:
            winner.rollback()
        if loser.active:
            loser.rollback()
        database.close()

    with connect(root, **_OPTIONS) as cold:
        definition = cold.indexes.index("pk_Decision").definition
        assert definition.positions == (1,)
        assert "IndexSeek" in _operators(
            cold, "MATCH (d:Decision) WHERE d.id = 7 RETURN d.id"
        )
        assert "IndexSeek" not in _operators(
            cold,
            "MATCH (d:Decision) WHERE d.business_key = 'winner' RETURN d.id",
        )
        assert cold.execute(
            "MATCH (d:Decision) WHERE d.id = 7 RETURN d.business_key, d.id"
        ).rows == (("winner", 7),)
        assert cold.verify("all").findings == ()


def test_foreign_durable_definition_replaces_local_speculation_before_writer_wal(
    tmp_path: Path,
) -> None:
    """A local DML commit must sync P before replay-floor checks can inspect Q."""

    root = tmp_path / "foreign-definition-writer-sync"
    source = _source_root()
    database = connect(root, **_OPTIONS)
    loser = database.begin("write")
    writer = None
    successor = None
    try:
        loser.execute(
            "CREATE NODE TABLE Decision("
            "business_key STRING, id INT64, PRIMARY KEY(business_key))"
        )
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                _DIFFERENT_PRIMARY_KEY_CHILD,
                str(root),
                source,
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert child.returncode == 0, child.stderr[-2_000:]

        # Refreshing the durable catalog may withhold P until the existing-only sync, but it
        # must never expose Q merely because P reused Q's provisional numeric id and name.
        assert database.execute(
            "MATCH (d:Decision) WHERE d.id = 7 RETURN d.business_key, d.id"
        ).rows == (("foreign", 7),)
        assert all(
            index.definition.positions != (0,)
            for index in database.indexes.indexes()
            if index.name == "pk_Decision"
        )
        assert database.verify("all").findings == ()

        # This is the existing-only synchronisation gate in a real workload.  P must replace Q
        # before any WAL byte is appended; otherwise replay-floor validation opens P's file
        # through Q's definition after the commit has already become durable.
        writer = database.begin("write")
        writer.execute("CREATE (:Decision {business_key: 'local', id: 8})")
        writer.commit()
        assert writer.active is False
        assert database.indexes.index("pk_Decision").definition.positions == (1,)
        assert database.execute(
            "MATCH (d:Decision) WHERE d.id = 8 RETURN d.business_key, d.id"
        ).rows == (("local", 8),)
        assert database.verify("all").findings == ()

        # Q's stale catalog snapshot must win the normal first OCC refusal before its displaced
        # physical artifact can reach WAL construction.  The refusal stays an ordinary retry
        # invitation: it increments the conflict count, leaves Q active and appends no byte.
        before_wal = (database.wal.last_lsn, database.wal.total_bytes())
        with pytest.raises(GrafxWriteConflict) as refusal:
            loser.commit()
        assert refusal.value.retryable is True
        assert loser.active
        assert loser._context.conflicts == 1
        assert (database.wal.last_lsn, database.wal.total_bytes()) == before_wal

        successor = database.retry(loser)
        assert loser.active is False
        assert successor.active
        assert successor._context.conflicts == 1
        successor.rollback()
        assert database.indexes.index("pk_Decision").definition.positions == (1,)
        assert set(
            database.execute(
                "MATCH (d:Decision) RETURN d.business_key, d.id"
            ).rows
        ) == {("foreign", 7), ("local", 8)}
        assert database.verify("all").findings == ()
    finally:
        if successor is not None and successor.active:
            try:
                successor.rollback()
            except Exception:
                pass
        if writer is not None and writer.active:
            try:
                writer.rollback()
            except Exception:
                pass
        if loser.active:
            try:
                loser.rollback()
            except Exception:
                pass
        try:
            database.close()
        except Exception:
            pass

    with connect(root, **_OPTIONS) as cold:
        assert cold.indexes.index("pk_Decision").definition.positions == (1,)
        assert set(
            cold.execute(
                "MATCH (d:Decision) RETURN d.business_key, d.id"
            ).rows
        ) == {("foreign", 7), ("local", 8)}
        assert cold.verify("all").findings == ()


def test_displaced_local_ddl_artifact_is_a_pre_wal_retryable_conflict(
    tmp_path: Path,
) -> None:
    """Physical provenance loss with no catalog winner uses the normal retry lifecycle."""

    root = tmp_path / "local-artifact-conflict"
    database = connect(root, **_OPTIONS)
    displaced = database.begin("write")
    replacement = database.begin("write")
    successor = None
    try:
        displaced.execute(
            "CREATE NODE TABLE Decision("
            "business_key STRING, id INT64, PRIMARY KEY(business_key))"
        )
        displaced.execute(
            "CREATE (:Decision {business_key: 'displaced', id: 6})"
        )
        replacement.execute(
            "CREATE NODE TABLE Decision("
            "business_key STRING, id INT64, PRIMARY KEY(id))"
        )
        replacement.execute(
            "CREATE (:Decision {business_key: 'replacement', id: 7})"
        )

        # No catalog transaction has committed, so logical OCC alone cannot reject the first
        # DDL.  B has nevertheless displaced A's canonical nonce/registry object.  That second,
        # physical OCC gate must translate provenance loss into the same public retry lifecycle
        # before a WAL record belonging to A exists.
        before_wal = (database.wal.last_lsn, database.wal.total_bytes())
        with pytest.raises(GrafxWriteConflict) as refusal:
            displaced.commit()
        assert refusal.value.retryable is True
        assert displaced.active
        assert displaced._context.conflicts == 1
        assert (database.wal.last_lsn, database.wal.total_bytes()) == before_wal

        successor = database.retry(displaced)
        assert displaced.active is False
        assert successor.active
        assert successor._context.conflicts == 1
        successor.rollback()

        replacement.commit()
        assert database.indexes.index("pk_Decision").definition.positions == (1,)
        assert database.execute(
            "MATCH (d:Decision) WHERE d.id = 7 RETURN d.business_key, d.id"
        ).rows == (("replacement", 7),)
        assert database.verify("all").findings == ()
    finally:
        if successor is not None and successor.active:
            successor.rollback()
        if replacement.active:
            replacement.rollback()
        if displaced.active:
            displaced.rollback()
        database.close()

    with connect(root, **_OPTIONS) as cold:
        assert cold.indexes.index("pk_Decision").definition.positions == (1,)
        assert cold.execute(
            "MATCH (d:Decision) WHERE d.id = 7 RETURN d.business_key, d.id"
        ).rows == (("replacement", 7),)
        assert cold.verify("all").findings == ()


def test_vector_adoption_requires_the_complete_embedding_space_definition(
    tmp_path: Path,
) -> None:
    """Equal table/index definitions do not imply equal vector runtime semantics."""

    root = tmp_path / "different-vector-space"
    database = connect(root, vector_exact_scan_threshold=64, **_OPTIONS)
    loser = database.begin("write")
    winner = database.begin("write")
    try:
        loser.execute(
            "CREATE VECTOR SPACE s {dimension: 2, metric: 'cosine', "
            "storage_dtype: 'float32'}"
        )
        loser.execute(
            "CREATE NODE TABLE Decision(id INT64, embedding VECTOR(s), PRIMARY KEY(id))"
        )

        winner.execute(
            "CREATE VECTOR SPACE s {dimension: 3, metric: 'euclidean', "
            "storage_dtype: 'float64'}"
        )
        winner.execute(
            "CREATE NODE TABLE Decision(id INT64, embedding VECTOR(s), PRIMARY KEY(id))"
        )
        winner.execute("CREATE (:Decision {id: 7, embedding: [1.0, 0.0, 0.0]})")
        winner.commit()

        # This snapshot is taken before Q unwinds, so it catches both an exposed Q and a mapping
        # that names P but still carries Q's runtime dimension/metric/dtype.
        space = database.vectors.space("s")
        vector_index = database.vectors.index("s")
        assert (space.dimension, space.metric.value, space.storage_dtype) == (
            3,
            "euclidean",
            "float64",
        )
        assert (
            vector_index.dimension,
            vector_index.metric_of_space.value,
            vector_index.storage_dtype,
        ) == (3, "euclidean", "float64")

        _settle_loser(loser)
        reader = database.begin("read")
        try:
            result = database.search_vectors(
                reader, space="s", query=(1.0, 0.0, 0.0), k=1
            )
        finally:
            reader.rollback()
        assert result.achieved_k == 1
        assert len(result.hits) == 1
        assert database.execute(
            "MATCH (d:Decision) WHERE d.id = 7 RETURN d.id"
        ).rows == ((7,),)
        assert database.verify("all").findings == ()
    finally:
        if winner.active:
            winner.rollback()
        if loser.active:
            loser.rollback()
        database.close()

    with connect(root, vector_exact_scan_threshold=64, **_OPTIONS) as cold:
        space = cold.vectors.space("s")
        vector_index = cold.vectors.index("s")
        assert (space.dimension, space.metric.value, space.storage_dtype) == (
            3,
            "euclidean",
            "float64",
        )
        assert (
            vector_index.dimension,
            vector_index.metric_of_space.value,
            vector_index.storage_dtype,
        ) == (3, "euclidean", "float64")
        reader = cold.begin("read")
        try:
            result = cold.search_vectors(
                reader, space="s", query=(1.0, 0.0, 0.0), k=1
            )
        finally:
            reader.rollback()
        assert result.achieved_k == 1
        assert len(result.hits) == 1
        assert cold.verify("all").findings == ()


def test_partial_multi_index_statement_releases_its_adopted_observation(
    tmp_path: Path,
) -> None:
    """Failure after PK adoption must not make an unrelated commit adopt that PK."""

    root = tmp_path / "partial-multi-index-unwind"
    long_space = "s" * 120
    database = connect(root, **_OPTIONS)
    owner = database.begin("write")
    partial = database.begin("write")
    try:
        owner.execute("CREATE NODE TABLE Decision(id INT64, PRIMARY KEY(id))")
        partial.execute(
            f"CREATE VECTOR SPACE {long_space} "
            "{dimension: 2, metric: 'cosine'}"
        )
        # pk_Decision is equivalent to the owner's first index and is staged first.  The vector
        # index name then exceeds the identifier budget, forcing statement-level reverse unwind.
        with pytest.raises(GrafxIndexError):
            partial.execute(
                "CREATE NODE TABLE Decision("
                f"id INT64, embedding VECTOR({long_space}), PRIMARY KEY(id))"
            )
        assert partial.active

        partial.execute("CREATE NODE TABLE Other(id INT64, PRIMARY KEY(id))")
        partial.execute("CREATE (:Other {id: 11})")
        partial.commit()
        _settle_loser(owner)

        # Preserved orphan bytes are allowed, but no Decision registration may be exposed for a
        # statement that never installed Decision.  The unrelated durable table remains indexed.
        assert tuple(index.name for index in database.indexes.indexes()) == ("pk_Other",)
        assert database.execute("MATCH (o:Other) WHERE o.id = 11 RETURN o.id").rows == (
            (11,),
        )
        assert database.verify("all").findings == ()

        # A later, incompatible definition must be able to reclaim the canonical name rather
        # than being refused forever by the preserved bytes.
        with database.begin("write") as retry:
            retry.execute(
                "CREATE NODE TABLE Decision("
                "business_key STRING, id INT64, PRIMARY KEY(id))"
            )
            retry.execute("CREATE (:Decision {business_key: 'new', id: 22})")
        assert tuple(index.name for index in database.indexes.indexes()) == (
            "pk_Decision",
            "pk_Other",
        )
        assert database.indexes.index("pk_Decision").definition.positions == (1,)
        assert database.execute(
            "MATCH (d:Decision) WHERE d.id = 22 RETURN d.business_key, d.id"
        ).rows == (("new", 22),)
        assert database.verify("all").findings == ()
    finally:
        if partial.active:
            partial.rollback()
        if owner.active:
            owner.rollback()
        database.close()

    with connect(root, **_OPTIONS) as cold:
        assert set(cold.attached_indexes) == {"pk_Decision", "pk_Other"}
        assert tuple(index.name for index in cold.indexes.indexes()) == (
            "pk_Decision",
            "pk_Other",
        )
        assert cold.indexes.index("pk_Decision").definition.positions == (1,)
        assert cold.execute("MATCH (o:Other) WHERE o.id = 11 RETURN o.id").rows == (
            (11,),
        )
        assert cold.execute(
            "MATCH (d:Decision) WHERE d.id = 22 RETURN d.business_key, d.id"
        ).rows == (("new", 22),)
        assert cold.verify("all").findings == ()


@pytest.mark.parametrize("cross_process", [False, True], ids=["same-process", "cross-process"])
def test_loser_rollback_cannot_erase_another_transactions_skip_claim(
    tmp_path: Path, cross_process: bool
) -> None:
    """Case-folded index-name decline is owned by every durable unindexed table."""

    root = tmp_path / "skip-claim"
    database = connect(root, **_OPTIONS)
    loser = None
    winner = None
    try:
        with database.begin("write") as seed:
            seed.execute(
                "CREATE NODE TABLE decision(id INT64, PRIMARY KEY(id))"
            )

        loser = database.begin("write")
        loser.execute("CREATE NODE TABLE Decision(id INT64, PRIMARY KEY(id))")
        if cross_process:
            source = _source_root()
            child = subprocess.run(
                [sys.executable, "-c", _SKIPPED_INDEX_CHILD, str(root), source],
                capture_output=True,
                text=True,
                timeout=180,
            )
            assert child.returncode == 0, child.stderr[-2_000:]
        else:
            winner = database.begin("write")
            winner.execute("CREATE NODE TABLE Decision(id INT64, PRIMARY KEY(id))")
            winner.execute("CREATE (:Decision {id: 7})")
            winner.commit()

        assert "Decision" in database.queries.skipped_indexes
        _settle_loser(loser)
        assert "Decision" in database.queries.skipped_indexes
        assert database.execute(
            "MATCH (d:Decision) WHERE d.id = 7 RETURN d.id"
        ).rows == ((7,),)
        assert database.verify("all").findings == ()
    finally:
        if winner is not None and winner.active:
            winner.rollback()
        if loser is not None and loser.active:
            loser.rollback()
        database.close()

    with connect(root, **_OPTIONS) as cold:
        assert "Decision" in cold.unindexed_tables
        assert cold.execute(
            "MATCH (d:Decision) WHERE d.id = 7 RETURN d.id"
        ).rows == ((7,),)
        assert cold.verify("all").findings == ()
