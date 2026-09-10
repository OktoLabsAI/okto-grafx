"""Explicit fused control reads selected at the storage-adapter boundary."""

from __future__ import annotations

from okto_grafx.domain.ports.storage import StorageDevice


def read_control_if_exists(
    storage: StorageDevice, file: str, offset: int, length: int
) -> bytes | None:
    """Use only a concrete type's declared fused capability, otherwise the port.

    A forwarding wrapper or inherited method alone does not opt a concrete type
    into the fused operation's identity semantics. Recheck on every call and then
    resolve through the instance, preserving instrumentation and replacement of
    the declared method. No descriptor/authority result is cached. In the fallback,
    a removal between exists and read_log remains an error, never invented absence.
    """
    implementation = vars(type(storage)).get("read_log_if_exists")
    if callable(implementation):
        fused_read = getattr(storage, "read_log_if_exists")
        return fused_read(file, offset, length)
    if not storage.exists(file):
        return None
    return storage.read_log(file, offset, length)

__all__ = ["read_control_if_exists"]
