"""Short synthetic comparison of full-row and endpoint-only relationship decode.

This is evidence, not a release gate.  Both arms parse the same encoded payload; the projected
arm also validates every property but retains only the two fixed relationship endpoints.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from okto_grafx.domain.model.schema import (
    ColumnDef,
    TableDef,
    decode_relationship_endpoints,
    decode_tuple,
    encode_tuple,
)
from okto_grafx.domain.model.value import ValueType, VectorValue


def _payload() -> tuple[TableDef, bytes]:
    table = TableDef(
        table_id=1,
        name="SyntheticEdge",
        kind="rel",
        columns=(
            ColumnDef(name="note", type=ValueType.STRING, nullable=False),
            ColumnDef(name="blob", type=ValueType.BYTES, nullable=False),
            ColumnDef(name="numbers", type=ValueType.LIST, nullable=False),
            ColumnDef(
                name="embedding",
                type=ValueType.VECTOR_F32,
                nullable=False,
                vector_space="synthetic",
            ),
            ColumnDef(name="metadata", type=ValueType.MAP, nullable=False),
        ),
        from_table="Node",
        to_table="Node",
    )
    values = (
        11,
        22,
        "n" * 2048,
        b"b" * 2048,
        tuple(range(64)),
        VectorValue(tuple(index / 384 for index in range(384)), space_ref=1),
        {"kind": "synthetic", "nested": (1, 2, 3)},
    )
    return table, encode_tuple(table, values)


def _sample(table: TableDef, payload: bytes, iterations: int, *, projected: bool) -> int:
    checksum = 0
    started = time.perf_counter_ns()
    if projected:
        for _ in range(iterations):
            source, target = decode_relationship_endpoints(table, payload)
            checksum ^= source ^ target
    else:
        for _ in range(iterations):
            values = decode_tuple(table, payload)
            checksum ^= int(values[0]) ^ int(values[1])
    elapsed = time.perf_counter_ns() - started
    if checksum not in (0, 29):
        raise AssertionError(f"unexpected benchmark checksum {checksum}")
    return elapsed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=2_000)
    parser.add_argument("--rounds", type=int, default=7)
    args = parser.parse_args()
    if args.iterations < 1 or args.rounds < 1:
        parser.error("iterations and rounds must be positive")

    table, payload = _payload()
    _sample(table, payload, 20, projected=False)
    _sample(table, payload, 20, projected=True)
    full = [
        _sample(table, payload, args.iterations, projected=False)
        for _ in range(args.rounds)
    ]
    projected = [
        _sample(table, payload, args.iterations, projected=True)
        for _ in range(args.rounds)
    ]
    full_ns = statistics.median(full) / args.iterations
    projected_ns = statistics.median(projected) / args.iterations
    print(
        json.dumps(
            {
                "benchmark": "incident_endpoint_decode_v1",
                "python": platform.python_version(),
                "payload_bytes": len(payload),
                "iterations_per_round": args.iterations,
                "rounds": args.rounds,
                "full_decode_ns_per_row_median": full_ns,
                "projected_decode_ns_per_row_median": projected_ns,
                "speedup": full_ns / projected_ns,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
