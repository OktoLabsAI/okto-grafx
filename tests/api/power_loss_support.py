"""Power loss through the public door: the fault bench (FR-16) over an in-memory device.

The bench keeps a write in its volatile window only while a crash could take it back, which is
while its barrier lies or while it reorders. Lying from the start would lose the log too, and
lying only at the end records nothing; so a faithful power loss is REORDERING: every write
applies at once, an honest barrier pins exactly the file it names, and a crash keeps a seeded
prefix of what was never pinned. Which prefix is a matter of the seed, so a scenario is built
under one seed after another until the state it needs appears, and asserts that it did (A72).
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from okto_grafx.adapters.storage_fault import (
    FaultInjectingStorageDevice,
    SimulatedCrash,
)
from okto_grafx.engine.database import META_FILE
from okto_grafx.runtime.bootstrap import build_default_registry
from okto_grafx.runtime.config import DatabaseConfig

CONTROL_PREFIX = "control/"
SEEDS = range(1, 257)
"""Seeds tried, in order, until a scenario produces the state it needs."""


def bench_registry(
    seed: int, **options: Any
) -> tuple[Any, FaultInjectingStorageDevice, Any]:
    """Return (registry, bench, inner): the default memory composition behind the fault bench."""
    config = DatabaseConfig(path=":memory:", **options)
    registry = build_default_registry(config)
    inner = registry.get("storage")
    bench = FaultInjectingStorageDevice(inner, seed=seed)
    registry.bind("storage", bench)
    return registry, bench, inner


def file_bytes(device: Any, name: str) -> bytes | None:
    """Return the whole content of one file of the inner device, or None when it is absent."""
    if not device.exists(name):
        return None
    return bytes(device.read_log(name, 0, device.log_size(name)))


def data_digests(device: Any) -> dict[str, str]:
    """Return a digest of every DATA file (the control plane excluded, as a reader must write there)."""
    digests: dict[str, str] = {}
    for name in device.list_files():
        if name.startswith(CONTROL_PREFIX):
            continue
        content = file_bytes(device, name)
        digests[name] = (
            "absent" if content is None else hashlib.sha256(content).hexdigest()
        )
    return digests


def durable_names(device: Any) -> tuple[str, ...]:
    """Return the files a commit makes durable by name: the log segments and the commit state."""
    return tuple(
        name
        for name in device.list_files()
        if name.startswith("wal/") or name == "control/commit.state"
    )


def power_loss(bench: FaultInjectingStorageDevice) -> None:
    """Cut the process off now: the seeded tail of every un-pinned write is gone, the rest stays.

    The crash is fired on the very next port call, whatever it is; afterwards the survivors are
    pinned and the bench stops reordering, because what survived a power loss is on the platter.
    """
    bench.clear_trail()
    bench.crash_at(1)
    with pytest.raises(SimulatedCrash):
        bench.exists(META_FILE)
    bench.disarm()
    bench.flush_reordered()
