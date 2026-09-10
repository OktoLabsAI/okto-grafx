"""Detached history page returned for one qualified MVCC observation."""
from __future__ import annotations

from dataclasses import dataclass

from okto_grafx.domain.txn.commit_catalog import CommitCatalogEntry


@dataclass(frozen=True, slots=True)
class CommitHistoryPage:
    """Ascending commits; history at/below activation is explicitly untracked.

    Continue after the final entry's identity in the SAME read transaction to
    retain this read_sequence. has_more describes only that transaction's view.
    """

    database_uuid: bytes
    activation_sequence: int
    read_sequence: int
    entries: tuple[CommitCatalogEntry, ...]
    has_more: bool

__all__ = ["CommitHistoryPage"]
