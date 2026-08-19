"""Temporary probe: restores the old behaviour so the new tests can be shown to fail."""
from __future__ import annotations
import os
from okto_grafx.adapters import storage_fault, storage_local

__all__ = ["pytest_configure"]

MODE = os.environ.get("GRAFX_OLD", "")


def pytest_configure(config: object) -> None:
    if MODE == "b1":
        storage_local._may_unlink = lambda relative, identity, path: True
    if MODE == "m4":
        def _pin(self, file):
            self._volatile.clear()
        storage_fault.FaultInjectingStorageDevice._pin = _pin
    if MODE == "m6":
        def _run(self, sequence, action):
            return action()
        storage_fault.FaultInjectingStorageDevice._run = _run
    if MODE == "m7":
        def _barrier(self, file=None):
            names = tuple(self._handles) if file is None else (storage_local.normalize_logical_name(file),)
            for name in names:
                os.fsync(self._descriptor(name))
        storage_local.LocalStorageDevice.durable_barrier = _barrier
    if MODE == "b1full":
        storage_local._may_unlink = lambda relative, identity, path: True
        storage_local.LocalStorageDevice._forget_deferred = lambda self, name: None
    if MODE == "b2":
        storage_fault.FaultInjectingStorageDevice._capture_state = lambda self, file: None
        def _crash(self, sequence, method, file, moment):
            self._mark(sequence, f"crash_{moment}")
            if self._plan.lying_barrier:
                self._discard_volatile()
            raise storage_fault.SimulatedCrash(
                "old", sequence=sequence, method=method, file=file, moment=moment
            )
        storage_fault.FaultInjectingStorageDevice._crash = _crash
    if MODE == "m5":
        def _pin(self, file):
            if file is None:
                self._volatile = [item for item in self._volatile if item.reordered]
                return
            self._volatile = [
                item for item in self._volatile if item.file != file or item.reordered
            ]
        storage_fault.FaultInjectingStorageDevice._pin = _pin
    if MODE == "m2":
        def _recycle(self, name, path, identity):
            try:
                storage_local._remove_file(path)
            except OSError:
                pending = self._pending_path(path)
                try:
                    os.replace(path, pending)
                except OSError:
                    return self._defer(path, identity)
                return self._defer(pending, identity)
            return True
        storage_local.LocalStorageDevice._recycle_on_windows = _recycle
    if MODE == "m3":
        def _device_failure(self, operation, name, failure, **details):
            from okto_grafx.domain.errors import GrafxCorruptionDetected
            return GrafxCorruptionDetected("old behaviour", reason="device_failure", file=name)
        storage_local.LocalStorageDevice._device_failure = _device_failure
