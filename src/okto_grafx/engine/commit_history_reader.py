"""Optimistic, bounded publication proof for append-only journal observations.

No global reader lock, shared authority cache, physical mutation or repair. Each
attempt verifies current publication, qualified page stamps, head and extents.
Old snapshots filter a current append-only prefix; they never invent old heads.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxError, GrafxUnsupportedOperation
from okto_grafx.domain.page import PageHeader
from okto_grafx.domain.txn.records import COMMIT_DIRECTORY_FILE, COMMIT_STREAM_FILE
from okto_grafx.engine.commit_catalog_store import CommitCatalogStore

_Result = TypeVar("_Result")


class _MovingPublication(Exception):
    pass


def observe_commit_catalog(
    *, database_uuid: bytes, page_size: int, activation: int,
    read_page: Callable[[str, int], bytes], file_size: Callable[[str], int],
    exists: Callable[[str], bool], published: Callable[[], int],
    operation: Callable[[CommitCatalogStore], _Result], empty: _Result,
    minimum_sequence: int = 0,
) -> _Result:
    """Return only a fully checked observation, or a typed bounded refusal.

    A page beyond control publication can belong to an in-flight/durable-but-not-
    published writer. It is never accepted as already committed. Persistent
    contention requires a later retry/recovery, not a weakened journal check.
    """
    files = (COMMIT_DIRECTORY_FILE, COMMIT_STREAM_FILE)
    for _attempt in range(4):
        before = published()
        if before < minimum_sequence:
            raise GrafxCorruptionDetected(
                "Commit history publication regressed below the owning snapshot.",
                field="published_regression",
            )
        head_before: bytes | None = None
        sizes: dict[str, int] = {}
        failure: GrafxError | None = None
        moving = False
        result = empty

        def read(file: str, index: int) -> bytes:
            """Read one page from this operation's selected journal view."""
            raw = read_page(file, index)
            stamp = PageHeader.decode(raw).page_lsn
            if stamp > before:
                raise _MovingPublication
            if stamp <= activation:
                raise GrafxCorruptionDetected(
                    "Commit history page predates activation.", field="published_page_sequence",
                )
            return raw

        try:
            present = tuple(exists(file) for file in files)
            if before == activation and not any(present):
                if published() == before and not any(exists(file) for file in files):
                    return empty
                continue
            sizes = {file: file_size(file) for file in files}
            head_before = read(COMMIT_DIRECTORY_FILE, 0)
            store = CommitCatalogStore(read, database_uuid=database_uuid, page_size=page_size)
            store.validate_published_head(
                sequence=before, activation_sequence=activation,
                file_size=sizes.__getitem__,
            )
            result = operation(store)
        except _MovingPublication:
            moving = True
        except GrafxError as caught:
            failure = caught
        after = published()
        if before != after or moving:
            continue
        # Compare the head again even on a failed scan: a concurrent tail must
        # not be misreported as stable corruption. No caller callback runs here.
        if head_before is not None:
            try:
                if read_page(COMMIT_DIRECTORY_FILE, 0) != head_before:
                    continue
                if {file: file_size(file) for file in files} != sizes:
                    continue
            except GrafxError:
                if failure is not None:
                    raise failure
                raise
        if published() != before:
            continue
        if failure is not None:
            raise failure
        return result
    raise GrafxUnsupportedOperation(
        "Commit history changed during bounded observation; retry the read after publication.",
        field="commit_history_observation", reason="publication_in_progress",
        retryable=True,
    )

__all__ = ["observe_commit_catalog"]
