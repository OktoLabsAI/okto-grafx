"""F1/CE-2 real-process acceptance instrument -- v8 (audited protocol).

Scenario-driven evidence for the CE-2 participant reader pin, with real OS processes and a
COMMANDED-CHECKPOINT protocol: the writer is paused except when the parent commands exactly
one checkpoint, so every observation has a provable causal position and nothing is inferred
from polling.

  long-reader    >=4 commanded checkpoints and >=4 removals of segments with last_lsn < S,
                 while no segment with last_lsn >= S is ever removed; oracle proved by the
                 reader's published progress; heartbeat; verify live + reopened
  close-advance  pause+ACK -> rollback -> pin > S within 10 s -> ONE commanded checkpoint ->
                 a previously protected segment is released -> only then close/unregister
  kill-hold      pause+ACK -> capture seq -> wait for a LARGER seq while paused -> ONE
                 commanded checkpoint (provably the writer's first observation of the new
                 seq; its before_at is the conservative anchor) -> kill+wait within 2 s ->
                 retention checkpoints that FINISH before anchor+stall -> explicit observer
                 probe at/after the limit -> a release checkpoint STARTED after the limit

Rules that hold everywhere:
  * protected means ``last_lsn >= S`` -- the segment containing S is what the reader needs
  * every cross-process bound is a difference of ABSOLUTE ``time.monotonic()`` stamps
  * every ACK is generational/tokenised; a stale probe file can never be mistaken for a new one
  * a valid release names a segment frozen in ``live_facts.protected_seen`` and its
    checkpoint has ``before_at >= limit``; a segment born after the reader ended proves nothing
  * no commanded checkpoint may straddle a limit (before_at/after_at are both recorded)
  * the writer is paused for every verify, in every scenario
  * children pin their import origin (A94); the parent imports nothing from okto_grafx

Scenario D of the design (4w+3r) remains tools/measure_concurrency.py run as-is.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import platform
import shutil
import subprocess
import sys
import tempfile
import time

POLL_SECONDS = 0.25
CHILD_TIMEOUT = 600
PROBE_DIR = "_probe"
SEGMENT_BYTES = 65536
NO_AUTO_CHECKPOINT = 1_000_000_000

READER_CHILD = r"""
import json, pathlib, sys, time
SRC = sys.argv[1]
sys.path.insert(0, SRC)
import okto_grafx
PACKAGE_PATH = pathlib.Path(okto_grafx.__file__).resolve()
SOURCE_PATH = pathlib.Path(SRC).resolve()
assert PACKAGE_PATH.is_relative_to(SOURCE_PATH), (
    "A94: child resolved okto_grafx outside the pinned source tree: " + str(PACKAGE_PATH)
)
from okto_grafx import connect

root, stall, mode = sys.argv[2], float(sys.argv[3]), sys.argv[4]
segment_bytes, no_auto = int(sys.argv[5]), int(sys.argv[6])
probe = pathlib.Path(root) / "_probe"
probe.mkdir(exist_ok=True)

def emit(name, payload):
    tmp = probe / (name + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    tmp.replace(probe / name)

db = connect(
    root,
    reader_stall_threshold_seconds=stall,
    wal_segment_bytes=segment_bytes,
    checkpoint_interval_records=no_auto,
)
reader = db.begin("read")
first = sorted(reader.execute("MATCH (i:Item) RETURN i.id, i.owner").rows)
snapshot = getattr(getattr(reader, "snapshot", None), "read_lsn", None)
emit("reader-ready.json", {"rows": len(first), "snapshot": snapshot,
                           "at": time.monotonic()})

report = {"scans": [len(first)], "ticks": 0, "mode": mode, "oracle_ok": True,
          "snapshot": snapshot}
interval = stall / 3.0
next_tick = time.monotonic() + interval
end_txn_flag = probe / "reader-end-txn.flag"
stop_flag = probe / "reader-stop.flag"
txn_open = True
hard_cap = time.monotonic() + 900
while time.monotonic() < hard_cap:
    if stop_flag.exists():
        break
    if txn_open and mode == "close" and end_txn_flag.exists():
        reader.rollback()
        txn_open = False
        emit("reader-txn-ended.json", {"at": time.monotonic()})
    time.sleep(0.1)
    if time.monotonic() >= next_tick:
        if txn_open:
            again = sorted(reader.execute("MATCH (i:Item) RETURN i.id, i.owner").rows)
            report["scans"].append(len(again))
            ok = again == first
            if not ok:
                report["oracle_ok"] = False
                report["oracle_failed"] = {"first": len(first), "now": len(again)}
            # Progress after EVERY scan: a killed reader leaves no final report, and the
            # oracle must be proved by what it did, never by absence.
            emit("reader-progress.json", {"scans": len(report["scans"]),
                                          "oracle_ok": ok, "at": time.monotonic()})
            if not ok:
                break
        trivial = db.begin("read")
        rollback = getattr(trivial, "rollback", None)
        if callable(rollback):
            rollback()
        report["ticks"] += 1
        next_tick += interval
if mode == "hold":
    emit("reader-holding.json", {"at": time.monotonic()})
    time.sleep(3600)  # the parent kills this process; never close, never rollback
if txn_open:
    reader.rollback()
emit("reader-close-start.json", {"at": time.monotonic()})
db.close()
report["closed_at"] = time.monotonic()
emit("reader-final.json", report)
"""

WRITER_CHILD = r"""
import json, pathlib, sys, time
SRC = sys.argv[1]
sys.path.insert(0, SRC)
import okto_grafx
PACKAGE_PATH = pathlib.Path(okto_grafx.__file__).resolve()
SOURCE_PATH = pathlib.Path(SRC).resolve()
assert PACKAGE_PATH.is_relative_to(SOURCE_PATH), (
    "A94: child resolved okto_grafx outside the pinned source tree: " + str(PACKAGE_PATH)
)
from okto_grafx import connect

root = sys.argv[2]
batch, stall = int(sys.argv[3]), float(sys.argv[4])
row_cap, preload_segments = int(sys.argv[5]), int(sys.argv[6])
segment_bytes, no_auto = int(sys.argv[7]), int(sys.argv[8])
probe = pathlib.Path(root) / "_probe"
probe.mkdir(exist_ok=True)

def emit(name, payload):
    tmp = probe / (name + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    tmp.replace(probe / name)

db = connect(
    root,
    reader_stall_threshold_seconds=stall,
    wal_segment_bytes=segment_bytes,
    checkpoint_interval_records=no_auto,
)

def inventory():
    rows = []
    for segment in db.wal.segments():
        rows.append({
            "name": str(getattr(segment, "name", getattr(segment, "file", ""))),
            "first": int(getattr(segment, "first_lsn", -1)),
            "last": int(getattr(segment, "last_lsn", -1)),
        })
    return rows

written = 0
base = 1_000_000

def write_batch():
    global written
    with db.begin("write") as txn:
        for offset in range(batch):
            txn.execute(
                "CREATE (:Item {id: $i, owner: 0, body: $b})",
                {"i": base + written + offset + 1, "b": "w" * 400},
            )
    written += batch

# --- PHASE A: preload backlog, NO checkpoint, then wait for commands -----------------------
preload_cap = time.monotonic() + 240
while time.monotonic() < preload_cap:
    if len(inventory()) >= preload_segments or written >= row_cap:
        break
    write_batch()
emit("writer-preloaded.json", {"written": written, "inventory": inventory(),
                               "at": time.monotonic()})

# --- PHASE B: a commanded protocol. The writer is paused unless told otherwise -------------
# Commands are TOKENISED files; every ACK carries its token, so a stale file can never be
# mistaken for a fresh one.
stop_flag = probe / "writer-stop.flag"
hard_cap = time.monotonic() + 900
served_pause = set()
served_checkpoint = set()
served_write = set()
while time.monotonic() < hard_cap and not stop_flag.exists():
    acted = False
    for command in sorted(probe.glob("cmd-*.json")):
        try:
            payload = json.loads(command.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        token, kind = payload.get("token"), payload.get("kind")
        if token is None:
            continue
        if kind == "pause" and token not in served_pause:
            served_pause.add(token)
            emit(f"ack-pause-{token}.json",
                 {"token": token, "at": time.monotonic(), "inventory": inventory(),
                  "checkpoints": len(served_checkpoint)})
            acted = True
        elif kind == "write" and token not in served_write:
            served_write.add(token)
            for _ in range(int(payload.get("batches", 1))):
                if written < row_cap * 3:
                    write_batch()
            emit(f"ack-write-{token}.json",
                 {"token": token, "at": time.monotonic(), "written": written})
            acted = True
        elif kind == "checkpoint" and token not in served_checkpoint:
            served_checkpoint.add(token)
            before_at = time.monotonic()
            before = inventory()
            # A checkpoint consults the reader horizon through THIS participant's
            # coordinator: that is the observation whose timing the anchor depends on.
            db.checkpoint()
            after_at = time.monotonic()
            after = inventory()
            emit(f"ack-checkpoint-{token}.json",
                 {"token": token, "before_at": before_at, "after_at": after_at,
                  "before": before, "after": after})
            acted = True
    if not acted:
        time.sleep(0.05)
db.close()
emit("writer-final.json", {"written": written, "checkpoints": len(served_checkpoint),
                           "at": time.monotonic()})
"""

OBSERVER_CHILD = r"""
import json, pathlib, sys, time
SRC = sys.argv[1]
sys.path.insert(0, SRC)
import okto_grafx
PACKAGE_PATH = pathlib.Path(okto_grafx.__file__).resolve()
SOURCE_PATH = pathlib.Path(SRC).resolve()
assert PACKAGE_PATH.is_relative_to(SOURCE_PATH), (
    "A94: child resolved okto_grafx outside the pinned source tree: " + str(PACKAGE_PATH)
)
from okto_grafx.adapters.coordination_local import decode_reader_record

root, reader_file = sys.argv[2], sys.argv[3]
probe = pathlib.Path(root) / "_probe"
probe.mkdir(exist_ok=True)
stop_flag = probe / "observer-stop.flag"
target = pathlib.Path(root) / reader_file

def sample():
    try:
        record = decode_reader_record(target.read_bytes(), file=reader_file)
        return {"pinned": int(record.snapshot_lsn),
                "seq": int(record.heartbeat_seq), "present": True}
    except FileNotFoundError:
        return {"present": False}
    except Exception as failure:
        return {"present": True, "decode_error": type(failure).__name__}

def emit(name, payload):
    tmp = probe / (name + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    tmp.replace(probe / name)

timeline = []
started = time.monotonic()
hard_cap = started + 900
last = None
served_probe = set()
emit("observer-ready.json", {"at": time.monotonic(), "sample": sample()})
while time.monotonic() < hard_cap and not stop_flag.exists():
    entry = sample()
    now = time.monotonic()
    if entry != last:
        timeline.append({"t": round(now - started, 2), "at": now, **entry})
        last = entry
        emit("observer-final.json", {"timeline": timeline})
    for command in sorted(probe.glob("cmd-probe-*.json")):
        try:
            payload = json.loads(command.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        token = payload.get("token")
        if token is None or token in served_probe:
            continue
        served_probe.add(token)
        fresh = sample()
        at = time.monotonic()
        emit(f"ack-probe-{token}.json", {"token": token, "at": at, "sample": fresh})
        timeline.append({"t": round(at - started, 2), "at": at, "probe": token, **fresh})
        emit("observer-final.json", {"timeline": timeline})
    time.sleep(0.25)
emit("observer-final.json", {"timeline": timeline})
"""

VERIFY_CHILD = r"""
import json, pathlib, sys
SRC = sys.argv[1]
sys.path.insert(0, SRC)
import okto_grafx
PACKAGE_PATH = pathlib.Path(okto_grafx.__file__).resolve()
SOURCE_PATH = pathlib.Path(SRC).resolve()
assert PACKAGE_PATH.is_relative_to(SOURCE_PATH), (
    "A94: child resolved okto_grafx outside the pinned source tree: " + str(PACKAGE_PATH)
)
from okto_grafx import connect

db = connect(sys.argv[2], wal_segment_bytes=int(sys.argv[3]),
             checkpoint_interval_records=int(sys.argv[4]))
findings = len(db.verify("all").findings)
db.close()
print(json.dumps({"findings": findings}), flush=True)
"""


class SetupFailure(Exception):
    """A setup step failed; the scenario still reports JSON (never a killed report)."""


class Conductor:
    """Tokenised command channel to the paused writer and the observer."""

    def __init__(self, root: pathlib.Path, timeline: "Timeline") -> None:
        self.root = root
        self.timeline = timeline
        self.next_token = 1

    def _issue(self, kind: str, prefix: str, **extra: object) -> int:
        token = self.next_token
        self.next_token += 1
        probe = self.root / PROBE_DIR
        probe.mkdir(exist_ok=True)
        body = {"token": token, "kind": kind, **extra}
        tmp = probe / f"{prefix}-{token}.json.tmp"
        tmp.write_text(json.dumps(body, sort_keys=True), encoding="utf-8")
        tmp.replace(probe / f"{prefix}-{token}.json")
        return token

    def _await(self, process: subprocess.Popen, name: str, seconds: float) -> dict:
        return wait_probe(self.root, process, name, seconds)

    def pause(self, writer: subprocess.Popen) -> dict:
        token = self._issue("pause", "cmd-pause")
        ack = self._await(writer, f"ack-pause-{token}.json", 120)
        self.timeline.note("writer-paused", token=token, at=ack.get("at"))
        return ack

    def write(self, writer: subprocess.Popen, batches: int) -> dict:
        token = self._issue("write", "cmd-write", batches=batches)
        return self._await(writer, f"ack-write-{token}.json", 240)

    def checkpoint(self, writer: subprocess.Popen, why: str) -> dict:
        token = self._issue("checkpoint", "cmd-checkpoint")
        ack = self._await(writer, f"ack-checkpoint-{token}.json", 240)
        ack["why"] = why
        self.timeline.note(
            "checkpoint",
            token=token,
            why=why,
            before_at=ack.get("before_at"),
            after_at=ack.get("after_at"),
        )
        return ack

    def probe(self, observer: subprocess.Popen) -> dict:
        token = self._issue("probe", "cmd-probe")
        ack = self._await(observer, f"ack-probe-{token}.json", 120)
        self.timeline.note(
            "observer-probe", token=token, at=ack.get("at"), sample=ack.get("sample")
        )
        return ack


def reader_records(root: pathlib.Path) -> tuple[str, ...]:
    readers = root / "control" / "readers"
    if not readers.is_dir():
        return ()
    return tuple(sorted(p.name for p in readers.glob("*.reader")))


def read_probe(root: pathlib.Path, name: str) -> dict | None:
    target = root / PROBE_DIR / name
    if not target.exists():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def flag(root: pathlib.Path, name: str) -> None:
    probe = root / PROBE_DIR
    probe.mkdir(exist_ok=True)
    (probe / name).write_text("1", encoding="utf-8")


class Timeline:
    def __init__(self) -> None:
        self.started = time.monotonic()
        self.events: list[dict] = []

    def now(self) -> float:
        return round(time.monotonic() - self.started, 2)

    def note(self, event: str, **detail: object) -> None:
        self.events.append({"t": self.now(), "event": event, **detail})


def spawn(template: str, args: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", template, *args],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


def run_child(template: str, args: list[str]) -> dict:
    done = subprocess.run(
        [sys.executable, "-c", template, *args],
        capture_output=True,
        text=True,
        timeout=CHILD_TIMEOUT,
    )
    if done.returncode != 0 or not done.stdout.strip():
        return {
            "child_failed": True,
            "code": done.returncode,
            "stderr": (done.stderr or "")[-300:],
        }
    try:
        return json.loads(done.stdout.strip().splitlines()[-1])
    except json.JSONDecodeError:
        return {
            "child_failed": True,
            "code": done.returncode,
            "stderr": "garbage stdout: " + done.stdout[-200:],
        }


def bootstrap(src: str, root: str) -> None:
    script = (
        "import sys; sys.path.insert(0, sys.argv[1]);\n"
        "from okto_grafx import connect\n"
        "db = connect(sys.argv[2], wal_segment_bytes=int(sys.argv[3]),\n"
        "             checkpoint_interval_records=int(sys.argv[4]))\n"
        "with db.begin('write') as txn:\n"
        "    txn.execute('CREATE NODE TABLE Item(id INT64, owner INT64, body STRING, "
        "PRIMARY KEY(id))')\n"
        "with db.begin('write') as txn:\n"
        "    for key in range(1, 21):\n"
        "        txn.execute('CREATE (:Item {id: $i, owner: -1, body: \\'seed\\'})', "
        "{'i': key})\n"
        "db.close()\n"
    )
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            src,
            root,
            str(SEGMENT_BYTES),
            str(NO_AUTO_CHECKPOINT),
        ],
        capture_output=True,
        text=True,
        timeout=CHILD_TIMEOUT,
    )
    if done.returncode != 0:
        raise SetupFailure(f"bootstrap failed: {done.stderr[-400:]}")


def wait_probe(
    root: pathlib.Path, process: subprocess.Popen, name: str, seconds: float
) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = read_probe(root, name)
        if found is not None:
            return found
        if process.poll() is not None:
            _, err = process.communicate(timeout=10)
            raise SetupFailure(
                f"child died before {name} (exit {process.returncode}): "
                f"{(err or '')[-400:]}"
            )
        time.sleep(POLL_SECONDS)
    raise SetupFailure(f"timed out waiting for {name}")


def collect(
    processes: list[tuple[str, subprocess.Popen]], *, grace: float = 0.0
) -> list[dict]:
    if grace > 0.0:
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if all(
                p.poll() is not None or label == "reader-killed"
                for label, p in processes
            ):
                break
            time.sleep(POLL_SECONDS)
    failures: list[dict] = []
    for label, process in processes:
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=10)
        else:
            process.communicate(timeout=10)
        code = process.returncode
        if code not in (0, None) and label != "reader-killed":
            failures.append({"child": label, "code": code})
    return failures


def spans(rows: list[dict]) -> dict[str, tuple[int, int]]:
    return {row["name"]: (row["first"], row["last"]) for row in rows}


def analyse(
    marks: list[dict], snapshot: int, *, carry: list[dict] | None = None
) -> dict:
    """Fold commanded-checkpoint inventories into the F1 facts.

    ``carry`` is the previous phase's last inventory, so a removal at a phase boundary
    cannot hide. Protected means ``last_lsn >= S``: the segment containing S is exactly the
    one the reader still needs, so the textual "> S" is a floor, not a licence.
    """
    protected_seen: set[str] = set()
    pre_s_removed: set[str] = set()
    protected_removed: list[dict] = []
    previous = spans(carry) if carry else None
    for mark in marks:
        before, after = spans(mark["before"]), spans(mark["after"])
        pairs = [(previous, before)] if previous is not None else []
        pairs.append((before, after))
        for older, newer in pairs:
            for name, (_first, last) in older.items():
                if last >= snapshot:
                    protected_seen.add(name)
                if name in newer:
                    continue
                if last < snapshot:
                    pre_s_removed.add(name)
                else:
                    protected_removed.append(
                        {
                            "name": name,
                            "last": last,
                            "before_at": mark.get("before_at"),
                            "after_at": mark.get("after_at"),
                        }
                    )
        previous = after
    return {
        "checkpoints": len(marks),
        "protected_seen": sorted(protected_seen),
        "pre_s_removed": sorted(pre_s_removed),
        "protected_removed": protected_removed,
        "last_inventory": marks[-1]["after"] if marks else (carry or []),
    }


def observer_samples(root: pathlib.Path) -> list[dict]:
    return (read_probe(root, "observer-final.json") or {}).get("timeline", [])


def latest_seq(root: pathlib.Path) -> int | None:
    seqs = [
        s.get("seq") for s in observer_samples(root) if s.get("present") and "seq" in s
    ]
    return seqs[-1] if seqs else None


def run_scenario(name: str, opts: argparse.Namespace) -> dict:
    root = pathlib.Path(tempfile.mkdtemp(prefix=f"grafx-pin-{name}-"))
    timeline = Timeline()
    criteria: list[dict] = []
    processes: list[tuple[str, subprocess.Popen]] = []
    conductor = Conductor(root, timeline)
    interval = opts.stall / 3.0
    try:
        try:
            bootstrap(opts.src, str(root))
            (root / PROBE_DIR).mkdir(exist_ok=True)
            mode = {
                "long-reader": "plain",
                "close-advance": "close",
                "kill-hold": "hold",
            }[name]

            # --- PHASE A: backlog with NO checkpoint -------------------------------------
            writer = spawn(
                WRITER_CHILD,
                [
                    opts.src,
                    str(root),
                    str(opts.batch),
                    str(opts.stall),
                    str(opts.rows),
                    str(opts.preload_segments),
                    str(SEGMENT_BYTES),
                    str(NO_AUTO_CHECKPOINT),
                ],
            )
            processes.append(("writer", writer))
            preloaded = wait_probe(root, writer, "writer-preloaded.json", 260)
            backlog = preloaded.get("inventory", [])
            timeline.note("preload-done", segments=len(backlog))
            criteria.append(
                {
                    "name": "backlog_before_snapshot",
                    "pass": len(backlog) >= opts.preload_segments,
                    "observed": len(backlog),
                    "bound": f">={opts.preload_segments} segments, none "
                    "checkpointed yet",
                }
            )

            # --- the reader opens S on top of that backlog -------------------------------
            records_before = set(reader_records(root))
            reader = spawn(
                READER_CHILD,
                [
                    opts.src,
                    str(root),
                    str(opts.stall),
                    mode,
                    str(SEGMENT_BYTES),
                    str(NO_AUTO_CHECKPOINT),
                ],
            )
            processes.append(
                ("reader-killed" if name == "kill-hold" else "reader", reader)
            )
            ready = wait_probe(root, reader, "reader-ready.json", 90)
            snapshot = ready.get("snapshot")
            live_started_at = time.monotonic()
            timeline.note("reader-ready", snapshot=snapshot, at=ready.get("at"))
            criteria.append(
                {
                    "name": "snapshot_reported",
                    "pass": snapshot is not None,
                    "observed": snapshot,
                    "bound": "public txn.snapshot.read_lsn",
                }
            )
            new_records = set(reader_records(root)) - records_before
            reader_file = (
                f"control/readers/{new_records.pop()}"
                if len(new_records) == 1
                else None
            )
            criteria.append(
                {
                    "name": "reader_record_identified",
                    "pass": reader_file is not None,
                    "observed": reader_file,
                    "bound": "exactly one new .reader after begin",
                }
            )
            observer = None
            if reader_file is not None:
                observer = spawn(OBSERVER_CHILD, [opts.src, str(root), reader_file])
                processes.append(("observer", observer))
                initial = wait_probe(root, observer, "observer-ready.json", 60)
                sample = initial.get("sample") or {}
                criteria.append(
                    {
                        "name": "observer_ready_record_present",
                        "pass": bool(sample.get("present"))
                        and "decode_error" not in sample,
                        "observed": sample,
                        "bound": "present and decodable at observer start",
                    }
                )

            # --- PHASE B: commanded work and commanded checkpoints -----------------------
            live_marks: list[dict] = []
            for index in range(opts.min_checkpoints):
                conductor.write(writer, opts.write_batches)
                live_marks.append(conductor.checkpoint(writer, why=f"live-{index + 1}"))
            if name == "long-reader":
                # This scenario is explicitly a duration proof, not merely a wait-for-two-scans
                # smoke. Keep both participants alive for the requested interval even when the
                # oracle becomes ready immediately.
                live_until = live_started_at + opts.seconds
                while time.monotonic() < live_until:
                    remaining = live_until - time.monotonic()
                    if remaining <= 0:
                        break
                    time.sleep(min(POLL_SECONDS, remaining))
            else:
                deadline = time.monotonic() + opts.seconds
                while time.monotonic() < deadline:
                    progress = read_probe(root, "reader-progress.json") or {}
                    if progress.get("scans", 0) >= 2 and progress.get("oracle_ok"):
                        break
                    time.sleep(POLL_SECONDS)
            live_elapsed = time.monotonic() - live_started_at
            progress = read_probe(root, "reader-progress.json") or {}
            criteria.append(
                {
                    "name": "reader_proved_repeated_oracle_scans",
                    "pass": progress.get("scans", 0) >= 2
                    and bool(progress.get("oracle_ok"))
                    and (name != "long-reader" or live_elapsed >= opts.seconds),
                    "observed": {
                        **progress,
                        "live_seconds": round(live_elapsed, 3),
                    },
                    "bound": ">=2 identical scans published (never auto-PASS "
                    "through a kill); long-reader also remains live for the "
                    "requested duration",
                }
            )

            # --- boundary: pause+ACK, then freeze the analysed facts ---------------------
            paused = conductor.pause(writer)
            live_facts = (
                analyse(live_marks, snapshot, carry=backlog)
                if snapshot is not None
                else {}
            )
            frozen_protected = set(live_facts.get("protected_seen", []))
            timeline.note(
                "live-boundary-frozen",
                checkpoints=len(live_marks),
                protected=len(frozen_protected),
            )
            criteria.append(
                {
                    "name": "writer_paused_for_boundary_capture",
                    "pass": paused.get("at") is not None,
                    "observed": paused.get("at"),
                    "bound": "pause ACK precedes the analysed boundary",
                }
            )

            anchor_at: float | None = None
            limit: float | None = None
            release_after: float | None = None
            presence_through: float | None = None
            pin_advanced_at: float | None = None
            post_marks: list[dict] = []
            kill_at: float | None = None
            orphan_pruned = None

            if name == "kill-hold" and observer is not None:
                # The anchor: with the writer PAUSED, capture the current heartbeat, wait
                # for a LARGER one, then command exactly ONE checkpoint -- provably the
                # writer's first observation of that new seq. Its before_at is the
                # conservative bound of the real foreign observation.
                start_seq = latest_seq(root)
                seq_deadline = time.monotonic() + opts.stall * 2
                new_seq = start_seq
                while time.monotonic() < seq_deadline:
                    new_seq = latest_seq(root)
                    if (
                        start_seq is not None
                        and new_seq is not None
                        and new_seq > start_seq
                    ):
                        break
                    time.sleep(POLL_SECONDS)
                criteria.append(
                    {
                        "name": "fresh_heartbeat_before_anchor",
                        "pass": start_seq is not None
                        and new_seq is not None
                        and new_seq > start_seq,
                        "observed": {"start": start_seq, "new": new_seq},
                        "bound": "a strictly larger heartbeat seq while paused",
                    }
                )
                anchor_mark = conductor.checkpoint(writer, why="anchor")
                anchor_at = anchor_mark.get("before_at")
                anchor_after = anchor_mark.get("after_at")
                live_marks.append(anchor_mark)
                live_facts = (
                    analyse(live_marks, snapshot, carry=backlog)
                    if snapshot is not None
                    else {}
                )
                frozen_protected = set(live_facts.get("protected_seen", []))
                reader.kill()
                reader.wait(timeout=30)
                kill_at = time.monotonic()
                limit = (anchor_at or 0) + opts.stall
                release_after = (anchor_after or 0) + opts.stall
                timeline.note(
                    "reader-killed",
                    at=kill_at,
                    anchor_at=anchor_at,
                    gap=round(kill_at - (anchor_at or kill_at), 2),
                )
                criteria.append(
                    {
                        "name": "kill_follows_anchor_within_2s",
                        "pass": anchor_at is not None and (kill_at - anchor_at) <= 2.0,
                        "observed": round(kill_at - (anchor_at or kill_at), 2),
                        "bound": "<= 2 s between the anchor checkpoint and kill",
                    }
                )
                # Retention checkpoints must FINISH before the limit; then, still paused,
                # an explicit observer probe at/after the limit; then the release.
                last_retention_after = anchor_after or 0
                retention_floor = (limit or 0) - opts.retention_margin
                while True:
                    remaining = (limit or 0) - time.monotonic()
                    if remaining <= 0 or last_retention_after >= retention_floor:
                        break
                    mark = conductor.checkpoint(writer, why="retention")
                    post_marks.append(mark)
                    last_retention_after = mark.get("after_at", 0)
                    if mark.get("after_at", 0) >= (limit or 0):
                        break
                    remaining = (limit or 0) - time.monotonic()
                    delay = min(interval, max(0.0, remaining - opts.retention_margin))
                    if delay > 0.0:
                        time.sleep(delay)
                while time.monotonic() < (release_after or 0):
                    time.sleep(POLL_SECONDS)
                probe_ack = conductor.probe(observer)
                probe_sample = probe_ack.get("sample") or {}
                presence_through = probe_ack.get("at")
                criteria.append(
                    {
                        "name": "record_probed_at_or_after_limit",
                        "pass": probe_ack.get("at", 0) >= (release_after or 0)
                        and bool(probe_sample.get("present"))
                        and "decode_error" not in probe_sample,
                        "observed": {
                            "at": probe_ack.get("at"),
                            "retain_until": limit,
                            "release_after": release_after,
                            "sample": probe_sample,
                        },
                        "bound": "present and decodable at/after the latest "
                        "possible stall expiration",
                    }
                )
                release = conductor.checkpoint(writer, why="release")
                post_marks.append(release)
                orphan_pruned = (
                    reader_file is not None and not (root / reader_file).exists()
                )
            elif name == "close-advance" and observer is not None:
                flag(root, "reader-end-txn.flag")
                ended = wait_probe(root, reader, "reader-txn-ended.json", 90)
                anchor_at = ended.get("at")
                timeline.note("reader-txn-ended", at=anchor_at)
                advance_deadline = time.monotonic() + 10.0
                while time.monotonic() < advance_deadline and pin_advanced_at is None:
                    for entry in observer_samples(root):
                        if (
                            entry.get("present")
                            and snapshot is not None
                            and entry.get("pinned", -1) > snapshot
                            and entry.get("at", 0) >= (anchor_at or 0)
                        ):
                            pin_advanced_at = entry["at"]
                            break
                    if pin_advanced_at is None:
                        time.sleep(POLL_SECONDS)
                timeline.note("pin-advance-window-closed", at=pin_advanced_at)
                limit = pin_advanced_at
                if pin_advanced_at is not None:
                    post_marks.append(conductor.checkpoint(writer, why="release"))

            post_facts = (
                analyse(post_marks, snapshot, carry=live_facts.get("last_inventory"))
                if snapshot is not None
                else {}
            )

            # --- verify with the writer PAUSED, in every scenario ------------------------
            verify_live = run_child(
                VERIFY_CHILD,
                [opts.src, str(root), str(SEGMENT_BYTES), str(NO_AUTO_CHECKPOINT)],
            )
            criteria.append(
                {
                    "name": "verify_clean_with_participants_alive",
                    "pass": verify_live.get("findings") == 0,
                    "observed": verify_live,
                    "bound": "0 findings",
                }
            )

            flag(root, "reader-stop.flag")
            if name == "close-advance":
                closed = read_probe(root, "reader-close-start.json")
                if closed is None:
                    try:
                        closed = wait_probe(root, reader, "reader-close-start.json", 60)
                    except SetupFailure:
                        closed = None
            flag(root, "writer-stop.flag")
            time.sleep(1.0)
            flag(root, "observer-stop.flag")
            child_failures = collect(processes, grace=opts.grace)
            processes.clear()
            verify_reopen = run_child(
                VERIFY_CHILD,
                [opts.src, str(root), str(SEGMENT_BYTES), str(NO_AUTO_CHECKPOINT)],
            )
            criteria.append(
                {
                    "name": "verify_clean_after_reopen",
                    "pass": verify_reopen.get("findings") == 0,
                    "observed": verify_reopen,
                    "bound": "0 findings",
                }
            )
            reader_report = read_probe(root, "reader-final.json") or {}
            final_progress = read_probe(root, "reader-progress.json") or {}
            samples = observer_samples(root)
            pins = [e for e in samples if e.get("present")]

            # --- criteria every scenario owes --------------------------------------------
            oracle_ok = (
                bool(reader_report.get("oracle_ok"))
                if reader_report
                else bool(final_progress.get("oracle_ok"))
                and final_progress.get("scans", 0) >= 2
            )
            criteria.append(
                {
                    "name": "oracle_scans_identical_while_txn_lived",
                    "pass": oracle_ok,
                    "observed": reader_report.get("scans") or final_progress,
                    "bound": "identical scans, proved by published progress "
                    "when a kill leaves no final report",
                }
            )
            criteria.append(
                {
                    "name": "no_protected_loss_while_reader_lived",
                    "pass": not live_facts.get("protected_removed"),
                    "observed": live_facts.get("protected_removed"),
                    "bound": "no last_lsn >= S removal before the reader ended",
                }
            )
            close_start = (read_probe(root, "reader-close-start.json") or {}).get("at")
            window_end = (
                (presence_through or release_after or limit or 0)
                if name == "kill-hold"
                else close_start or (anchor_at or 0) + opts.post_seconds
            )
            during = [
                s for s in samples if s.get("at") is not None and s["at"] <= window_end
            ]
            criteria.append(
                {
                    "name": "record_present_and_decodable_at_all_samples",
                    "pass": bool(during)
                    and all(
                        s.get("present") and "decode_error" not in s for s in during
                    ),
                    "observed": [
                        s for s in during if not s.get("present") or "decode_error" in s
                    ][:4],
                    "bound": "present and decodable at every periodic sample until close "
                    "(close/long) or anchor+stall (kill)",
                }
            )

            if name == "long-reader":
                criteria.append(
                    {
                        "name": "commanded_checkpoints_after_snapshot",
                        "pass": live_facts.get("checkpoints", 0)
                        >= opts.min_checkpoints,
                        "observed": live_facts.get("checkpoints"),
                        "bound": f">={opts.min_checkpoints} commanded",
                    }
                )
                criteria.append(
                    {
                        "name": "pre_s_segments_recycled",
                        "pass": len(live_facts.get("pre_s_removed", [])) >= 4,
                        "observed": live_facts.get("pre_s_removed"),
                        "bound": ">=4 segments with last_lsn < S removed",
                    }
                )
                pin_ok = (
                    bool(pins)
                    and snapshot is not None
                    and all(
                        e.get("pinned", 0) <= snapshot for e in pins if "pinned" in e
                    )
                )
                criteria.append(
                    {
                        "name": "pinned_lsn_never_exceeds_snapshot_while_live",
                        "pass": pin_ok,
                        "observed": [e.get("pinned") for e in pins][:8],
                        "bound": f"<= snapshot {snapshot}",
                    }
                )
                seqs = [e.get("seq") for e in pins if "seq" in e]
                criteria.append(
                    {
                        "name": "heartbeat_monotone_increasing",
                        "pass": seqs == sorted(seqs)
                        and len(seqs) >= 2
                        and len(set(seqs)) >= 2,
                        "observed": seqs[:8],
                        "bound": "non-decreasing with >=2 distinct values",
                    }
                )
            elif name == "close-advance":
                within = (
                    pin_advanced_at is not None
                    and anchor_at is not None
                    and (pin_advanced_at - anchor_at) <= 10.0
                )
                criteria.append(
                    {
                        "name": "pin_advanced_past_snapshot_within_10s",
                        "pass": within,
                        "observed": None
                        if pin_advanced_at is None
                        else round(pin_advanced_at - (anchor_at or 0), 2),
                        "bound": f"> snapshot {snapshot} within 10 s of txn end",
                    }
                )
                released = [
                    r
                    for r in (post_facts.get("protected_removed") or [])
                    if r.get("before_at", 0) >= (limit or float("inf"))
                    and r["name"] in frozen_protected
                ]
                criteria.append(
                    {
                        "name": "protected_released_after_pin_advance",
                        "pass": bool(released),
                        "observed": released[:3],
                        "bound": "a segment from the frozen protected set removed "
                        "by a checkpoint STARTED after the pin advanced",
                    }
                )
                withdrawn = (
                    reader_file is not None and not (root / reader_file).exists()
                )
                criteria.append(
                    {
                        "name": "record_withdrawn_only_at_close",
                        "pass": withdrawn,
                        "observed": withdrawn,
                        "bound": "the reader's OWN file absent after close",
                    }
                )
            elif name == "kill-hold":
                straddling = [
                    m
                    for m in post_marks
                    if limit is not None
                    and m.get("before_at", 0) < limit <= m.get("after_at", 0)
                ]
                criteria.append(
                    {
                        "name": "no_checkpoint_straddles_the_limit",
                        "pass": not straddling,
                        "observed": [
                            {
                                "before_at": m.get("before_at"),
                                "after_at": m.get("after_at"),
                            }
                            for m in straddling
                        ][:3],
                        "bound": "every commanded checkpoint lies wholly before "
                        "or wholly after anchor+stall",
                    }
                )
                removed = post_facts.get("protected_removed") or []
                early = [
                    r
                    for r in removed
                    if limit is not None and r.get("after_at", 0) < limit
                ]
                retention_marks = [m for m in post_marks if m.get("why") == "retention"]
                last_retention_after = max(
                    (m.get("after_at", 0) for m in retention_marks), default=0
                )
                retention_gap = (
                    None
                    if limit is None or not retention_marks
                    else limit - last_retention_after
                )
                criteria.append(
                    {
                        "name": "protected_held_until_ttl",
                        "pass": not early
                        and retention_gap is not None
                        and 0 <= retention_gap <= opts.retention_margin,
                        "observed": {
                            "early_removals": early[:3],
                            "last_retention_after": last_retention_after,
                            "limit": limit,
                            "untested_gap_seconds": retention_gap,
                        },
                        "bound": f"no last_lsn >= S removal by a checkpoint that "
                        f"finished before anchor+{opts.stall:.0f}s, with the final "
                        f"retention checkpoint within {opts.retention_margin:.1f}s "
                        "of that boundary",
                    }
                )
                released = [
                    r
                    for r in removed
                    if release_after is not None
                    and r.get("before_at", 0) >= release_after
                    and r["name"] in frozen_protected
                ]
                criteria.append(
                    {
                        "name": "protected_released_after_ttl",
                        "pass": bool(released),
                        "observed": released[:3],
                        "bound": "a segment from the frozen protected set removed "
                        "by a checkpoint started after the latest "
                        "possible stall expiration",
                    }
                )
                criteria.append(
                    {
                        "name": "orphan_record_pruned",
                        "pass": bool(orphan_pruned),
                        "observed": orphan_pruned,
                        "bound": "the orphan .reader removed by a live participant",
                    }
                )
            if child_failures:
                timeline.note("child-failures", failures=child_failures)
                for criterion in criteria:
                    criterion["pass"] = False
                criteria.append(
                    {
                        "name": "children_exited_cleanly",
                        "pass": False,
                        "observed": child_failures,
                        "bound": "all zero exits",
                    }
                )
            return {
                "scenario": name,
                "criteria": criteria,
                "timeline": timeline.events,
                "reader": reader_report,
                "live_facts": live_facts,
                "post_facts": post_facts,
                "observer_samples": len(samples),
                "anchor_at": anchor_at,
                "limit": limit,
                "release_after": release_after,
                "kill_at": kill_at,
                "pass": all(c["pass"] for c in criteria),
            }
        except SetupFailure as failure:
            criteria.append(
                {
                    "name": "setup_completed",
                    "pass": False,
                    "observed": str(failure)[:400],
                    "bound": "bootstrap + child handshakes succeed",
                }
            )
            timeline.note("setup-failed", detail=str(failure)[:200])
            return {
                "scenario": name,
                "criteria": criteria,
                "timeline": timeline.events,
                "reader": {},
                "observer_samples": 0,
                "pass": False,
            }
    finally:
        collect(processes, grace=opts.grace)
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", required=True)
    parser.add_argument(
        "--scenario",
        default="all",
        choices=["long-reader", "close-advance", "kill-hold", "all"],
    )
    parser.add_argument("--json", dest="json_out", default=None)
    parser.add_argument(
        "--seconds",
        type=float,
        default=90.0,
        help="long-reader live duration; oracle wait bound in the other scenarios",
    )
    parser.add_argument("--post-seconds", type=float, default=20.0)
    parser.add_argument("--stall", type=float, default=15.0)
    parser.add_argument("--batch", type=int, default=25)
    parser.add_argument(
        "--write-batches", type=int, default=3, help="batches per commanded write step"
    )
    parser.add_argument("--rows", type=int, default=2000)
    parser.add_argument("--preload-segments", type=int, default=6)
    parser.add_argument("--min-checkpoints", type=int, default=4)
    parser.add_argument(
        "--retention-margin",
        type=float,
        default=3.0,
        help="stop retention checkpoints this long before the limit",
    )
    parser.add_argument("--grace", type=float, default=10.0)
    opts = parser.parse_args()
    # Children receive this path after spawning and enforce A94 against the imported package.
    # Normalize a documented relative `--src .\src` before it crosses that process boundary.
    opts.src = str(pathlib.Path(opts.src).resolve())
    positive = {
        "seconds": opts.seconds,
        "post_seconds": opts.post_seconds,
        "stall": opts.stall,
        "batch": opts.batch,
        "write_batches": opts.write_batches,
        "rows": opts.rows,
        "preload_segments": opts.preload_segments,
        "min_checkpoints": opts.min_checkpoints,
        "retention_margin": opts.retention_margin,
        "grace": opts.grace,
    }
    invalid = sorted(field for field, value in positive.items() if value <= 0)
    if invalid:
        parser.error(f"these options must be greater than zero: {', '.join(invalid)}")
    if opts.retention_margin >= opts.stall:
        parser.error("--retention-margin must be smaller than --stall")
    names = (
        ["long-reader", "close-advance", "kill-hold"]
        if opts.scenario == "all"
        else [opts.scenario]
    )
    script_path = pathlib.Path(__file__).resolve()
    repository = pathlib.Path(opts.src).parent
    commit_probe = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    status_probe = subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "status",
            "--porcelain",
            "--untracked-files=all",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    product_status_probe = subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            "src",
            "tests",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    report = {
        "tool": "measure_reader_pin",
        "version": 8,
        "provenance": {
            "command": [sys.executable, str(script_path), *sys.argv[1:]],
            "grafx_commit": commit_probe.stdout.strip()
            if commit_probe.returncode == 0
            else None,
            "git_status_porcelain": status_probe.stdout.splitlines()
            if status_probe.returncode == 0
            else None,
            "product_src_tests_clean": product_status_probe.returncode == 0
            and not product_status_probe.stdout,
            "python_executable": sys.executable,
            "python_version": platform.python_version(),
            "repository": str(repository),
            "script_sha256": hashlib.sha256(script_path.read_bytes()).hexdigest(),
            "source_root": opts.src,
            "system": platform.platform(),
        },
        "config": {
            "seconds": opts.seconds,
            "post_seconds": opts.post_seconds,
            "stall": opts.stall,
            "batch": opts.batch,
            "write_batches": opts.write_batches,
            "rows": opts.rows,
            "preload_segments": opts.preload_segments,
            "min_checkpoints": opts.min_checkpoints,
            "retention_margin": opts.retention_margin,
            "wal_segment_bytes": SEGMENT_BYTES,
            "checkpoint_interval_records": NO_AUTO_CHECKPOINT,
        },
        "scenarios": [run_scenario(name, opts) for name in names],
    }
    report["pass"] = all(s["pass"] for s in report["scenarios"])
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if opts.json_out:
        pathlib.Path(opts.json_out).write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
