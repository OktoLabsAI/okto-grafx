"""The Okto Grafx operator command line (C12).

Everything else in this database refuses precisely, classifies damage and preserves evidence.
This package is what makes those qualities legible to a person at a terminal, and to the scripts
that person writes::

    python -m okto_grafx.cli status ./mydb
    python -m okto_grafx.cli verify ./mydb --scope all
    python -m okto_grafx.cli ledger list ./mydb --origin-class forensic --json

A statement runs through one of the two doors CONTRACT.md section 10 defines, and ``query`` makes
which one explicit. Without ``--write`` it is the autocommit read, ``db.execute(...)``; with
``--write`` it is a write transaction, ``db.begin("write")`` plus ``txn.execute(...)``, committed
when the statement succeeds::

    python -m okto_grafx.cli query ./mydb "CREATE NODE TABLE Person(id INT64)" --write
    python -m okto_grafx.cli query ./mydb "CREATE (:Person {id: 1})" --write
    python -m okto_grafx.cli query ./mydb "MATCH (p:Person) RETURN p.id"

A statement that writes, run without ``--write``, is refused by the engine and this tool adds a
line naming the flag -- beside the refusal, never in place of it.

Six commands cover what an operator has to be able to do: ``status`` opens a database and says
what state it is in, ``verify`` walks it and locates every finding, ``query`` runs one statement,
``recovery`` reports what the replay at open did, ``ledger`` and ``quarantine`` read the evidence
a discard preserved, and ``metrics`` reports the endpoint and the current values.

Two properties are the point of the whole package. **The exit code is the contract**: the set is
frozen in :mod:`okto_grafx.cli.exits` and derived from the same decision as the printed verdict,
so a script can tell clean from damaged from could-not-run without reading English. And **no
input produces a traceback**: :func:`okto_grafx.cli.entry.main` returns a documented code for a
malformed argument, a missing path, a damaged database or an interrupt, and for nothing else.
"""

from __future__ import annotations

from okto_grafx.cli.entry import main

__all__ = ["main"]
