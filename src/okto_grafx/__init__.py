"""Okto Grafx: an embedded graph database for Python.

Correct under multi-process concurrency, verifiable on disk and recoverable by construction.

This module is the public entry point of the package (CONTRACT.md section 10)::

    from okto_grafx import connect, Database, Transaction

    db = connect("./mydb", partitions_per_table=64)      # or ":memory:"
    with db.begin("write") as txn:
        txn.execute("CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))")
    report = db.recover()
    db.close()

Errors keep their own supported import path in :mod:`okto_grafx.errors`, so the taxonomy of
section 2 is reachable without importing the engine. A database composed without a given engine
refuses the door that needs it with a typed GrafxUnsupportedOperation naming the missing
component, rather than pretending to answer.

Every name below is re-exported, never redefined: the definition of each lives in the component
that owns it (A24), and this module only makes it reachable by its supported path (A10).
"""

from __future__ import annotations

from okto_grafx.api import ConnectOptions, connect
from okto_grafx.domain.index.fulltext import TextIndexOptions, TextSearchLimits, TextHit, TextSearchResult
from okto_grafx.domain.query.hybrid import HybridHit, HybridSearchOptions, HybridSearchResult
from okto_grafx.domain.model import Timestamp, VectorValue
from okto_grafx.domain.query.control import CancellationToken
from okto_grafx.domain.txn.commit_identity import CommitId
from okto_grafx.domain.txn.commit_metadata import CommitMetadata, MetadataLimits
from okto_grafx.domain.txn.commit_catalog import CommitCatalogEntry, CommitKind
from okto_grafx.domain.txn.commit_history import CommitHistoryPage
from okto_grafx.domain.txn.commit_transfer import CommitImport, CommitMapping, prepare_commit_import
from okto_grafx.engine.database import (
    Database,
    DatabaseIdentity,
    ExecuteManyReport,
    Query,
    QueryCursor,
    ScanCursorV1,
    ScanPageV1,
    ScanRowV1,
    Transaction,
)
from okto_grafx.engine.query_engine import QueryResult
from okto_grafx.runtime.config import DatabaseConfig
from okto_grafx.runtime.registry import PortRegistry

__all__ = [
    "CancellationToken",
    "CommitCatalogEntry",
    "CommitHistoryPage",
    "CommitId",
    "CommitImport",
    "CommitKind",
    "CommitMapping",
    "CommitMetadata",
    "ConnectOptions",
    "Database",
    "DatabaseConfig",
    "DatabaseIdentity",
    "ExecuteManyReport",
    "HybridHit",
    "HybridSearchOptions",
    "HybridSearchResult",
    "MetadataLimits",
    "PortRegistry",
    "Query",
    "QueryCursor",
    "QueryResult",
    "ScanCursorV1",
    "ScanPageV1",
    "ScanRowV1",
    "TextHit",
    "TextIndexOptions",
    "TextSearchLimits",
    "TextSearchResult",
    "Timestamp",
    "Transaction",
    "VectorValue",
    "__version__",
    "connect",
    "prepare_commit_import",
]

__version__: str = "0.0.5"
