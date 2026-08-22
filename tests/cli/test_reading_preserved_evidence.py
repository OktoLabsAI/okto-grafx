"""Reading what a discard preserved: the ledger and the quarantine (C12).

SPEC-M1 FR-9 and FR-10 are the reason this database is worth trusting: nothing is discarded
without a trace, and the trace is preserved before anything is destroyed. None of that is worth
anything if an operator cannot reach it, so these tests seed a real discard and then read it back
the way a person would.

The export tests carry LESSONS L5 directly. C2 shipped a rename that reported success, wrote the
right bytes and used a garbled name, and every content assertion passed because it read back
through the handle the operation returned. So the export here is confirmed from the namespace
instead: the directory is listed for the exact base name, and the file is re-opened by a path
built again from its parts.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from okto_grafx.cli.exits import FINDINGS, OK, REFUSED

from tests.cli.conftest import CliRunner, damage_log_tail


@pytest.fixture
def database_with_evidence(database_path: str, cli: CliRunner) -> str:
    """Return a database whose log was damaged and whose evidence has been preserved.

    The damage is seeded and then an open is forced, because it is recovery that quarantines the
    range and writes the ledger entry. Both are asserted here rather than assumed: a fixture that
    did not produce the state it claims makes every test below it vacuous (A72).
    """
    damage_log_tail(database_path)
    first = cli("status", database_path, "--json")
    document = first.document
    assert int(document["ledger"]["total"]) >= 1, "the fixture produced no ledger entry"
    assert int(document["quarantine"]["entries"]) >= 1, "the fixture quarantined nothing"
    return database_path


def _first_entry_id(cli: CliRunner, path: str) -> int:
    """Return the identifier of the first ledger entry, read through the tool itself."""
    document = cli("ledger", "list", path, "--json").document
    entries = document["entries"]
    assert isinstance(entries, list) and entries
    return int(entries[0]["entry_id"])


def _first_quarantine_name(cli: CliRunner, path: str) -> str:
    """Return the name of the first quarantine entry, read through the tool itself."""
    document = cli("quarantine", "list", path, "--json").document
    entries = document["entries"]
    assert isinstance(entries, list) and entries
    return str(entries[0]["name"])


def test_an_empty_ledger_reports_a_clean_answer(database_path: str, cli: CliRunner) -> None:
    run = cli("ledger", "list", database_path)
    assert run.code == OK
    assert "(no entries match)" in run.out
    assert "depth" in run.out


def test_a_ledger_holding_lost_work_is_not_clean(
    database_with_evidence: str, cli: CliRunner
) -> None:
    run = cli("ledger", "list", database_with_evidence)
    assert run.code == FINDINGS
    assert "forensic" in run.out
    assert "truncated_tail" in run.out


def test_the_ledger_listing_names_class_reason_and_range(
    database_with_evidence: str, cli: CliRunner
) -> None:
    document = cli("ledger", "list", database_with_evidence, "--json").document
    entry = document["entries"][0]
    assert entry["origin_class"] == "forensic"
    assert entry["reason"] == "truncated_tail"
    assert entry["reapplicable"] is False
    assert int(entry["lsn_start"]) >= 1
    assert int(entry["payload_bytes"]) > 0
    assert len(str(entry["digest"])) == 64


def test_the_ledger_listing_can_be_filtered_by_class(
    database_with_evidence: str, cli: CliRunner
) -> None:
    forensic = cli("ledger", "list", database_with_evidence, "--origin-class", "forensic")
    assert forensic.code == FINDINGS
    reapplicable = cli(
        "ledger", "list", database_with_evidence, "--origin-class", "reapplicable", "--json"
    )
    assert reapplicable.code == OK
    assert reapplicable.document["entries"] == []
    # The unfiltered depth is still reported, so a filter cannot make a database look clean.
    assert int(reapplicable.document["total"]) >= 1


def test_inspecting_a_ledger_entry_shows_where_the_bytes_came_from(
    database_with_evidence: str, cli: CliRunner
) -> None:
    entry_id = _first_entry_id(cli, database_with_evidence)
    run = cli("ledger", "inspect", database_with_evidence, str(entry_id))
    assert run.code == OK
    document = cli("ledger", "inspect", database_with_evidence, str(entry_id), "--json").document
    provenance = document["provenance"]
    assert provenance["origin"].endswith(".wal")
    assert int(provenance["offset"]) > 0
    assert int(provenance["length"]) > 0
    assert provenance["failure"], "a forensic entry records why the bytes did not decode"
    assert provenance["quarantine"], "a forensic entry names the quarantine that holds the bytes"
    assert "forensic" in run.out


def test_inspecting_an_entry_the_ledger_does_not_hold_is_refused(
    database_with_evidence: str, cli: CliRunner
) -> None:
    run = cli("ledger", "inspect", database_with_evidence, "99999")
    assert run.code == REFUSED
    assert "ledger_error" in run.err
    assert "99999" in run.err


def test_exporting_preserved_bytes_writes_them_and_confirms_the_file(
    database_with_evidence: str, tmp_path: Path, cli: CliRunner
) -> None:
    entry_id = _first_entry_id(cli, database_with_evidence)
    output = tmp_path / "exported.bin"
    run = cli("ledger", "export", database_with_evidence, str(entry_id), "--output", str(output))
    assert run.code == OK
    document = cli(
        "ledger",
        "export",
        database_with_evidence,
        str(entry_id),
        "--output",
        str(tmp_path / "again.bin"),
        "--json",
    ).document
    assert document["confirmed_in_directory"] is True
    # LESSONS L5: assert the NAMESPACE, not the handle the write returned. A directory listing
    # is the one check a wrongly named file cannot pass.
    listed = {item.name for item in tmp_path.iterdir()}
    assert "exported.bin" in listed
    assert "again.bin" in listed
    body = output.read_bytes()
    assert len(body) == int(document["bytes"])
    assert hashlib.sha256(body).hexdigest() == document["digest"]


def test_an_export_never_overwrites_evidence_already_written(
    database_with_evidence: str, tmp_path: Path, cli: CliRunner
) -> None:
    entry_id = _first_entry_id(cli, database_with_evidence)
    output = tmp_path / "once.bin"
    output.write_bytes(b"an earlier export an operator kept")
    run = cli("ledger", "export", database_with_evidence, str(entry_id), "--output", str(output))
    assert run.code == REFUSED
    assert "already exists" in run.err
    assert output.read_bytes() == b"an earlier export an operator kept"


def test_an_export_without_a_destination_is_refused(
    database_with_evidence: str, cli: CliRunner
) -> None:
    entry_id = _first_entry_id(cli, database_with_evidence)
    run = cli("ledger", "export", database_with_evidence, str(entry_id))
    assert run.code == REFUSED
    assert "--output" in run.err


def test_an_export_to_a_place_that_cannot_hold_a_file_is_refused(
    database_with_evidence: str, tmp_path: Path, cli: CliRunner
) -> None:
    entry_id = _first_entry_id(cli, database_with_evidence)
    run = cli(
        "ledger",
        "export",
        database_with_evidence,
        str(entry_id),
        "--output",
        str(tmp_path / "absent-directory" / "file.bin"),
    )
    assert run.code == REFUSED
    assert "could not be created" in run.err
    assert "Traceback" not in run.text


def test_a_written_file_that_the_directory_does_not_list_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # LESSONS L5, driven directly: the case the confirmation exists for is the one where the
    # write reports success and the NAME is wrong. Nothing in a real run produces that, so the
    # listing is faked -- and without this the guard is a guard no test can tell from its absence.
    from okto_grafx.cli import commands
    from okto_grafx.domain.errors import GrafxConfigurationError

    monkeypatch.setattr(commands.os, "listdir", lambda _directory: ["a-different-name"])
    with pytest.raises(GrafxConfigurationError) as raised:
        commands._write_evidence(str(tmp_path / "evidence.bin"), b"preserved bytes")
    assert "ABSENT" in raised.value.message
    assert "do not treat this file as the evidence" in raised.value.message


class _DriftingDigests:
    """Digests the bytes it is given the first time and different bytes every time after.

    Stands in for a file whose content is not what the write put there. Looser than ``hashlib``
    in exactly that way and in no other: every call still returns a real SHA-256.
    """

    def __init__(self) -> None:
        """Start with the count that decides which answer the next call gets."""
        self.calls = 0

    def sha256(self, data: bytes) -> object:
        """Return the true digest once, then the digest of something else."""
        self.calls += 1
        return hashlib.sha256(data if self.calls == 1 else data + b"drift")


def test_a_written_file_whose_bytes_do_not_read_back_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from okto_grafx.cli import commands
    from okto_grafx.domain.errors import GrafxConfigurationError

    drifting = _DriftingDigests()
    monkeypatch.setattr(commands, "hashlib", drifting)
    with pytest.raises(GrafxConfigurationError) as raised:
        commands._write_evidence(str(tmp_path / "lying.bin"), b"preserved bytes")
    assert "digest to" in raised.value.message
    assert drifting.calls == 2, "the confirmation must digest the file it read back by name"


def test_the_confirmation_passes_for_a_file_that_really_was_written(tmp_path: Path) -> None:
    # A34: the two guards above are only meaningful if the honest path is unaffected.
    from okto_grafx.cli import commands

    written = commands._write_evidence(str(tmp_path / "honest.bin"), b"preserved bytes")
    assert written["confirmed"] is True
    assert (tmp_path / "honest.bin").read_bytes() == b"preserved bytes"
    assert written["digest"] == hashlib.sha256(b"preserved bytes").hexdigest()


def test_an_empty_quarantine_reports_a_clean_answer(database_path: str, cli: CliRunner) -> None:
    run = cli("quarantine", "list", database_path)
    assert run.code == OK
    assert "(nothing is quarantined)" in run.out


def test_quarantine_lists_what_it_preserved_with_its_manifest(
    database_with_evidence: str, cli: CliRunner
) -> None:
    run = cli("quarantine", "list", database_with_evidence)
    assert run.code == FINDINGS
    document = cli("quarantine", "list", database_with_evidence, "--json").document
    entry = document["entries"][0]
    assert entry["origin"].endswith(".wal")
    assert entry["reason"] == "truncated_tail"
    assert int(entry["length"]) > 0
    assert len(str(entry["digest"])) == 64
    assert entry["payload_file"].startswith("quarantine/")
    assert entry["manifest_file"].endswith("manifest.json")


def test_inspecting_a_quarantine_entry_shows_its_manifest(
    database_with_evidence: str, cli: CliRunner
) -> None:
    name = _first_quarantine_name(cli, database_with_evidence)
    run = cli("quarantine", "inspect", database_with_evidence, name)
    assert run.code == OK
    assert "origin" in run.out
    assert "digest" in run.out
    assert name in run.out


def test_inspecting_a_quarantine_entry_that_does_not_exist_is_refused(
    database_with_evidence: str, cli: CliRunner
) -> None:
    run = cli("quarantine", "inspect", database_with_evidence, "no-such-entry")
    assert run.code == REFUSED
    assert "quarantine_error" in run.err


def test_reading_quarantined_bytes_proves_them_against_the_manifest(
    database_with_evidence: str, tmp_path: Path, cli: CliRunner
) -> None:
    name = _first_quarantine_name(cli, database_with_evidence)
    listing = cli("quarantine", "list", database_with_evidence, "--json").document
    manifest_digest = listing["entries"][0]["digest"]
    output = tmp_path / "quarantined.bin"
    document = cli(
        "quarantine", "read", database_with_evidence, name, "--output", str(output), "--json"
    ).document
    assert document["confirmed_in_directory"] is True
    assert output.name in {item.name for item in tmp_path.iterdir()}
    body = output.read_bytes()
    assert hashlib.sha256(body).hexdigest() == manifest_digest
    assert document["digest"] == manifest_digest


def test_the_bytes_exported_from_the_ledger_are_the_bytes_quarantine_holds(
    database_with_evidence: str, tmp_path: Path, cli: CliRunner
) -> None:
    # The two doors preserve one range, and an operator has to be able to trust that they agree.
    entry_id = _first_entry_id(cli, database_with_evidence)
    name = _first_quarantine_name(cli, database_with_evidence)
    from_ledger = tmp_path / "from-ledger.bin"
    from_quarantine = tmp_path / "from-quarantine.bin"
    cli("ledger", "export", database_with_evidence, str(entry_id), "--output", str(from_ledger))
    cli("quarantine", "read", database_with_evidence, name, "--output", str(from_quarantine))
    assert from_ledger.read_bytes() == from_quarantine.read_bytes()


def test_metrics_reports_whether_an_endpoint_is_exposed(
    database_path: str, cli: CliRunner
) -> None:
    without = cli("metrics", database_path, "--json")
    assert without.code == OK
    assert without.document["endpoint"] is None
    with_endpoint = cli("metrics", database_path, "--metrics", "openmetrics", "--json")
    assert with_endpoint.code == OK
    endpoint = with_endpoint.document["endpoint"]
    assert isinstance(endpoint, str) and endpoint.startswith("http://")
    assert endpoint.endswith("/metrics")
    assert int(with_endpoint.document["series_count"]) > 0


def test_metrics_says_plainly_when_the_no_op_sink_keeps_nothing(
    database_path: str, cli: CliRunner
) -> None:
    run = cli("metrics", database_path)
    assert run.code == OK
    assert "no endpoint is exposed" in run.out
    assert "no-op sink" in run.out
