"""Fixtures shared by the storage adapter suite (C2).

The behavioural suite is written once and executed once per device family, so the local device
and the in-memory twin are held to exactly the same contract. A family specific fixture exists
as well, for the few tests that must talk to one medium on purpose.

``holder_process`` starts a REAL second process that holds a handle on one file, which is what
TS-9 describes and what a same-process handle cannot honestly stand in for. The child announces
that it holds the file on its standard output and waits for a line on its standard input before
letting go, so the test never sleeps and never races.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from okto_grafx.adapters.storage_local import LocalStorageDevice
from okto_grafx.adapters.storage_memory import MemoryStorageDevice

PAGE_SIZE: int = 512
"""Small page, so a test can print a whole file and still read the assertion that failed."""

DEVICE_FAMILIES: tuple[str, ...] = ("local", "memory")
"""The two implementations of the storage port that must behave identically."""

HOLDER_SOURCE: str = r'''
import sys

path, mode, source_root = sys.argv[1], sys.argv[2], sys.argv[3]
if mode == "grafx":
    # A second Okto Grafx process, which is what a slow reader really is (AC-9, TS-9).
    import os

    sys.path.insert(0, source_root)
    from okto_grafx.adapters import storage_local
    from okto_grafx.adapters.storage_local import LocalStorageDevice

    if os.environ.get("GRAFX_OLD") == "a16":
        # The pre-A16 mechanism, so the mechanism test can be shown to fail without it.
        storage_local._WINDOWS_OPENER = None
    root, _, name = path.replace(os.sep, "/").rpartition("/wal/")
    device = LocalStorageDevice(root, page_size=512)
    device.read_log("wal/" + name, 0, 4)
    holder = device
elif mode == "legacy":
    # How this adapter opened its files before A16: no delete sharing.
    import os

    holder = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
elif mode == "share_delete":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    handle = kernel32.CreateFileW(path, 0x80000000, 0x1 | 0x2 | 0x4, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        sys.stdout.write("failed\n")
        sys.stdout.flush()
        raise SystemExit(1)
    holder = msvcrt.open_osfhandle(handle, 0)
else:
    holder = open(path, "rb")
sys.stdout.write("held\n")
sys.stdout.flush()
sys.stdin.readline()
'''
"""Program of the second process: hold a handle, say so, wait for permission to let go."""


class FileHolder:
    """A second process holding one file open, released when the test asks for it."""

    def __init__(self, process: subprocess.Popen[str]) -> None:
        """Take ownership of a child that already announced it holds the file."""
        self._process = process

    def release(self) -> None:
        """Let the child close the handle and wait for it to be gone."""
        if self._process.poll() is not None:
            return
        assert self._process.stdin is not None
        try:
            self._process.stdin.write("\n")
            self._process.stdin.flush()
        except (BrokenPipeError, ValueError):  # pragma: no cover - the child died early
            self._process.kill()
        self._process.wait(timeout=30)

    def kill(self) -> None:
        """Stop the child whatever state it is in."""
        if self._process.poll() is None:  # pragma: no cover - only on a failing test
            self._process.kill()
            self._process.wait(timeout=30)


@pytest.fixture
def holder_process() -> Iterator[Callable[..., FileHolder]]:
    """Return a factory that starts a second process holding a real file open."""
    holders: list[FileHolder] = []

    def _hold(path: Path, *, share_delete: bool = False, mode: str | None = None) -> FileHolder:
        selected = mode if mode is not None else ("share_delete" if share_delete else "plain")
        source_root = str(Path(__file__).resolve().parents[2] / "src")
        process = subprocess.Popen(
            [sys.executable, "-c", HOLDER_SOURCE, str(path), selected, source_root],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "held", "the second process could not hold the file"
        holder = FileHolder(process)
        holders.append(holder)
        return holder

    try:
        yield _hold
    finally:
        for holder in holders:
            holder.kill()


PORT_HOLDER_SOURCE: str = r'''
import sys

root, name, source_root = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, source_root)
from okto_grafx.adapters.storage_local import LocalStorageDevice

device = LocalStorageDevice(root, page_size=512)


def summary(payload):
    """Return the length and the distinct bytes, which is what proves a read was not a mix."""
    return "len=%d,bytes=%s" % (len(payload), ",".join(sorted({chr(byte) for byte in payload})))


held = device.read_log(name, 0, 1 << 20)
sys.stdout.write("held:" + summary(held) + chr(10))
sys.stdout.flush()
sys.stdin.readline()
# The same device, the same descriptor it already holds: this is the reader the publication
# had to leave undisturbed.
sys.stdout.write("after:" + summary(device.read_log(name, 0, 1 << 20)) + chr(10))
sys.stdout.flush()
device.close()
'''
"""Program of the reader process: open a file through the PORT, hold it, read it again later."""


class PortHolder:
    """A second process reading one file through the storage port and holding it open."""

    def __init__(self, process: subprocess.Popen[str], first: str) -> None:
        """Take ownership of a reader that already reported what it read."""
        self._process = process
        self.first: str = first

    def release(self) -> str:
        """Let the reader read once more through its open device and report what it saw."""
        assert self._process.stdin is not None
        assert self._process.stdout is not None
        self._process.stdin.write("\n")
        self._process.stdin.flush()
        answer = self._process.stdout.readline().strip()
        self._process.wait(timeout=30)
        return answer.removeprefix("after:")

    def kill(self) -> None:
        """Stop the reader whatever state it is in."""
        if self._process.poll() is None:  # pragma: no cover - only on a failing test
            self._process.kill()
            self._process.wait(timeout=30)


@pytest.fixture
def port_holder() -> Iterator[Callable[..., PortHolder]]:
    """Return a factory that starts a second process reading one file through the port."""
    holders: list[PortHolder] = []

    def _hold(root: Path, name: str) -> PortHolder:
        source_root = str(Path(__file__).resolve().parents[2] / "src")
        process = subprocess.Popen(
            [sys.executable, "-c", PORT_HOLDER_SOURCE, str(root), name, source_root],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            # A94: the child must resolve okto_grafx from the tree under test, never from an
            # editable install that could point somewhere else entirely.
            env={**os.environ, "PYTHONPATH": source_root},
        )
        assert process.stdout is not None
        first = process.stdout.readline().strip()
        assert first.startswith("held:"), "the reader process could not open the file"
        holder = PortHolder(process, first.removeprefix("held:"))
        holders.append(holder)
        return holder

    try:
        yield _hold
    finally:
        for holder in holders:
            holder.kill()


@pytest.fixture(params=DEVICE_FAMILIES)
def device(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[object]:
    """Yield one open device per family, closed when the test ends."""
    if request.param == "local":
        instance: LocalStorageDevice | MemoryStorageDevice = LocalStorageDevice(
            tmp_path / "db", page_size=PAGE_SIZE
        )
    else:
        instance = MemoryStorageDevice(page_size=PAGE_SIZE)
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def local_device(tmp_path: Path) -> Iterator[LocalStorageDevice]:
    """Yield a device over a real directory."""
    instance = LocalStorageDevice(tmp_path / "db", page_size=PAGE_SIZE)
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def memory_device() -> Iterator[MemoryStorageDevice]:
    """Yield a device over byte buffers."""
    instance = MemoryStorageDevice(page_size=PAGE_SIZE)
    try:
        yield instance
    finally:
        instance.close()
