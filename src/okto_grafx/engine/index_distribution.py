"""Explicit bounded physical hash diagnostics; no key values leave the operation."""

from __future__ import annotations
from typing import TYPE_CHECKING

from dataclasses import dataclass

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxUnsupportedOperation, GrafxQueryBudgetExceeded
from okto_grafx.domain.index.layout import IndexLayout

__all__ = ["IndexDistribution"]

if TYPE_CHECKING:
    from okto_grafx.engine.database import Database
    from okto_grafx.engine.index_manager import _IndexReadCertificate


@dataclass(frozen=True, slots=True)
class IndexDistribution:
    """Physical entries, including retained versions; not live row cardinality."""

    bucket_count: int
    entries: int
    pages: int
    overflow_pages: int
    largest_chain_pages: int
    largest_bucket_entries: int
    largest_key_entries: int
    dominant_key_fraction: float
    recommendation: str


def index_distribution(database: Database, name: str, *, max_pages: int, max_entries: int,
                       max_memory_bytes: int) -> IndexDistribution:
    """Perform an explicit bounded physical hash census, without disclosing keys."""
    for field, value in (("max_pages", max_pages), ("max_entries", max_entries),
                         ("max_memory_bytes", max_memory_bytes)):
        if type(value) is not int or not 1 <= value <= 2**31:
            raise GrafxConfigurationError("Invalid distribution bound.", field=field)
    with database._public_operation("index_distribution"), database.begin("read") as reader:
        with database._transactions.page_access_section(transaction=reader._context):
            store = database._indexes.active_index(name)
            if store.definition.layout not in (IndexLayout.HASH, IndexLayout.SPARSE_HASH):
                raise GrafxUnsupportedOperation("Distribution describes HASH indexes only.", field="layout")
            pages_work = 0

            def visit() -> None:
                """Bound total page work, including repeated certificate attempts."""
                nonlocal pages_work
                pages_work += 1
                if pages_work > max_pages:
                    raise GrafxQueryBudgetExceeded("Distribution page budget exceeded.", resource="index_distribution")

            def observe(certificate: _IndexReadCertificate) -> IndexDistribution:
                """Build distribution statistics under one native stable-view attempt."""
                counts = {}
                entries = pages = largest_chain = largest_bucket = heads = 0
                memory = 0
                for bucket in range(store.definition.bucket_count):
                    if store.definition.layout is IndexLayout.SPARSE_HASH:
                        visit()  # pointer-page work, including empty buckets
                    chain_pages = 0

                    def visit_chain() -> None:
                        """Reserve each captured chain position before retaining it."""
                        nonlocal chain_pages
                        visit()
                        chain_pages += 1
                        if memory + chain_pages * 64 > max_memory_bytes:
                            raise GrafxQueryBudgetExceeded("Distribution memory budget exceeded.", resource="index_distribution")

                    chain, _ = store._scan_bucket(bucket, visit=visit_chain)
                    heads += bool(chain)
                    pages += len(chain)
                    largest_chain = max(largest_chain, len(chain))
                    local = 0
                    for page in chain:
                        for entry in store._entries_on(page):
                            entries += 1
                            local += 1
                            if entries > max_entries:
                                raise GrafxQueryBudgetExceeded("Distribution entry budget exceeded.", resource="index_distribution")
                            if entry.key not in counts:
                                memory += 128 + len(entry.key)
                                if memory + len(chain) * 64 > max_memory_bytes:
                                    raise GrafxQueryBudgetExceeded("Distribution memory budget exceeded.", resource="index_distribution")
                            counts[entry.key] = counts.get(entry.key, 0) + 1
                    largest_bucket = max(largest_bucket, local)
                dominant = max(counts.values(), default=0)
                fraction = dominant / entries if entries else 0.0
                overflow = pages - heads
                recommendation = ("inspect_key_skew" if entries >= 16 and fraction >= 0.5 else
                                  "consider_growth" if overflow or entries > store.definition.bucket_count * 64 else "balanced")
                return IndexDistribution(store.definition.bucket_count, entries, pages, overflow,
                                         largest_chain, largest_bucket, dominant, fraction, recommendation)

            return store._stable_view(reader.snapshot.read_lsn, observe)
