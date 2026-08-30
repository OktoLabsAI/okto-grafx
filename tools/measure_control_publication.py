"""Measure the CE-1 control-record publication primitive without changing production.

The ``atomic_replace`` case is the protocol used by control records today.  The
``two_slot`` case is an exploratory lower bound for CE-1: one persistent descriptor,
two fixed-size slots, a positional write and one durability barrier.  This tool does
not change the on-disk format and is not evidence that the two-slot protocol is safe;
format, migration and crash/fault proofs remain separate gates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import statistics
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "src"))

from okto_grafx.adapters.storage_local import LocalStorageDevice, _open_descriptor  # noqa: E402

_TARGET = "control/state"
_STAGING = "control/state.spike.tmp"


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _nearest_rank(samples: Sequence[int], fraction: float) -> int:
    """Return a nearest-rank percentile for a non-empty integer sample."""
    if not samples:
        raise ValueError("a percentile needs at least one sample")
    ordered = sorted(samples)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def summarize(samples: Sequence[int]) -> dict[str, int | float]:
    """Summarize nanosecond samples without discarding the auditable raw values."""
    if not samples:
        raise ValueError("a summary needs at least one sample")
    return {
        "count": len(samples),
        "minimum_ns": min(samples),
        "median_ns": statistics.median(samples),
        "p95_ns": _nearest_rank(samples, 0.95),
        "maximum_ns": max(samples),
    }


def _payload(iteration: int, size: int) -> bytes:
    marker = iteration.to_bytes(8, "little", signed=False)
    return marker + bytes(
        ((iteration * 17 + index) % 251 for index in range(size - len(marker)))
    )


def _timed(operation: Callable[[], None]) -> int:
    started = time.perf_counter_ns()
    operation()
    return time.perf_counter_ns() - started


def _measure_current(
    root: Path, warmups: int, samples: int, payload_size: int
) -> dict[str, Any]:
    device = LocalStorageDevice(root / "atomic-replace")
    phases: dict[str, list[int]] = {
        "prepare": [],
        "append": [],
        "stage_barrier": [],
        "replace": [],
        "target_barrier": [],
    }
    totals: list[int] = []
    last_payload = b""
    try:
        for iteration in range(warmups + samples):
            payload = _payload(iteration, payload_size)
            sample_phases: dict[str, int] = {}
            started = time.perf_counter_ns()

            def prepare() -> None:
                if device.exists(_STAGING):
                    device.remove(_STAGING)
                device.create(_STAGING, exclusive=True)

            sample_phases["prepare"] = _timed(prepare)
            sample_phases["append"] = _timed(
                lambda: device.append_log(_STAGING, payload)
            )
            sample_phases["stage_barrier"] = _timed(
                lambda: device.durable_barrier(_STAGING)
            )
            sample_phases["replace"] = _timed(
                lambda: device.atomic_replace(_STAGING, _TARGET)
            )
            sample_phases["target_barrier"] = _timed(
                lambda: device.durable_barrier(_TARGET)
            )
            total = time.perf_counter_ns() - started
            last_payload = payload
            if iteration >= warmups:
                totals.append(total)
                for name, elapsed in sample_phases.items():
                    phases[name].append(elapsed)

        observed_size = device.log_size(_TARGET)
        observed = device.read_log(_TARGET, 0, observed_size)
        if observed != last_payload:
            raise RuntimeError(
                "atomic-replace readback differs from the last published payload"
            )
    finally:
        device.close()
    return {
        "total": {"summary": summarize(totals), "samples_ns": totals},
        "phases": {
            name: {"summary": summarize(values), "samples_ns": values}
            for name, values in phases.items()
        },
    }


def _write_at(descriptor: int, payload: bytes, offset: int) -> None:
    """Write all bytes at offset; use pwrite where the runtime exposes it."""
    written = 0
    pwrite = getattr(os, "pwrite", None)
    if pwrite is not None:
        while written < len(payload):
            count = pwrite(descriptor, payload[written:], offset + written)
            if count <= 0:
                raise OSError("positional write made no progress")
            written += count
        return
    os.lseek(descriptor, offset, os.SEEK_SET)
    while written < len(payload):
        count = os.write(descriptor, payload[written:])
        if count <= 0:
            raise OSError("write made no progress")
        written += count


def _read_at(descriptor: int, length: int, offset: int) -> bytes:
    pread = getattr(os, "pread", None)
    if pread is not None:
        return pread(descriptor, length, offset)
    os.lseek(descriptor, offset, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _measure_two_slot(
    root: Path,
    warmups: int,
    samples: int,
    payload_size: int,
    slot_size: int,
) -> dict[str, Any]:
    directory = root / "two-slot"
    directory.mkdir(parents=True)
    path = directory / "control.state"
    descriptor = _open_descriptor(str(path), create_new=True)
    phases: dict[str, list[int]] = {"write": [], "barrier": []}
    totals: list[int] = []
    expected: dict[int, bytes] = {}
    try:
        os.ftruncate(descriptor, slot_size * 2)
        os.fsync(descriptor)
        for iteration in range(warmups + samples):
            slot = iteration % 2
            payload = _payload(iteration, payload_size)
            padded = payload + bytes(slot_size - len(payload))
            started = time.perf_counter_ns()
            write_ns = _timed(lambda: _write_at(descriptor, padded, slot * slot_size))
            barrier_ns = _timed(lambda: os.fsync(descriptor))
            total = time.perf_counter_ns() - started
            expected[slot] = padded
            if iteration >= warmups:
                totals.append(total)
                phases["write"].append(write_ns)
                phases["barrier"].append(barrier_ns)

        for slot, wanted in expected.items():
            observed = _read_at(descriptor, slot_size, slot * slot_size)
            if observed != wanted:
                raise RuntimeError(f"two-slot readback differs in slot {slot}")
    finally:
        os.close(descriptor)
    return {
        "write_primitive": "os.pwrite" if hasattr(os, "pwrite") else "lseek+os.write",
        "barrier_primitive": "os.fsync",
        "total": {"summary": summarize(totals), "samples_ns": totals},
        "phases": {
            name: {"summary": summarize(values), "samples_ns": values}
            for name, values in phases.items()
        },
    }


def measure(
    root: Path,
    *,
    warmups: int,
    samples: int,
    payload_size: int,
    slot_size: int,
    order: str = "atomic-first",
) -> dict[str, Any]:
    """Run both protocols in isolated subdirectories and return an auditable report."""
    if payload_size < 8:
        raise ValueError("payload_size must be at least 8 bytes")
    if slot_size < payload_size:
        raise ValueError("slot_size must be at least payload_size")
    if order not in {"atomic-first", "two-slot-first"}:
        raise ValueError("order must be atomic-first or two-slot-first")
    root.mkdir(parents=True, exist_ok=True)
    if order == "atomic-first":
        current = _measure_current(root, warmups, samples, payload_size)
        two_slot = _measure_two_slot(root, warmups, samples, payload_size, slot_size)
    else:
        two_slot = _measure_two_slot(root, warmups, samples, payload_size, slot_size)
        current = _measure_current(root, warmups, samples, payload_size)
    current_median = float(current["total"]["summary"]["median_ns"])
    two_slot_median = float(two_slot["total"]["summary"]["median_ns"])
    return {
        "schema": "okto-grafx.ce1-publication-spike.v1",
        "scope": "exploratory primitive only; no production format or code change",
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
            "os_name": os.name,
            "working_volume": str(root.resolve().anchor),
            "tool_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        },
        "parameters": {
            "warmups": warmups,
            "samples": samples,
            "payload_size": payload_size,
            "slot_size": slot_size,
            "order": order,
        },
        "atomic_replace": current,
        "two_slot": two_slot,
        "median_speedup": current_median / two_slot_median,
        "gates_remaining": [
            "on-disk format amendment and migration ADR",
            "independent crash/fault matrix proving one valid slot always survives",
            "multi-process reader/writer equivalence and regression suite",
        ],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, help="parent directory for isolated measurement files"
    )
    parser.add_argument("--warmups", type=_positive, default=10)
    parser.add_argument("--samples", type=_positive, default=100)
    parser.add_argument("--payload-size", type=_positive, default=64)
    parser.add_argument("--slot-size", type=_positive, default=4096)
    parser.add_argument(
        "--order",
        choices=("atomic-first", "two-slot-first"),
        default="atomic-first",
        help="protocol execution order, recorded to expose order bias",
    )
    parser.add_argument("--output", type=Path, help="optional JSON output path")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.payload_size < 8:
        _parser().error("--payload-size must be at least 8")
    if args.slot_size < args.payload_size:
        _parser().error("--slot-size must be at least --payload-size")
    if args.root is None:
        with tempfile.TemporaryDirectory(prefix="okto-grafx-ce1-") as temporary:
            report = measure(
                Path(temporary),
                warmups=args.warmups,
                samples=args.samples,
                payload_size=args.payload_size,
                slot_size=args.slot_size,
                order=args.order,
            )
    else:
        report = measure(
            args.root,
            warmups=args.warmups,
            samples=args.samples,
            payload_size=args.payload_size,
            slot_size=args.slot_size,
            order=args.order,
        )
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
