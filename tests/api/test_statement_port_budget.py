"""Per-statement budgets at the ports a statement crosses, on disk.

Two budgets, both counted at the port and not by reading call sites:

* how many participant page-access sections one public statement opens, and
* how deep the section stack is at the moment the statement is parsed.

The first is a per-statement constant, so each assertion names an exact number rather than a
bound: a row-proportional or duplicated count is the defect these tests exist to catch. The
second is zero, and must stay zero: parsing is local work behind an LRU cache whose misses cost
hundreds of microseconds, and the participant section serialises the threads of this
participant, so a parse performed under it would be a new serialisation nobody asked for.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxError
from okto_grafx.engine.query_engine import QueryEngine
from okto_grafx.engine.txn_manager import TransactionManager


def _schema(db) -> None:
    with db.begin("write") as tx:
        tx.execute("CREATE NODE TABLE T(pk INT64, v INT64, s STRING, PRIMARY KEY(pk))")
        tx.execute("CREATE REL TABLE R(FROM T TO T, w INT64)")


def _rows(db, count: int) -> None:
    with db.begin("write") as tx:
        for i in range(count):
            tx.execute(
                "CREATE (n:T {pk:$p, v:$v, s:$s}) RETURN n.pk",
                {"p": i, "v": (i * 7) % count, "s": f"s{i % 3}"},
            )


@pytest.fixture
def count_sections(monkeypatch):
    """Count entries into the participant page-access section at the manager port."""
    tally = {"n": 0}
    original = TransactionManager.page_access_section

    def counted(self, *args, **kwargs):
        tally["n"] += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(TransactionManager, "page_access_section", counted)
    return tally


def test_write_statement_opens_two_page_access_sections(tmp_path, count_sections):
    """One to run the statement and stage it, one to settle after the public result.

    The staging mark is taken inside the section that runs the statement. A third section here
    means the mark re-acquired the participant section, the file lock and the stat that the
    running statement was already holding.
    """
    with connect(tmp_path / "write-budget") as db:
        _schema(db)
        _rows(db, 8)
        with db.begin("write") as tx:
            count_sections["n"] = 0
            tx.execute("CREATE (n:T {pk:$p, v:1, s:'x'}) RETURN n.pk", {"p": 100})
            assert count_sections["n"] == 2
            count_sections["n"] = 0
            for offset in range(5):
                tx.execute(
                    "CREATE (n:T {pk:$p, v:1, s:'x'}) RETURN n.pk", {"p": 200 + offset}
                )
            assert count_sections["n"] == 10


def test_relationship_write_statement_opens_two_page_access_sections(
    tmp_path, count_sections
):
    """The Pulse relation shape lands two endpoints and still pays one staging section."""
    with connect(tmp_path / "rel-budget") as db:
        _schema(db)
        _rows(db, 8)
        with db.begin("write") as tx:
            count_sections["n"] = 0
            tx.execute(
                "MATCH (source:T {pk:$a}), (target:T {pk:$b}) "
                "CREATE (source)-[r:R {w:1}]->(target) RETURN source.pk, target.pk",
                {"a": 1, "b": 2},
            )
            assert count_sections["n"] == 2


def test_read_statement_opens_one_page_access_section(tmp_path, count_sections):
    """A read stages nothing, so it never opens a staging or settling section."""
    with connect(tmp_path / "read-budget") as db:
        _schema(db)
        _rows(db, 8)
        with db.begin("read") as tx:
            tx.execute("MATCH (n:T) RETURN n.pk ORDER BY n.pk")
            count_sections["n"] = 0
            tx.execute("MATCH (n:T) RETURN n.pk ORDER BY n.pk")
            assert count_sections["n"] == 1


def test_schema_statement_opens_one_page_access_section(tmp_path, count_sections):
    """DDL carries its own catalog journal and must not take a row-only staging snapshot."""
    with connect(tmp_path / "ddl-budget") as db:
        with db.begin("write") as tx:
            count_sections["n"] = 0
            tx.execute("CREATE NODE TABLE U(pk INT64, PRIMARY KEY(pk))")
            assert count_sections["n"] == 1


@pytest.fixture
def parse_depth(monkeypatch):
    """Record the page-access section depth held at every entry into the parser.

    The depth is counted at the manager port, incremented only once the real section has been
    entered, and the parser is counted at its own port on the engine class. ``execute`` parses
    the statement itself from inside the section, so the interesting entry is the FIRST one of
    a statement: that is the boundary's own question about whether the statement stages rows.
    """
    state = {"depth": 0, "parses": []}
    original_section = TransactionManager.page_access_section

    @contextmanager
    def tracked(self, *args, **kwargs):
        with original_section(self, *args, **kwargs):
            state["depth"] += 1
            try:
                yield
            finally:
                state["depth"] -= 1

    monkeypatch.setattr(TransactionManager, "page_access_section", tracked)

    original_parse = QueryEngine.parse

    def counted(self, text, *args, **kwargs):
        state["parses"].append((state["depth"], text))
        return original_parse(self, text, *args, **kwargs)

    monkeypatch.setattr(QueryEngine, "parse", counted)
    return state


def test_write_statement_is_parsed_with_no_page_access_section_held(
    tmp_path, parse_depth
):
    """The boundary asks whether a statement writes before it holds anything.

    ``QueryEngine.parse`` is LRU-cached, so a repeated parameterised statement pays microseconds
    -- but a cache miss costs hundreds of microseconds to milliseconds, and the participant
    section it would be held under serialises every other thread of this participant. Taking the
    mark inside the running statement's section must not drag the parse in with it.
    """
    with connect(tmp_path / "parse-depth") as db:
        _schema(db)
        _rows(db, 4)
        with db.begin("write") as tx:
            parse_depth["parses"].clear()
            tx.execute("CREATE (n:T {pk:$p, v:1, s:'x'}) RETURN n.pk", {"p": 500})
    assert parse_depth["parses"], "the statement never reached the parser"
    first_depth, first_text = parse_depth["parses"][0]
    assert first_text.startswith("CREATE (n:T")
    assert first_depth == 0, parse_depth["parses"]


def test_relationship_write_statement_is_parsed_with_no_section_held(
    tmp_path, parse_depth
):
    """The Pulse relation shape, whose plan is the more expensive one to build."""
    with connect(tmp_path / "parse-depth-rel") as db:
        _schema(db)
        _rows(db, 4)
        with db.begin("write") as tx:
            parse_depth["parses"].clear()
            tx.execute(
                "MATCH (source:T {pk:$a}), (target:T {pk:$b}) "
                "CREATE (source)-[r:R {w:1}]->(target) RETURN source.pk",
                {"a": 1, "b": 2},
            )
    assert parse_depth["parses"], "the statement never reached the parser"
    assert parse_depth["parses"][0][0] == 0, parse_depth["parses"]


def test_a_refused_statement_leaves_the_transaction_usable_and_unchanged(tmp_path):
    """A statement the parser refuses is refused before any section or mark exists.

    Three things have to hold together: the refusal is a typed ``GrafxError``, the participant
    section it would otherwise have been raised inside is not wedged (the next statement in the
    SAME transaction succeeds), and nothing of the refused statement is visible afterwards.
    """
    with connect(tmp_path / "refused") as db:
        _schema(db)
        _rows(db, 4)
        with db.begin("write") as tx:
            with pytest.raises(GrafxError):
                tx.execute("CRATE (n:T {pk:900, v:1, s:'x'})")
            with pytest.raises(GrafxError):
                tx.execute("CREATE (n:T {pk:901, v:1, s:'x'")
            tx.execute("CREATE (n:T {pk:902, v:1, s:'x'}) RETURN n.pk")
        with db.begin("read") as tx:
            rows = tx.execute("MATCH (n:T) RETURN n.pk ORDER BY n.pk").rows
    assert [row[0] for row in rows] == [0, 1, 2, 3, 902]


def test_a_statement_that_fails_mid_flight_stages_nothing(tmp_path):
    """The rollback boundary itself: rows staged before the failure never become visible."""
    with connect(tmp_path / "midflight") as db:
        _schema(db)
        _rows(db, 4)
        with db.begin("write") as tx:
            with pytest.raises(GrafxError):
                # 3 already exists, so the statement refuses after staging 800 and 801.
                tx.execute("UNWIND [800, 801, 3, 802] AS i CREATE (n:T {pk:i, v:1, s:'x'})")
            inside = tx.execute("MATCH (n:T) RETURN n.pk ORDER BY n.pk").rows
            assert [row[0] for row in inside] == [0, 1, 2, 3]
            tx.execute("CREATE (n:T {pk:900, v:1, s:'x'}) RETURN n.pk")
        with db.begin("read") as tx:
            rows = tx.execute("MATCH (n:T) RETURN n.pk ORDER BY n.pk").rows
    assert [row[0] for row in rows] == [0, 1, 2, 3, 900]
