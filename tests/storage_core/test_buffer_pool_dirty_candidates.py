"""D-10: dirty-candidate indexing remains equivalent to the complete frame scan."""

from __future__ import annotations

import random

from okto_grafx.domain.errors import GrafxBufferBudgetExceeded
from okto_grafx.domain.page import Page, PageType
from okto_grafx.engine.buffer_pool import BufferPool, apply_page_image

from .conftest import MemoryDevice, RecordingMetrics, make_pool

FILE = "heap.dat"
OTHER = "other.dat"


def _seed(pool: BufferPool, count: int, *, file: str = FILE) -> None:
    pool.storage.create(file)
    for number in range(count):
        page = pool.allocate(file, int(PageType.HEAP))
        page.insert_slot(f"seed-{number}".encode())
        pool.unpin(file, page.page_index, dirty=True, page=page)
    pool.flush(file)
    pool.forget_modified()


def _full_modified(
    pool: BufferPool, file: str | None = None
) -> frozenset[tuple[str, int]]:
    live = {
        key
        for key, frame in pool._frames.items()  # noqa: SLF001 - full-scan oracle
        if frame.page.dirty
    }
    return frozenset(
        key
        for key in (live | pool._modified)  # noqa: SLF001 - full-scan oracle
        if file is None or key[0] == file
    )


def _full_has_dirty(pool: BufferPool, file: str | None = None) -> bool:
    if any(
        frame.page.dirty and (file is None or name == file)
        for (name, _page_index), frame in pool._frames.items()  # noqa: SLF001
    ):
        return True
    return any(
        frame.page.dirty and (file is None or name == file)
        for (name, _page_index), frames in pool._doomed.items()  # noqa: SLF001
        for frame in frames
    )


def _assert_full_scan_equivalence(pool: BufferPool) -> None:
    pool._assert_dirty_candidate_coverage()  # noqa: SLF001 - D-10 test oracle
    for file in (None, FILE, OTHER):
        expected_modified = _full_modified(pool, file)
        expected_dirty = _full_has_dirty(pool, file)
        assert pool.modified_pages(file) == expected_modified
        assert pool.has_dirty_pages(file) is expected_dirty
    pool._assert_dirty_candidate_coverage()  # noqa: SLF001 - revalidation is exact


def test_multi_pin_candidate_survives_flush_and_a_late_second_mutation() -> None:
    pool = make_pool(MemoryDevice(), RecordingMetrics(), budget_pages=4)
    _seed(pool, 4)
    first = pool.pin(FILE, 0)
    second = pool.pin(FILE, 0)

    first.update_slot(0, b"first-mutation")
    _assert_full_scan_equivalence(pool)
    assert pool.flush(FILE) == 1
    assert not first.dirty
    _assert_full_scan_equivalence(pool)

    second.update_slot(0, b"late-second-mutation")
    pool.unpin(FILE, 0, dirty=first.dirty, page=first)
    _assert_full_scan_equivalence(pool)
    pool.unpin(FILE, 0, dirty=second.dirty, page=second)
    _assert_full_scan_equivalence(pool)

    assert pool.flush(FILE) == 1
    assert not any(pool._dirty_candidates.values())  # noqa: SLF001


def test_doomed_dirty_holder_is_indexed_until_discard_only_release() -> None:
    pool = make_pool(MemoryDevice(), RecordingMetrics(), budget_pages=2)
    _seed(pool, 1)
    held = pool.pin(FILE, 0)

    assert pool.begin_read_view("foreign") is True
    held.update_slot(0, b"late-stale-mutation")
    _assert_full_scan_equivalence(pool)
    assert pool.modified_pages() == frozenset()

    pool.unpin(FILE, 0, dirty=True, page=held)
    _assert_full_scan_equivalence(pool)
    assert not any(pool._dirty_candidates.values())  # noqa: SLF001


def test_apply_invalidate_and_dirty_eviction_settle_candidates_exactly() -> None:
    device = MemoryDevice()
    seeder = make_pool(device, RecordingMetrics(), budget_pages=8)
    _seed(seeder, 6)
    pool = make_pool(device, RecordingMetrics(), budget_pages=2)

    replacement = Page(
        int(PageType.HEAP), page_size=pool.page_size, page_index=0, page_lsn=9
    )
    replacement.insert_slot(b"redo-image")
    assert apply_page_image(pool, FILE, 0, pool.codec.encode_page(replacement))
    _assert_full_scan_equivalence(pool)

    # A cold admission evicts and writes the dirty page. It must leave durable-change evidence
    # in _modified while removing the settled location from the dirty candidate index.
    clean = pool.pin(FILE, 1)
    pool.unpin(FILE, 1, page=clean)
    cold = pool.pin(FILE, 2)
    pool.unpin(FILE, 2, page=cold)
    _assert_full_scan_equivalence(pool)
    assert (FILE, 0) in pool.modified_pages()

    with pool.pinned(FILE, 2) as dirty:
        dirty.update_slot(0, b"invalidate-write")
    pool.invalidate(FILE)
    _assert_full_scan_equivalence(pool)
    assert not any(pool._dirty_candidates.values())  # noqa: SLF001


def test_clean_hot_candidate_linger_is_bounded_and_drained_by_flush() -> None:
    pool = make_pool(MemoryDevice(), RecordingMetrics(), budget_pages=48)
    _seed(pool, 40)

    for page_index in range(40):
        page = pool.pin(FILE, page_index)
        pool.unpin(FILE, page_index, page=page)

    candidates = pool._dirty_candidates[FILE]  # noqa: SLF001 - D-10 bound
    assert len(candidates) == 32
    assert candidates == set(range(8, 40))
    assert set(pool._clean_candidate_linger[FILE]) == candidates  # noqa: SLF001

    # A repeatedly read hot page remains in the bounded cohort; no candidate-set mutation is
    # needed on its 0 -> 1 -> 0 pin cycle.
    for _ in range(20):
        page = pool.pin(FILE, 39)
        pool.unpin(FILE, 39, page=page)
    assert pool._dirty_candidates[FILE] == candidates  # noqa: SLF001

    assert pool.flush(FILE) == 0
    assert FILE not in pool._dirty_candidates  # noqa: SLF001
    assert FILE not in pool._clean_candidate_linger  # noqa: SLF001
    _assert_full_scan_equivalence(pool)


def test_seeded_transition_sequences_match_the_full_scan_oracle() -> None:
    rng = random.Random(20260904)
    pool = make_pool(MemoryDevice(), RecordingMetrics(), budget_pages=4)
    _seed(pool, 9)
    held: list[tuple[int, Page]] = []

    for step in range(240):
        operation = rng.randrange(8)
        page_index = rng.randrange(9)
        if operation == 0 and len(held) < 3:
            try:
                held.append((page_index, pool.pin(FILE, page_index)))
            except GrafxBufferBudgetExceeded:
                pass
        elif operation == 1 and held:
            _index, page = rng.choice(held)
            page.update_slot(0, f"mutation-{step}".encode())
        elif operation == 2 and held:
            position = rng.randrange(len(held))
            index, page = held.pop(position)
            pool.unpin(FILE, index, dirty=page.dirty, page=page)
        elif operation == 3:
            pool.flush(FILE if rng.randrange(2) else None)
        elif operation == 4:
            pool.write_back(FILE, page_index)
        elif operation == 5:
            pool.discard(FILE, page_index)
        elif operation == 6 and not held:
            pool.begin_read_view(f"view-{step}")
        elif operation == 7:
            image = Page(
                int(PageType.HEAP),
                page_size=pool.page_size,
                page_index=page_index,
                page_lsn=step + 1,
            )
            image.insert_slot(f"redo-{step}".encode())
            apply_page_image(pool, FILE, page_index, pool.codec.encode_page(image))
        _assert_full_scan_equivalence(pool)

    while held:
        index, page = held.pop()
        pool.unpin(FILE, index, dirty=page.dirty, page=page)
        _assert_full_scan_equivalence(pool)
