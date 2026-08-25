"""Re-measure the C13 full profile from CLEAN committed trees and capture real provenance.

The first freeze recorded source_commits that did not identify the code that ran: the C13
worker was patched to ef_search=320 in an UNCOMMITTED tree, so c7a493 (which carries 64)
was attributed a run it never produced, and the engine came from 5e2fa294, which is not an
ancestor of the C13 branch at all.

This capture fixes that by construction:

* both worktrees must be CLEAN before and after the run -- a dirty tree aborts, so no
  measurement can ever again be attributed to a commit that does not contain it;
* identity is recorded as commit + git BLOB for each file that actually executes;
* which engine loaded is PROVEN by a probe subprocess in the same environment that reports
  okto_grafx.__file__ back, rather than inferred from the PYTHONPATH we intended;
* the exit code is captured from the process rather than assumed to be 0;
* the command and the BLAS environment are recorded verbatim so the run is reproducible.

5e2fa294 is recorded as a SEPARATE INTEGRATION DEPENDENCY, never as an ancestor.

Run: python capture_c13_evidence.py
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

C13 = Path(r"D:\Projetos\Techridy\okto_grafx-c13")
HNSW = Path(r"D:\Projetos\Techridy\okto_grafx-hnsw-recall")
OUT = Path(
    r"C:\Users\jpamb\AppData\Local\Temp\claude"
    r"\D--Projetos-Techridy-okto-grafx\9699f9ef-9534-43db-9888-7031689e86f5"
    r"\scratchpad\coord\evidence2"
)
OUT.mkdir(parents=True, exist_ok=True)

WORKER_REL = "bench/harness/recall_worker.py"
ENGINE_REL = "src/okto_grafx/domain/vector/hnsw.py"
BLAS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)


def git(tree: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(tree), capture_output=True, text=True, check=True
    ).stdout.strip()


def require_clean(tree: Path, when: str) -> None:
    porcelain = git(tree, "status", "--porcelain")
    if porcelain:
        raise SystemExit(
            f"ABORT: {tree.name} is not clean {when}:\n{porcelain}\n"
            "A measurement taken in a dirty tree cannot be attributed to any commit."
        )


def blob_identity(tree: Path, relative: str) -> dict[str, str]:
    """Commit + blob + a hash of the BLOB CONTENT, not of the working file.

    On Windows the working file carries CRLF while the object store keeps LF, so a hash of
    the bytes on disk is not reproducible on another platform's checkout. The blob content
    is the identity that travels.
    """
    commit = git(tree, "rev-parse", "HEAD")
    blob = git(tree, "rev-parse", f"HEAD:{relative}")
    content = subprocess.run(
        ["git", "show", f"HEAD:{relative}"],
        cwd=str(tree),
        capture_output=True,
        check=True,
    ).stdout
    return {
        "commit": commit,
        "blob": blob,
        "path": relative,
        "blob_sha256": hashlib.sha256(content).hexdigest(),
    }


print("== preflight: both trees must be clean ==")
require_clean(C13, "before the run")
require_clean(HNSW, "before the run")

worker = blob_identity(C13, WORKER_REL)
engine = blob_identity(HNSW, ENGINE_REL)
print("worker:", worker["commit"][:12], worker["blob"][:12])
print("engine:", engine["commit"][:12], engine["blob"][:12])

# The worker must be the ef320 one, and the check reads the blob rather than the disk.
worker_source = subprocess.run(
    ["git", "show", f"HEAD:{WORKER_REL}"], cwd=str(C13), capture_output=True, check=True
).stdout.decode("utf-8")
if '"ef_search": 320,' not in worker_source:
    raise SystemExit("ABORT: the committed worker does not carry ef_search 320")

environment = {str(k): str(v) for k, v in os.environ.items()}
for name in BLAS:
    environment[name] = "1"
environment["PYTHONPATH"] = str(HNSW / "src")

# PROVE which engine loads -- do not trust PYTHONPATH to have won.
probe = subprocess.run(
    [
        sys.executable,
        "-c",
        (
            "import json,sys,hashlib;import okto_grafx;"
            "from okto_grafx.domain.vector import hnsw as h;"
            "import numpy;"
            "d=open(h.__file__,'rb').read();"
            "print(json.dumps({'okto_grafx':okto_grafx.__file__,"
            "'hnsw':h.__file__,"
            "'hnsw_disk_sha256':hashlib.sha256(d).hexdigest(),"
            "'hnsw_lf_sha256':hashlib.sha256(d.replace(b'\\r\\n',b'\\n')).hexdigest(),"
            "'numpy':numpy.__version__,'python':sys.version}))"
        ),
    ],
    cwd=str(C13),
    env=environment,
    capture_output=True,
    text=True,
    check=True,
)
loaded = json.loads(probe.stdout.strip().splitlines()[-1])
print("loaded engine:", loaded["hnsw"])

if not loaded["hnsw"].startswith(str(HNSW)):
    raise SystemExit(
        f"ABORT: the engine that loaded is {loaded['hnsw']}, not the one under {HNSW}"
    )
# The LF-normalized hash of the loaded file must equal the blob content hash: that is what
# proves the file on disk IS the committed object, CRLF checkout notwithstanding.
if loaded["hnsw_lf_sha256"] != engine["blob_sha256"]:
    raise SystemExit(
        "ABORT: the loaded engine does not match the committed blob "
        f"({loaded['hnsw_lf_sha256']} vs {engine['blob_sha256']})"
    )
print("engine identity PROVEN against the committed blob")

if "--preflight-only" in sys.argv:
    print("PREFLIGHT OK -- clean trees, proven engine, committed ef320 worker")
    raise SystemExit(0)

raw_path = OUT / "recall-full-ef320.verdict.json"
if raw_path.exists():
    raw_path.unlink()

command = [
    sys.executable,
    "-m",
    "bench.harness.recall_worker",
    "--profile",
    "full",
    "--gt",
    "auto",
    "--out",
    str(raw_path),
]
print("== running the full profile; this takes roughly an hour ==")
print(" ".join(command), flush=True)

started = time.monotonic()
completed = subprocess.run(
    command, cwd=str(C13), env=environment, capture_output=True, text=True, check=False
)
duration = time.monotonic() - started
print("exit code:", completed.returncode, "duration:", duration, flush=True)

require_clean(C13, "after the run")
require_clean(HNSW, "after the run")

if not raw_path.exists():
    raise SystemExit(
        f"ABORT: the worker wrote no verdict (exit {completed.returncode})\n"
        f"stdout: {completed.stdout[-4000:]}\nstderr: {completed.stderr[-4000:]}"
    )
raw_bytes = raw_path.read_bytes()

metadata = {
    "what": "C13 full-profile vector recall measurement, re-taken from clean trees",
    "exit_code": completed.returncode,
    "duration_seconds": duration,
    "command": command,
    "cwd": "the C13 worktree",
    "blas_environment": {name: environment[name] for name in BLAS},
    "pythonpath_note": (
        "PYTHONPATH pointed at the engine worktree's src; which module actually loaded is "
        "recorded under loaded_modules and was verified against the committed blob before "
        "the run started"
    ),
    "worker": worker,
    "engine": engine,
    "engine_relationship": (
        "5e2fa294e144c4ebf4dfb3e3723af455ddc5d422 is NOT an ancestor of the C13 branch. It "
        "is a SEPARATE INTEGRATION DEPENDENCY: the calibrated HNSW search beam must land in "
        "the release lineage for this measurement to describe production. It is recorded "
        "here as a dependency, never as an ancestor."
    ),
    "loaded_modules": {
        "okto_grafx": Path(loaded["okto_grafx"]).name,
        "hnsw": Path(loaded["hnsw"]).name,
        "hnsw_lf_sha256": loaded["hnsw_lf_sha256"],
        "matches_committed_blob": loaded["hnsw_lf_sha256"] == engine["blob_sha256"],
    },
    "numpy": loaded["numpy"],
    "python": loaded["python"].split()[0],
    "python_full": loaded["python"],
    "platform": sys.platform,
    "raw_verdict_sha256": hashlib.sha256(raw_bytes).hexdigest(),
    "raw_verdict_bytes": len(raw_bytes),
    "worker_stdout_tail": completed.stdout[-2000:],
    "worker_stderr_tail": completed.stderr[-2000:],
}
meta_path = OUT / "recall-full-ef320.metadata.json"
meta_path.write_text(
    json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)

print("raw verdict sha256:", metadata["raw_verdict_sha256"])
print("raw:", raw_path)
print("metadata:", meta_path)
try:
    verdict = json.loads(raw_bytes.decode("utf-8"))
    print("ok:", verdict.get("ok"), "gauge:", verdict.get("gauge"))
    print("observed:", json.dumps(verdict.get("observed"), sort_keys=True))
except Exception as failure:  # noqa: BLE001 -- reporting only
    print("could not summarize the verdict:", failure)
print("CAPTURE COMPLETE")
