"""Provenance boundary tests, deliberately not campaign qualification."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from bench.harness.pulse_qualification import canonical_path, inventory, verify_snapshot


def test_snapshot_compares_actual_bytes_not_declared_revision(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "example.py").write_bytes(b"original\n")
    tracked = {"src/example.py": hashlib.sha256(b"original\n").hexdigest()}
    verify_snapshot(tmp_path, tracked)
    (source / "example.py").write_bytes(b"changed\n")
    with pytest.raises(ValueError, match="snapshot differs"):
        verify_snapshot(tmp_path, tracked)


def test_snapshot_refuses_inventory_escape(tmp_path):
    with pytest.raises(ValueError):
        verify_snapshot(tmp_path, {"../escaped": "not-a-hash"})


def test_relative_operational_paths_are_rejected():
    with pytest.raises(ValueError, match="absolute canonical"):
        canonical_path(Path("relative"))


def test_inventory_uses_git_blobs_with_crlf_checkout_and_export_ignored_files(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()

    def command(*args):
        return subprocess.check_output(["git", "-C", str(checkout), *args])

    command("init", "--quiet")
    attributes = b"*.txt text eol=crlf\nsecret.txt export-ignore\n"
    (checkout / ".gitattributes").write_bytes(attributes)
    (checkout / "example.txt").write_bytes(b"one\r\ntwo\r\n")
    (checkout / "secret.txt").write_bytes(b"included\r\n")
    command("add", ".")
    command("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "--quiet", "-m", "CRLF fixture")
    revision = command("rev-parse", "HEAD").decode().strip()
    assert command("status", "--porcelain") == b""
    assert (checkout / "example.txt").read_bytes() == b"one\r\ntwo\r\n"
    tracked = inventory(checkout, revision)
    assert set(tracked) == {".gitattributes", "example.txt", "secret.txt"}
    for name in tracked:
        (snapshot / name).write_bytes(command("show", revision + ":" + name))
    assert (snapshot / "example.txt").read_bytes() == b"one\ntwo\n"
    verify_snapshot(snapshot, tracked)
    (snapshot / "secret.txt").write_bytes(b"tampered\n")
    with pytest.raises(ValueError, match="snapshot differs"):
        verify_snapshot(snapshot, tracked)
    (checkout / "example.txt").write_bytes(b"tampered\r\n")
    with pytest.raises(ValueError, match="dirty source"):
        inventory(checkout, revision)


def test_inventory_checks_real_git_revision_and_dirty_files(tmp_path):
    def command(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args])

    command("init", "--quiet")
    (tmp_path / "example.py").write_bytes(b"original\n")
    command("add", "example.py")
    command("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "--quiet", "-m", "fixture")
    revision = command("rev-parse", "HEAD").decode().strip()
    assert inventory(tmp_path, revision) == {
        "example.py": hashlib.sha256(b"original\n").hexdigest(),
    }
    with pytest.raises(ValueError, match="revision mismatch"):
        inventory(tmp_path, "0" * 40)
    (tmp_path / "untracked").write_text("dirty", encoding="utf-8")
    with pytest.raises(ValueError, match="dirty source"):
        inventory(tmp_path, revision)


def test_worker_failure_preserves_receipt_and_logs_without_claiming_qualification(tmp_path, monkeypatch):
    from bench.harness import pulse_qualification as bridge

    # Fault injection tests the bridge's failure boundary only. No timing sample
    # or campaign result is synthesized, and no qualification is asserted.
    root = Path(__file__).resolve().parents[2]
    app = tmp_path / "app"
    base = app / "Okto" / "Grafx"
    base.mkdir(parents=True)
    output = base / "runs" / "failed"
    (base / "rf01-qualification.json").write_text(json.dumps({
        "schema_version": "grafx-pulse-qualification/v1", "candidate_root": str(root),
        "baseline_root": str(root), "python": str(Path(sys.executable).resolve()),
        "candidate_revision": "1" * 40, "output": str(output),
    }), encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(app))
    monkeypatch.setattr(bridge, "inventory", lambda *args: {"fixture": "digest"})
    monkeypatch.setattr(bridge, "verify_snapshot", lambda *args: None)

    def failed(command, log, timeout):
        assert 0 < timeout <= 90
        log.write("injected child failure; no calibration executed")
        return 9

    monkeypatch.setattr("bench.harness.campaign.execute_worker", failed)
    with pytest.raises(RuntimeError, match="phase 0 exited 9"):
        bridge.run(root)
    receipt = json.loads((output / "runner-receipt.json").read_text(encoding="utf-8"))
    assert receipt["decision"] == "failed"
    assert receipt["artifact_sha256"]["phase-0.log"] == hashlib.sha256(
        (output / "phase-0.log").read_bytes()
    ).hexdigest()
    assert not (output / "campaign").exists()
