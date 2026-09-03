"""Provenance receipts and fail-closed guards shared by the round-0.0.2 instruments.

A receipt answers, for one raw output, every question the measurement policy asks (plan section
7): which script (path and SHA-256), which seed and parameters, which Grafx/Pulse commits, which
Python, OS and dependencies, which ``okto_grafx.__file__``, which ``descriptor_revalidation``
mode, cold or warm, raw or instrumented, which inputs (and whether each is a DECLARED COPY), with
their inventory hashes.

Two digests, because a receipt has two natures:

* ``identity_sha256`` -- over the STABLE part only (schema, tool, seed, parameters, series,
  config, commits, inputs): two runs of the same instrument on the same inputs share it;
* ``receipt_sha256`` -- over the whole occurrence except the timestamp and the digests: the
  environment, the machine sample and the results are volatile by nature and are meant to be.

Receipts are pure JSON: only ``dict``/``list``/``str``/``int``/``float``/``bool``/``None``
reach the file, NaN and infinity are refused, keys are sorted, so the bytes are canonical.

Guards:

* :func:`guard_not_data_home` refuses any path at or under a live data home. The live homes are
  the Pulse default ``~/.okto-pulse`` plus whatever ``OKTO_PULSE_HOME``, ``DATA_DIR``,
  ``KG_BASE_DIR`` or their ``OKTO_PULSE_``-prefixed forms name (Pulse settings ``data_dir`` and
  ``kg_base_dir``, ``acceptance.py:23`` for the home);
* :func:`inventory` refuses a tree that is not INDEPENDENT: a symlink, junction or other reparse
  point anywhere in it, a file with more than one hard link, or an entry whose real path leaves
  the root -- any of those could alias the live board;
* :func:`require_declared_copy` accepts a board directory only when it carries the manifest
  :mod:`tools.perf_round.board_copy` wrote, marked ``declared_copy: true`` and ``byte_identical:
  true``, well-typed, and its files still hash to what the manifest recorded. A malformed
  manifest is a typed refusal, never a stray decode error.

The SHA-256 sidecars make accidental or uncoordinated byte changes detectable; they are not a
signature against a malicious local operator. A signing/authority bundle remains a separately
governed future security feature and is deliberately not introduced by this performance round.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import importlib
import importlib.metadata
import json
import math
import os
import platform
import stat
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCHEMA = "okto-grafx.perf-round-0.0.2.receipt.v1"
COPY_MANIFEST_SCHEMA = "okto-grafx.perf-round-0.0.2.copy-manifest.v1"
COPY_MANIFEST_NAME = "COPY_MANIFEST.json"
DATA_HOME_ENV = (
    "OKTO_PULSE_HOME",
    "OKTO_PULSE_DATA_DIR",
    "OKTO_PULSE_KG_BASE_DIR",
    "DATA_DIR",
    "KG_BASE_DIR",
)
DEFAULT_DATA_HOME_NAME = ".okto-pulse"
MODES = ("strict", "generation", "n/a")
THERMALS = ("cold", "warm", "mixed")
KINDS = ("raw", "instrumented")
TRACKED_MODULES = ("numpy", "psutil", "google-crc32c", "pytest", "py-spy", "ladybug")
_IDENTITY_KEYS = (
    "schema",
    "tool",
    "seed",
    "parameters",
    "series",
    "config",
    "commits",
    "inputs",
)
_VOLATILE_KEYS = ("timestamp_utc", "identity_sha256", "receipt_sha256")
_HEX64 = frozenset("0123456789abcdef")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_IMPORT_ROOT = PROJECT_ROOT / "src"


class LiveBoardRefused(RuntimeError):
    """The path names (or could alias) the live data home, or is not a proved declared copy."""


class ReceiptInvalid(ValueError):
    """A receipt misses a required field, is not pure JSON, or uses a value outside the vocabulary."""


# --- hashing and inventories ------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_hex64(value: object) -> bool:
    return type(value) is str and len(value) == 64 and set(value) <= _HEX64


def _is_reparse_point(path: Path) -> bool:
    """True for a symlink, a Windows junction or any other reparse point (never followed)."""
    if path.is_symlink():
        return True
    try:
        attributes = os.lstat(path).st_file_attributes  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def inventory(root: Path | str, *, exclude: Iterable[str] = ()) -> dict[str, Any]:
    """Return every regular file under ``root`` with size and SHA-256, plus one digest of the list.

    The list digest covers ``path\\0size\\0sha256\\n`` per file in POSIX-path order, so two trees
    with the same files, sizes and bytes have the same digest wherever they live. The walk never
    follows links: a reparse point (symlink/junction), a multiply-linked file or an entry whose
    real path leaves the root makes the tree NOT independent and is refused.
    """
    root = Path(root)
    if _is_reparse_point(root):
        raise LiveBoardRefused(
            f"{root} is a symlink/junction; an inventory needs a real directory"
        )
    root = root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"{root} is not a directory")
    skipped = set(exclude)
    files: list[dict[str, Any]] = []

    def refuse_walk_error(failure: OSError) -> None:
        raise LiveBoardRefused(
            f"cannot inventory {failure.filename or root}: {failure}"
        ) from failure

    for directory, subdirectories, names in os.walk(
        root, followlinks=False, onerror=refuse_walk_error
    ):
        base = Path(directory)
        for name in list(subdirectories):
            if _is_reparse_point(base / name):
                raise LiveBoardRefused(
                    f"{base / name} is a symlink/junction inside {root}; the tree is not independent"
                )
        for name in sorted(names):
            candidate = base / name
            relative = candidate.relative_to(root).as_posix()
            if relative in skipped:
                continue
            if _is_reparse_point(candidate):
                raise LiveBoardRefused(
                    f"{candidate} is a symlink/reparse point inside {root}; the tree is not independent"
                )
            info = os.lstat(candidate)
            if not stat.S_ISREG(info.st_mode):
                raise LiveBoardRefused(f"{candidate} is not a regular file")
            if getattr(info, "st_nlink", 1) > 1:
                raise LiveBoardRefused(
                    f"{candidate} has {info.st_nlink} hard links; it may alias a file outside {root}"
                )
            real = candidate.resolve()
            if root not in real.parents:
                raise LiveBoardRefused(
                    f"{candidate} resolves to {real}, outside {root}"
                )
            files.append(
                {
                    "path": relative,
                    "size": int(info.st_size),
                    "sha256": sha256_file(candidate),
                }
            )
    files.sort(key=lambda entry: entry["path"])
    listing = "".join(f"{f['path']}\0{f['size']}\0{f['sha256']}\n" for f in files)
    return {
        "root": str(root),
        "file_count": len(files),
        "total_bytes": sum(int(f["size"]) for f in files),
        "sha256": sha256_text(listing),
        "files": files,
    }


# --- guards -----------------------------------------------------------------------------------


def data_homes() -> tuple[Path, ...]:
    """Return every directory that counts as the live data home (default plus env overrides)."""
    homes = [Path.home() / DEFAULT_DATA_HOME_NAME]
    for name in DATA_HOME_ENV:
        value = os.environ.get(name)
        if value:
            homes.append(Path(value).expanduser())
    resolved = []
    for home in homes:
        try:
            resolved.append(home.resolve())
        except OSError:
            resolved.append(home)
    return tuple(resolved)


def guard_not_data_home(path: Path | str) -> Path:
    """Refuse a path at or under any live data home (also through a link); return it resolved."""
    candidate = Path(path)
    resolved = candidate.resolve()
    for home in data_homes():
        for view in (resolved, candidate.absolute()):
            if view == home or home in view.parents or view in home.parents:
                raise LiveBoardRefused(
                    f"{candidate} overlaps the live data home {home}; instruments only accept an "
                    "explicitly declared, disjoint copy outside it."
                )
    return resolved


def _refuse(reason: str) -> LiveBoardRefused:
    return LiveBoardRefused(reason)


def require_declared_copy(path: Path | str) -> dict[str, Any]:
    """Accept a board directory only as the declared copy its manifest describes, re-hashed now."""
    root = guard_not_data_home(path)
    manifest_path = root / COPY_MANIFEST_NAME
    sidecar_path = root / (COPY_MANIFEST_NAME + ".sha256")
    if _is_reparse_point(manifest_path) or not manifest_path.is_file():
        raise _refuse(
            f"{root} carries no regular {COPY_MANIFEST_NAME}; only a copy produced by "
            "tools/perf_round/board_copy.py with --declare-copy is accepted."
        )
    if _is_reparse_point(sidecar_path) or not sidecar_path.is_file():
        raise _refuse(f"{root} carries no regular {sidecar_path.name}")
    for authority_file in (manifest_path, sidecar_path):
        try:
            authority_info = os.lstat(authority_file)
        except OSError as failure:
            raise _refuse(
                f"{authority_file} cannot be inspected: {failure}"
            ) from failure
        if (
            not stat.S_ISREG(authority_info.st_mode)
            or getattr(authority_info, "st_nlink", 1) != 1
        ):
            raise _refuse(f"{authority_file} is not an independent regular file")
        if root not in authority_file.resolve().parents:
            raise _refuse(f"{authority_file} resolves outside {root}")
    try:
        declared_manifest_hash = sidecar_path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError) as failure:
        raise _refuse(f"{sidecar_path} is unreadable: {failure}") from failure
    if not _is_hex64(declared_manifest_hash):
        raise _refuse(f"{sidecar_path} does not contain one lowercase SHA-256 digest")
    observed_manifest_hash = sha256_file(manifest_path)
    if observed_manifest_hash != declared_manifest_hash:
        raise _refuse(
            f"{manifest_path} does not match its sidecar "
            f"({observed_manifest_hash} != {declared_manifest_hash})"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as failure:
        raise _refuse(
            f"{manifest_path} is unreadable or not JSON: {failure}"
        ) from failure
    if not isinstance(manifest, dict):
        raise _refuse(f"{manifest_path} is not a JSON object")
    if manifest.get("schema") != COPY_MANIFEST_SCHEMA:
        raise _refuse(
            f"{manifest_path} has schema {manifest.get('schema')!r}, expected {COPY_MANIFEST_SCHEMA!r}"
        )
    if manifest.get("declared_copy") is not True:
        raise _refuse(f"{manifest_path} is not marked declared_copy: true")
    if manifest.get("byte_identical") is not True:
        raise _refuse(
            f"{manifest_path} records a copy that was NOT proved byte-identical to its source"
        )
    copy = manifest.get("copy")
    if not isinstance(copy, dict) or not _is_hex64(copy.get("sha256")):
        raise _refuse(f"{manifest_path} carries no well-formed copy.sha256")
    declared_path = copy.get("path")
    if type(declared_path) is not str or Path(declared_path).resolve() != root:
        raise _refuse(f"{manifest_path} copy.path does not identify {root}")
    current = inventory(
        root, exclude=(COPY_MANIFEST_NAME, COPY_MANIFEST_NAME + ".sha256")
    )
    if current["sha256"] != copy["sha256"]:
        raise _refuse(
            f"{root} no longer hashes to its manifest (inventory {current['sha256']} != {copy['sha256']}); "
            "the copy was modified after it was declared."
        )
    for field in ("file_count", "total_bytes"):
        if copy.get(field) != current[field]:
            raise _refuse(
                f"{manifest_path} copy.{field}={copy.get(field)!r} does not match "
                f"the current inventory {current[field]!r}"
            )
    if copy.get("files") != current["files"]:
        raise _refuse(
            f"{manifest_path} copy.files does not match the current inventory"
        )
    source = manifest.get("source")
    if type(source) is not dict:
        raise _refuse(f"{manifest_path} carries no source proof")
    if type(source.get("path")) is not str or not source["path"]:
        raise _refuse(f"{manifest_path} source.path is missing")
    if type(source.get("inside_data_home")) is not bool:
        raise _refuse(f"{manifest_path} source.inside_data_home is not a boolean")
    for field in ("sha256_before_copy", "sha256_after_copy"):
        if not _is_hex64(source.get(field)):
            raise _refuse(f"{manifest_path} source.{field} is not a SHA-256")
    if not (
        source["sha256_before_copy"] == source["sha256_after_copy"] == current["sha256"]
    ):
        raise _refuse(
            f"{manifest_path} does not prove one stable source and copy inventory"
        )
    for field in ("file_count", "total_bytes"):
        if source.get(field) != current[field]:
            raise _refuse(
                f"{manifest_path} source.{field}={source.get(field)!r} does not match "
                f"the current copy inventory {current[field]!r}"
            )
    return manifest


# --- environment ------------------------------------------------------------------------------


def tool_sha256(path: Path | str) -> str:
    return sha256_file(Path(path))


def git_sha_of(path: Path | str) -> str | None:
    """Return the HEAD commit of the repository containing ``path``, or None outside a repository."""
    candidate = Path(path).resolve()
    git_start = candidate if candidate.is_dir() else candidate.parent
    try:
        completed = subprocess.run(
            ["git", "-C", str(git_start), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def module_version(distribution: str) -> str | None:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None


def grafx_identity() -> dict[str, Any]:
    """Resolve Grafx from this checkout, insulated from an unrelated editable installation."""
    probe_environment = dict(os.environ)
    inherited_pythonpath = probe_environment.get("PYTHONPATH")
    probe_environment["PYTHONPATH"] = str(SOURCE_IMPORT_ROOT) + (
        os.pathsep + inherited_pythonpath if inherited_pythonpath else ""
    )
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json, okto_grafx; print(json.dumps({'file': okto_grafx.__file__, "
                "'version': getattr(okto_grafx, '__version__', None)}))",
            ],
            cwd=PROJECT_ROOT,
            env=probe_environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        probed = json.loads(completed.stdout) if completed.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        probed = {}
    file = probed.get("file") if isinstance(probed, dict) else None
    version = probed.get("version") if isinstance(probed, dict) else None
    return {
        "file": str(file) if file else None,
        "version": str(version) if version else None,
        "git_sha": git_sha_of(file) if file else None,
    }


def environment(extra_modules: Sequence[str] = ()) -> dict[str, Any]:
    return {
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
            "executable": sys.executable,
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "processor": platform.processor(),
        },
        "okto_grafx": grafx_identity(),
        "dependencies": {
            name: module_version(name) for name in (*TRACKED_MODULES, *extra_modules)
        },
    }


def machine_sample(interval_seconds: float = 1.0) -> dict[str, Any]:
    """Observe the box (never assert it idle): CPU percent over one interval, cores, memory."""
    sample: dict[str, Any] = {
        "psutil": None,
        "cpu_percent": None,
        "cpu_count": os.cpu_count(),
    }
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        return sample
    sample["psutil"] = str(getattr(psutil, "__version__", "?"))
    sample["cpu_percent"] = float(psutil.cpu_percent(interval=interval_seconds))
    memory = psutil.virtual_memory()
    sample["memory_total_bytes"] = int(memory.total)
    sample["memory_available_bytes"] = int(memory.available)
    return sample


# --- receipts ---------------------------------------------------------------------------------


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def plain_input(
    role: str, path: Path | str, *, inventory_digest: str | None
) -> dict[str, Any]:
    """An input that is NOT a declared copy (a statements file, a workspace, an output dir)."""
    return {
        "role": role,
        "path": str(Path(path).resolve()),
        "declared_copy": False,
        "inventory_sha256": inventory_digest,
    }


def declared_copy_input(role: str, path: Path | str) -> dict[str, Any]:
    """An input that IS a declared copy: the flag is derived from the proof, never asserted."""
    manifest = require_declared_copy(path)
    return {
        "role": role,
        "path": str(Path(path).resolve()),
        "declared_copy": True,
        "inventory_sha256": manifest["copy"]["sha256"],
    }


def build_receipt(
    *,
    tool: Path | str,
    seed: int | None,
    parameters: Mapping[str, Any],
    series: Mapping[str, str],
    config: Mapping[str, Any],
    inputs: Sequence[Mapping[str, Any]],
    results: Mapping[str, Any] | None = None,
    grafx_sha: str | None = None,
    pulse_sha: str | None = None,
    machine_idle_asserted: bool = False,
    machine: Mapping[str, Any] | None = None,
    notes: Sequence[str] = (),
    timestamp: str | None = None,
) -> dict[str, Any]:
    tool_path = Path(tool).resolve()
    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "official": False,
        "timestamp_utc": timestamp or utc_now(),
        "tool": {"path": str(tool_path), "sha256": tool_sha256(tool_path)},
        "seed": seed,
        "parameters": dict(parameters),
        "series": {
            "mode": series["mode"],
            "thermal": series["thermal"],
            "kind": series["kind"],
        },
        "config": dict(config),
        "commits": {"grafx": grafx_sha, "pulse": pulse_sha},
        "environment": environment(),
        "machine": {
            "idle_asserted_by_operator": bool(machine_idle_asserted),
            "sample": dict(machine) if machine is not None else machine_sample(),
        },
        "inputs": [dict(item) for item in inputs],
        "results": dict(results or {}),
        "notes": list(notes),
    }
    validate_receipt(receipt)
    return receipt


_REQUIRED_TOP = (
    "schema",
    "official",
    "timestamp_utc",
    "tool",
    "seed",
    "parameters",
    "series",
    "config",
    "commits",
    "environment",
    "machine",
    "inputs",
    "results",
    "notes",
)


def _require_pure_json(value: Any, where: str) -> None:
    """Refuse anything json.dumps would only serialise through a default hook, plus NaN/inf."""
    if value is None or type(value) in (bool, str):
        return
    if type(value) is int:
        return
    if type(value) is float:
        if math.isnan(value) or math.isinf(value):
            raise ReceiptInvalid(f"{where}: NaN/infinity is not canonical JSON")
        return
    if type(value) is list:
        for index, item in enumerate(value):
            _require_pure_json(item, f"{where}[{index}]")
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ReceiptInvalid(f"{where}: key {key!r} is not a string")
            _require_pure_json(item, f"{where}.{key}")
        return
    raise ReceiptInvalid(f"{where}: {type(value).__name__} is not a JSON value")


def validate_receipt(receipt: Mapping[str, Any]) -> None:
    """Refuse a receipt that could not answer the measurement policy's questions."""
    if type(receipt) is not dict:
        raise ReceiptInvalid("receipt must be a plain JSON object")
    missing = [key for key in _REQUIRED_TOP if key not in receipt]
    if missing:
        raise ReceiptInvalid(f"receipt misses {missing}")
    _require_pure_json(dict(receipt), "receipt")
    if receipt["schema"] != SCHEMA:
        raise ReceiptInvalid(f"schema {receipt['schema']!r} is not {SCHEMA!r}")
    if receipt["official"] is not False:
        raise ReceiptInvalid("a raw receipt can never claim official: true")
    if type(receipt["timestamp_utc"]) is not str or not receipt["timestamp_utc"]:
        raise ReceiptInvalid("timestamp_utc must be a non-empty string")
    tool = receipt["tool"]
    if (
        type(tool) is not dict
        or type(tool.get("path")) is not str
        or not tool["path"]
        or not _is_hex64(tool.get("sha256"))
    ):
        raise ReceiptInvalid("tool needs a path and a 64-hex sha256")
    if receipt["seed"] is not None and type(receipt["seed"]) is not int:
        raise ReceiptInvalid("seed must be an integer or null")
    for key in ("parameters", "commits", "results"):
        if type(receipt[key]) is not dict:
            raise ReceiptInvalid(f"{key} must be a JSON object")
    for project in ("grafx", "pulse"):
        commit = receipt["commits"].get(project)
        if commit is not None and (type(commit) is not str or not commit):
            raise ReceiptInvalid(
                f"commits.{project} must be a non-empty string or null"
            )
    series = receipt["series"]
    if type(series) is not dict:
        raise ReceiptInvalid("series must be a JSON object")
    if series.get("mode") not in MODES:
        raise ReceiptInvalid(f"series.mode {series.get('mode')!r} not in {MODES}")
    if series.get("thermal") not in THERMALS:
        raise ReceiptInvalid(
            f"series.thermal {series.get('thermal')!r} not in {THERMALS}"
        )
    if series.get("kind") not in KINDS:
        raise ReceiptInvalid(f"series.kind {series.get('kind')!r} not in {KINDS}")
    config = receipt["config"]
    if type(config) is not dict:
        raise ReceiptInvalid("config must be a JSON object")
    if config.get("descriptor_revalidation") not in MODES:
        raise ReceiptInvalid(
            f"config.descriptor_revalidation {config.get('descriptor_revalidation')!r} not in {MODES}"
        )
    if config["descriptor_revalidation"] != series["mode"]:
        raise ReceiptInvalid("config.descriptor_revalidation must equal series.mode")
    environment_node = receipt["environment"]
    if type(environment_node) is not dict:
        raise ReceiptInvalid("environment must be a JSON object")
    grafx = environment_node.get("okto_grafx")
    if (
        type(grafx) is not dict
        or type(grafx.get("file")) is not str
        or not grafx["file"]
    ):
        raise ReceiptInvalid(
            "environment.okto_grafx.file is required (which Grafx ran?)"
        )
    if type(receipt["inputs"]) is not list:
        raise ReceiptInvalid("inputs must be a JSON array")
    for item in receipt["inputs"]:
        if type(item) is not dict:
            raise ReceiptInvalid("each input must be a JSON object")
        for key in ("role", "path", "declared_copy", "inventory_sha256"):
            if key not in item:
                raise ReceiptInvalid(f"input {item!r} misses {key!r}")
        if type(item["role"]) is not str or not item["role"]:
            raise ReceiptInvalid("input.role must be a non-empty string")
        if type(item["path"]) is not str or not item["path"]:
            raise ReceiptInvalid("input.path must be a non-empty string")
        if type(item["declared_copy"]) is not bool:
            raise ReceiptInvalid("input.declared_copy must be a boolean")
        if item["inventory_sha256"] is not None and not _is_hex64(
            item["inventory_sha256"]
        ):
            raise ReceiptInvalid(
                "input.inventory_sha256 must be null or a 64-hex sha256"
            )
    machine_node = receipt["machine"]
    if (
        type(machine_node) is not dict
        or type(machine_node.get("idle_asserted_by_operator")) is not bool
        or type(machine_node.get("sample")) is not dict
    ):
        raise ReceiptInvalid("machine needs idle_asserted_by_operator and sample")
    if type(receipt["notes"]) is not list or any(
        type(note) is not str for note in receipt["notes"]
    ):
        raise ReceiptInvalid("notes must be a JSON array of strings")
    if "identity_sha256" in receipt:
        if not _is_hex64(receipt["identity_sha256"]) or receipt[
            "identity_sha256"
        ] != identity_digest(receipt):
            raise ReceiptInvalid(
                "identity_sha256 does not match the stable receipt identity"
            )
    if "receipt_sha256" in receipt:
        if not _is_hex64(receipt["receipt_sha256"]) or receipt[
            "receipt_sha256"
        ] != receipt_digest(receipt):
            raise ReceiptInvalid("receipt_sha256 does not match the receipt occurrence")


def canonical_json(value: Any) -> str:
    _require_pure_json(value, "document")
    return (
        json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False)
        + "\n"
    )


def _compact(value: Any) -> str:
    _require_pure_json(value, "document")
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def identity_digest(receipt: Mapping[str, Any]) -> str:
    """Digest of the stable identity: same instrument, seed, parameters, series, config, inputs."""
    return sha256_text(
        _compact({key: receipt[key] for key in _IDENTITY_KEYS if key in receipt})
    )


def receipt_digest(receipt: Mapping[str, Any]) -> str:
    """Digest of the whole occurrence except the timestamp and the digests themselves."""
    return sha256_text(
        _compact({k: v for k, v in receipt.items() if k not in _VOLATILE_KEYS})
    )


def write_receipt(path: Path | str, receipt: Mapping[str, Any]) -> Path:
    """Validate and atomically publish canonical JSON plus a fail-closed digest sidecar."""
    validate_receipt(receipt)
    stamped = dict(receipt)
    stamped["identity_sha256"] = identity_digest(stamped)
    stamped["receipt_sha256"] = receipt_digest(stamped)
    target = guard_not_data_home(path)
    sidecar = Path(str(target) + ".sha256")
    target.parent.mkdir(parents=True, exist_ok=True)
    text = canonical_json(stamped)
    if target.exists() or sidecar.exists():
        raise FileExistsError(f"refusing to overwrite receipt or sidecar at {target}")
    token = f"{os.getpid()}-{uuid.uuid4().hex}"
    temporary = target.parent / f".{target.name}.{token}.tmp"
    temporary_sidecar = target.parent / f".{sidecar.name}.{token}.tmp"
    try:
        temporary.write_bytes(text.encode("utf-8"))
        temporary_sidecar.write_bytes((sha256_text(text) + "\n").encode("ascii"))
        # Publish the sidecar first: a crash can leave an orphan sidecar (fail-closed), never a
        # receipt that appears complete without the digest needed to authenticate its bytes.
        os.replace(temporary_sidecar, sidecar)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
        temporary_sidecar.unlink(missing_ok=True)
    return target


def read_json(path: Path | str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))
