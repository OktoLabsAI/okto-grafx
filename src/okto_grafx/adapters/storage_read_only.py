"""A fail-closed read-only view of the storage port.

The engine has several collaborators that receive a :class:`StorageDevice`.  A policy flag at
the public facade is therefore not a sufficient read-only boundary: a missed call site could
still append WAL, allocate a page, or publish a control file.  This adapter makes the boundary
structural.  It forwards the storage protocol's observations unchanged and refuses every
persistent mutation before the wrapped device is invoked.
"""

from __future__ import annotations

from types import TracebackType
from typing import NoReturn, Self

from okto_grafx.adapters.control_record_io import read_control_if_exists
from okto_grafx.domain.errors import GrafxUnsupportedOperation
from okto_grafx.domain.ids import PageIndex
from okto_grafx.domain.ports.storage import StorageDevice

__all__ = ["ReadOnlyStorageDevice"]


class ReadOnlyStorageDevice:
    """Expose only the observational half of one storage device.

    The wrapped port is intentionally kept in a name-mangled slot and has no public accessor.
    Code that needs the writable device must retain that capability explicitly instead of
    recovering it from a read-only handle.
    """

    __slots__ = ("__device",)

    def __init__(self, device: StorageDevice) -> None:
        """Wrap ``device`` without reading from or mutating it."""
        self.__device: StorageDevice = device

    @property
    def name(self) -> str:
        """Return the wrapped device's metric-safe name unchanged."""
        return self.__device.name

    @property
    def page_size(self) -> int:
        """Return the wrapped device's page size unchanged."""
        return self.__device.page_size

    def invalidate_descriptor_identity(self, file: str | None = None) -> None:
        """Forward the optional cache-only descriptor identity invalidation capability."""
        invalidate = getattr(self.__device, "invalidate_descriptor_identity", None)
        if callable(invalidate):
            invalidate(file)

    def descriptor_cache_stats(self) -> object:
        """Forward the optional path-free descriptor-cache diagnostics capability."""
        snapshot = getattr(self.__device, "descriptor_cache_stats", None)
        if not callable(snapshot):
            return None
        return snapshot()

    def exists(self, file: str) -> bool:
        """Return whether ``file`` exists, preserving the wrapped observation."""
        return self.__device.exists(file)

    def create(self, file: str, *, exclusive: bool = True) -> None:
        """Refuse file creation before the wrapped device is reached."""
        self._refuse("create")

    def remove(self, file: str) -> None:
        """Refuse file removal before the wrapped device is reached."""
        self._refuse("remove")

    def list_files(self, prefix: str = "") -> tuple[str, ...]:
        """Return the wrapped namespace listing unchanged."""
        return self.__device.list_files(prefix)

    def file_size(self, file: str) -> int:
        """Return the wrapped file size unchanged."""
        return self.__device.file_size(file)

    def atomic_replace(self, source: str, target: str) -> None:
        """Refuse atomic replacement before the wrapped device is reached."""
        self._refuse("atomic_replace")

    def recycle(self, file: str) -> bool:
        """Refuse namespace recycling before the wrapped device is reached."""
        self._refuse("recycle")

    def page_count(self, file: str) -> int:
        """Return the wrapped page count unchanged."""
        return self.__device.page_count(file)

    def allocate(self, file: str, count: int = 1) -> PageIndex:
        """Refuse page allocation before the wrapped device is reached."""
        self._refuse("allocate")

    def read_page(self, file: str, page_index: PageIndex) -> bytes:
        """Return the exact page bytes produced by the wrapped device."""
        return self.__device.read_page(file, page_index)

    def write_page(self, file: str, page_index: PageIndex, data: bytes) -> None:
        """Refuse page writes before the wrapped device is reached."""
        self._refuse("write_page")

    def append_log(self, file: str, payload: bytes) -> int:
        """Refuse log appends before the wrapped device is reached."""
        self._refuse("append_log")

    def read_log(self, file: str, offset: int, length: int) -> bytes:
        """Return the fill-until-EOF byte range produced by the wrapped device."""
        return self.__device.read_log(file, offset, length)

    def read_log_if_exists(
        self, file: str, offset: int, length: int
    ) -> bytes | None:
        """Forward an explicitly declared fused read, else use the literal port sequence.

        A wrapped type that only exposes unknown attributes through ``__getattr__`` has not
        opted into the fused identity contract. In that case absence is observed through
        ``exists`` and a present name is read normally; a race that removes it is allowed to
        propagate the wrapped ``read_log`` refusal unchanged.
        """
        return read_control_if_exists(self.__device, file, offset, length)

    def log_size(self, file: str) -> int:
        """Return the wrapped log size unchanged."""
        return self.__device.log_size(file)

    def truncate_log(self, file: str, size: int) -> None:
        """Refuse log truncation before the wrapped device is reached."""
        self._refuse("truncate_log")

    def durable_barrier(self, file: str | None = None) -> None:
        """Refuse durability barriers because they can publish pending writable handles."""
        self._refuse("durable_barrier")

    def close(self) -> None:
        """Do nothing because this view never owns the wrapped device's lifecycle.

        Closing a storage device is not observational: the local adapter retries pending
        deletions after releasing handles.  Composition must therefore retain and close the raw
        device through its separate ownership path; a read-only view cannot gain that authority.
        """

    def __enter__(self) -> Self:
        """Return this read-only handle for context-manager use."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Leave the non-owning view without touching the wrapped adapter."""
        self.close()

    @staticmethod
    def _refuse(operation: str) -> NoReturn:
        """Raise the stable refusal shared by every persistent mutation door."""
        raise GrafxUnsupportedOperation(
            f"Storage operation {operation!r} is unavailable through a read-only handle.",
            operation=operation,
            read_only=True,
        )
