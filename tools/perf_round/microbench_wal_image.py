"""Synthetic microbench for D-09: old vs new generation of one local WAL page image.

``[MEDIDO-micro]`` by construction: pure Python, synthetic pages, this process only. It never
opens a database and proves nothing about the commit hold; that number belongs to the D-26
instrumentation on a real run. It exists so the "195 -> 87 us" order of magnitude the design
quoted is reproducible from the tree, with a receipt.

Usage::

    python tools/perf_round/microbench_wal_image.py --out <receipt.json> [--slots 40] [--rounds 2000]
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from okto_grafx.adapters.codec_v1 import PageCodecV1  # noqa: E402
from okto_grafx.domain.ids import NO_CSN, NO_PAGE, PROVISIONAL_CSN, RecordRef  # noqa: E402
from okto_grafx.domain.model.record import RecordHeader  # noqa: E402
from okto_grafx.domain.page import Page, PageType  # noqa: E402
from tools.perf_round.receipt import build_receipt, guard_not_data_home, write_receipt  # noqa: E402


def _page(page_size: int, slots: int) -> Page:
    page = Page(
        int(PageType.HEAP), page_size=page_size, page_index=5, page_lsn=3, seq=2
    )
    for ordinal in range(slots):
        header = RecordHeader(
            record_id=ordinal + 1,
            xmin=PROVISIONAL_CSN,
            xmax=NO_CSN,
            prev_version=RecordRef(NO_PAGE, 0).encode(),
            payload_len=64,
            schema_version=1,
            flags=0,
            reserved=0,
        ).encode()
        page.insert_slot(header + bytes(64))
    return page


def _stamp(page: Page, csn: int) -> None:
    if page.page_lsn < csn:
        page.page_lsn = csn


def _old(codec: PageCodecV1, page: Page, csn: int) -> bytes:
    decoded = codec.decode_page(codec.encode_page(page), verify=True)
    _stamp(decoded, csn)
    return codec.encode_page(decoded)


def _new(codec: PageCodecV1, page: Page, csn: int) -> bytes:
    clone = page.copy()
    _stamp(clone, csn)
    return codec.encode_page(clone)


def _measure(function, codec: PageCodecV1, page: Page, rounds: int) -> list[float]:
    samples = []
    for csn in range(1, rounds + 1):
        started = time.perf_counter_ns()
        function(codec, page, csn)
        samples.append((time.perf_counter_ns() - started) / 1000.0)
    return samples


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--page-size", type=int, default=8192)
    parser.add_argument("--slots", type=int, default=40)
    parser.add_argument("--rounds", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    out = guard_not_data_home(args.out)
    codec = PageCodecV1(page_size=args.page_size)
    page = _page(args.page_size, args.slots)
    assert _old(codec, page, 7) == _new(codec, page, 7)
    results = {}
    for name, function in (
        ("old_encode_decode_encode", _old),
        ("new_copy_encode", _new),
    ):
        samples = _measure(function, codec, page, args.rounds)
        samples.sort()
        results[name] = {
            "unit": "microseconds_per_page",
            "n": len(samples),
            "min": samples[0],
            "p50": statistics.median(samples),
            "p90": samples[int(0.9 * (len(samples) - 1))],
            "max": samples[-1],
        }
    results["ratio_p50_old_over_new"] = (
        results["old_encode_decode_encode"]["p50"] / results["new_copy_encode"]["p50"]
    )
    results["byte_identical"] = True
    receipt = build_receipt(
        tool=__file__,
        seed=args.seed,
        parameters={
            "page_size": args.page_size,
            "slots": args.slots,
            "rounds": args.rounds,
        },
        series={"mode": "n/a", "thermal": "warm", "kind": "instrumented"},
        config={"descriptor_revalidation": "n/a", "page_size": args.page_size},
        inputs=[],
        results=results,
        notes=[
            "MEDIDO-micro: synthetic page, pure Python, this process only; not a hold measurement"
        ],
    )
    write_receipt(out, receipt)
    for name in ("old_encode_decode_encode", "new_copy_encode"):
        print(
            f"{name}: p50={results[name]['p50']:.1f} us  p90={results[name]['p90']:.1f} us  (n={results[name]['n']})"
        )
    print(f"ratio p50 old/new = {results['ratio_p50_old_over_new']:.2f}; receipt {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
