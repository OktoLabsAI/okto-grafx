"""No state is shared between two databases, structurally and not only by behaviour.

SPEC-M1 FR-13 and BR-8 are about isolation: a database that runs out of memory affects its own
transactions and nothing else, and there is no global cooldown. The behavioural proof lives in
test_buffer_pool.py. This file is the structural half, because a shared cache usually arrives as
a convenience rather than as a decision: a module-level dictionary keyed by file name, a class
attribute holding frames, a lazily built registry. Every one of those would pass a behavioural
test that only ever builds one database.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.catalog_store import CatalogStore
from okto_grafx.engine.heap_store import HeapStore

from .conftest import MemoryDevice, RecordingMetrics, make_pool

PACKAGE_ROOT: Path = Path(__file__).resolve().parents[2] / "src" / "okto_grafx"

OWNED_MODULES: tuple[Path, ...] = (
    PACKAGE_ROOT / "engine" / "buffer_pool.py",
    PACKAGE_ROOT / "engine" / "heap_store.py",
    PACKAGE_ROOT / "engine" / "catalog_store.py",
    PACKAGE_ROOT / "domain" / "page" / "slotted.py",
    PACKAGE_ROOT / "domain" / "page" / "layout.py",
    PACKAGE_ROOT / "domain" / "page" / "checksum.py",
    PACKAGE_ROOT / "domain" / "page" / "file_header.py",
    PACKAGE_ROOT / "domain" / "page" / "overflow.py",
    PACKAGE_ROOT / "domain" / "model" / "value.py",
    PACKAGE_ROOT / "domain" / "model" / "schema.py",
    PACKAGE_ROOT / "domain" / "model" / "catalog.py",
    PACKAGE_ROOT / "domain" / "model" / "record.py",
    PACKAGE_ROOT / "domain" / "rand.py",
)
"""Every module of the storage core that could hide a shared container."""

MUTABLE_CONTAINERS: tuple[type, ...] = (list, set, bytearray, dict)

DECLARED_BINDINGS: frozenset[str] = frozenset({"__all__", "VECTOR_DTYPES"})
"""The two module-level containers of the storage core, both declarations rather than state.

``__all__`` is the export list every module declares (amendment A6). ``VECTOR_DTYPES`` is the
fixed mapping from the storage dtype of an embedding space to the value type its vectors encode
to; it is written once at import and only ever read. Neither of them accumulates anything, which
is what this gate is looking for: a container that grows as databases are used would be shared
between them.
"""


@pytest.mark.parametrize("path", OWNED_MODULES, ids=lambda path: path.name)
def test_no_module_of_the_storage_core_binds_a_mutable_container(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = [target.id for target in targets if isinstance(target, ast.Name)]
        if not names or node.value is None:
            continue
        if all(name in DECLARED_BINDINGS for name in names):
            continue
        assert not isinstance(node.value, (ast.List, ast.Dict, ast.Set, ast.ListComp)), (
            f"{path.name} binds the mutable container {names} at module level"
        )
        if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name):
            assert node.value.func.id not in {"list", "dict", "set", "bytearray"}, (
                f"{path.name} builds the mutable container {names} at module level"
            )


@pytest.mark.parametrize(
    "owner", [BufferPool, HeapStore, CatalogStore], ids=lambda owner: owner.__name__
)
def test_no_class_of_the_storage_core_holds_data_on_the_class(owner: type) -> None:
    # Every one of them declares __slots__, so an instance cannot even grow a shared attribute
    # by accident, and nothing on the class itself is a container.
    assert owner.__slots__
    for name, value in vars(owner).items():
        assert not isinstance(value, MUTABLE_CONTAINERS), f"{owner.__name__}.{name}"


def test_two_pools_built_from_the_same_arguments_share_no_frame() -> None:
    device = MemoryDevice()
    first = make_pool(device, RecordingMetrics(), db_label="alpha")
    second = make_pool(device, RecordingMetrics(), db_label="beta")
    assert first is not second
    device.create("heap.dat")
    page = first.allocate("heap.dat", 2)
    first.unpin("heap.dat", page.page_index, dirty=True)
    first.flush()
    assert first.is_resident("heap.dat", 0)
    assert not second.is_resident("heap.dat", 0)
    second.pin("heap.dat", 0)
    assert first.pin_count("heap.dat", 0) == 0
    assert second.pin_count("heap.dat", 0) == 1
    # The two caches hold two different page objects over the same stored bytes.
    assert first.pin("heap.dat", 0) is not second.pin("heap.dat", 0)


def test_a_second_database_opens_while_the_first_is_out_of_memory() -> None:
    starved = make_pool(MemoryDevice(), RecordingMetrics(), budget_pages=2, db_label="starved")
    starved_catalog = CatalogStore(starved)
    starved_catalog.bootstrap()
    starved.storage.create("heap.dat")
    for _ in range(2):
        page = starved.allocate("heap.dat", 2)
        assert starved.pin_count("heap.dat", page.page_index) == 1

    # The first database is completely out of frames. Opening a second one still works, with no
    # cooldown, no shared lock and no shared budget.
    healthy = make_pool(MemoryDevice(), RecordingMetrics(), budget_pages=4, db_label="healthy")
    catalog = CatalogStore(healthy)
    catalog.bootstrap()
    heap = HeapStore(healthy, catalog)
    heap.bootstrap()
    assert catalog.catalog.is_empty()
    assert heap.is_bootstrapped()
