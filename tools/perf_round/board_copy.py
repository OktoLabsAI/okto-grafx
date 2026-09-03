"""Copy a board directory and PROVE the copy (P0.3 step 1): inventory the source, copy, inventory both.

File-level only: this tool never opens a database, so it cannot run recovery or write a byte into
the source (the ``okto_grafx`` package is imported by the receipt solely to record which Grafx
this environment resolves, never to open anything). The proof is three inventories -- source before the
copy, the copy, and the source again after the copy -- and the inventory itself refuses a tree
that is not independent (symlink, junction, multiply-linked file, entry resolving outside). If
the source changed while it was being copied, the copy is not a copy of anything that existed:
the manifest says ``byte_identical: false``, the census/baseline tools refuse it, and this tool
exits 3.

The copy is built in a sibling temporary directory (``<dest>.tmp-<pid>``), verified there, and
only then renamed into place, so a destination directory that exists is always a finished,
verified copy or nothing.

Guards (fail-closed):

* the destination is refused inside the live data home and must NOT exist;
* a source inside the live data home is refused unless ``--source-is-drained-live-board`` states
  that the worker has drained (plan P0.3: "depois de o backfill terminar");
* an empty source tree is refused (there is nothing to prove);
* the destination must lie outside the source and the source outside the destination;
* the receipt (and its sidecar) is refused under the source, the destination or any data home.

Usage::

    python tools/perf_round/board_copy.py --source <board> --dest <copy> --declare-copy \\
        --grafx-sha SHA --pulse-sha SHA [--source-is-drained-live-board]

Exit codes: 0 proved, 2 refused (guards/arguments), 3 NOT proved and staging discarded.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.perf_round.receipt import (  # noqa: E402
    COPY_MANIFEST_NAME,
    COPY_MANIFEST_SCHEMA,
    LiveBoardRefused,
    build_receipt,
    canonical_json,
    data_homes,
    declared_copy_input,
    guard_not_data_home,
    inventory,
    plain_input,
    sha256_text,
    tool_sha256,
    utc_now,
    write_receipt,
)


class CopyNotProved(RuntimeError):
    """The source changed during the copy; the unproved staging tree was discarded."""

    def __init__(self, manifest: dict) -> None:
        super().__init__("source changed during the copy")
        self.manifest = manifest


def _inside_data_home(path: Path) -> bool:
    return any(path == home or home in path.parents for home in data_homes())


def receipt_location(requested: Path | None, source: Path, dest: Path) -> Path:
    """Canonicalise the receipt path and refuse it under the source, the destination or a data home."""
    candidate = requested or (
        dest.resolve().parent / f"{dest.resolve().name}.copy-receipt.json"
    )
    resolved = guard_not_data_home(candidate)
    for forbidden, label in (
        (source.resolve(), "source"),
        (dest.resolve(), "destination"),
    ):
        if resolved == forbidden or forbidden in resolved.parents:
            raise LiveBoardRefused(
                f"receipt {resolved} would be written inside the {label} {forbidden}"
            )
    sidecar = Path(str(resolved) + ".sha256")
    if resolved.exists() or sidecar.exists():
        raise LiveBoardRefused(f"receipt {resolved} or its sidecar already exists")
    return resolved


def _source_allowed(source: Path, drained: bool) -> Path:
    resolved = source.resolve()
    if not resolved.is_dir():
        raise LiveBoardRefused(f"source {resolved} is not a directory")
    containing_homes = [home for home in data_homes() if resolved in home.parents]
    if containing_homes:
        raise LiveBoardRefused(
            f"source {resolved} contains live data home(s) {containing_homes}; "
            "select one drained board, not an ancestor"
        )
    if _inside_data_home(resolved) and not drained:
        raise LiveBoardRefused(
            f"source {resolved} lies inside the live data home; pass "
            "--source-is-drained-live-board only after the worker has drained."
        )
    return resolved


def copy_board(
    source: Path,
    dest: Path,
    *,
    declare_copy: bool,
    drained: bool,
    grafx_sha: str | None = None,
    pulse_sha: str | None = None,
    pulse_core_sha: str | None = None,
    timestamp: str | None = None,
) -> dict:
    """Copy ``source`` into ``dest`` through a verified sibling temp; return the manifest written."""
    if not declare_copy:
        raise LiveBoardRefused(
            "--declare-copy is mandatory: the copy must be declared explicitly"
        )
    source = _source_allowed(source, drained)
    dest = guard_not_data_home(dest)
    if dest.exists():
        raise LiveBoardRefused(
            f"destination {dest} already exists; a copy is written only to a new path"
        )
    if dest == source or source in dest.parents or dest in source.parents:
        raise LiveBoardRefused("destination and source must not contain each other")

    before = inventory(source)
    if before["file_count"] == 0:
        raise LiveBoardRefused(
            f"source {source} holds no files; nothing to copy or prove"
        )
    staging = dest.parent / f"{dest.name}.tmp-{os.getpid()}"
    if staging.exists():
        raise LiveBoardRefused(f"staging directory {staging} already exists")
    staging.mkdir(parents=True)
    try:
        for entry in before["files"]:
            target = staging / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / entry["path"], target, follow_symlinks=False)
        copied = inventory(staging)
        after = inventory(source)
        identical = before["sha256"] == after["sha256"] == copied["sha256"]
        manifest = {
            "schema": COPY_MANIFEST_SCHEMA,
            "timestamp_utc": timestamp or utc_now(),
            "tool": {
                "path": str(Path(__file__).resolve()),
                "sha256": tool_sha256(__file__),
            },
            "declared_copy": True,
            "byte_identical": identical,
            "source": {
                "path": str(source),
                "inside_data_home": _inside_data_home(source),
                "sha256_before_copy": before["sha256"],
                "sha256_after_copy": after["sha256"],
                "file_count": before["file_count"],
                "total_bytes": before["total_bytes"],
            },
            "copy": {
                "path": str(dest),
                "sha256": copied["sha256"],
                "file_count": copied["file_count"],
                "total_bytes": copied["total_bytes"],
                "files": copied["files"],
            },
            "commits": {
                "grafx": grafx_sha,
                "pulse": pulse_sha,
                "pulse_core": pulse_core_sha,
            },
        }
        if not identical:
            raise CopyNotProved(manifest)
        text = canonical_json(manifest)
        (staging / COPY_MANIFEST_NAME).write_bytes(text.encode("utf-8"))
        (staging / (COPY_MANIFEST_NAME + ".sha256")).write_bytes(
            (sha256_text(text) + "\n").encode("ascii")
        )
        os.replace(staging, dest)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--dest", type=Path, required=True)
    parser.add_argument("--declare-copy", action="store_true")
    parser.add_argument("--source-is-drained-live-board", action="store_true")
    parser.add_argument("--grafx-sha", required=True)
    parser.add_argument("--pulse-sha", required=True)
    parser.add_argument(
        "--pulse-core-sha",
        help="exact okto-pulse-core commit, when the copy will feed a Pulse workload",
    )
    parser.add_argument(
        "--receipt",
        type=Path,
        help="where to write the receipt (default: <dest>/../<dest name>.copy-receipt.json)",
    )
    args = parser.parse_args(argv)
    proof_failed = False
    try:
        receipt_path = receipt_location(args.receipt, args.source, args.dest)
        try:
            manifest = copy_board(
                args.source,
                args.dest,
                declare_copy=args.declare_copy,
                drained=args.source_is_drained_live_board,
                grafx_sha=args.grafx_sha,
                pulse_sha=args.pulse_sha,
                pulse_core_sha=args.pulse_core_sha,
            )
        except CopyNotProved as unproved:
            manifest = unproved.manifest
            proof_failed = True
    except LiveBoardRefused as refused:
        print(f"REFUSED: {refused}", file=sys.stderr)
        return 2
    inputs = [
        plain_input(
            "source",
            manifest["source"]["path"],
            inventory_digest=manifest["source"]["sha256_before_copy"],
        )
    ]
    if not proof_failed:
        # The copy earns its flag only by passing the same proof every consumer will run.
        inputs.append(declared_copy_input("copy", args.dest))
    else:
        inputs.append(
            plain_input(
                "copy_unproved_discarded",
                args.dest,
                inventory_digest=manifest["copy"]["sha256"],
            )
        )
    receipt = build_receipt(
        tool=__file__,
        seed=None,
        parameters={
            "source": str(args.source),
            "dest": str(args.dest),
            "drained_live_board": bool(args.source_is_drained_live_board),
        },
        series={"mode": "n/a", "thermal": "cold", "kind": "raw"},
        config={"descriptor_revalidation": "n/a"},
        inputs=inputs,
        results={
            "byte_identical": manifest["byte_identical"],
            "destination_published": not proof_failed,
            "file_count": manifest["copy"]["file_count"],
            "total_bytes": manifest["copy"]["total_bytes"],
        },
        grafx_sha=args.grafx_sha,
        pulse_sha=args.pulse_sha,
        pulse_core_sha=args.pulse_core_sha,
        notes=[
            "file-level copy through a verified sibling temp directory; okto_grafx imported only for provenance; no database was opened"
        ],
    )
    write_receipt(receipt_path, receipt)
    status = (
        "PROVED"
        if not proof_failed
        else "NOT PROVED (source changed; staging discarded)"
    )
    print(
        f"{status}: {manifest['copy']['file_count']} files, {manifest['copy']['total_bytes']} bytes, inventory {manifest['copy']['sha256']}; receipt {receipt_path}"
    )
    return 3 if proof_failed else 0


if __name__ == "__main__":
    sys.exit(main())
