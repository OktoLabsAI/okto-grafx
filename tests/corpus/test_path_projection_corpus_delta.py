"""The pinned Pulse callsites, with explicit current-language verdict changes."""

from __future__ import annotations

import json
from pathlib import Path

CORPUS = Path(__file__).with_name("pulse_query_corpus_1_0.json")
ADMITTED = "MATCH path = (a:Decision)-[r:supersedes]->(b:Decision) RETURN path"
STILL_REFUSED = {
    "empty query",
    "non-mutating unsupported root",
    "unsupported clause after a supported root",
    "REMOVE",
    "DROP",
    "ALTER",
    "LOAD CSV",
    "COPY",
    "UNION",  # Historical probe returns n.id vs m.id, now a deliberate name mismatch.
}


def test_path_projection_stays_admitted_and_union_name_change_is_explicit() -> None:
    frozen = json.loads(CORPUS.read_text(encoding="utf-8"))
    raw = frozen["public_raw_contract"]
    probes = raw["probes"]
    by_construct = {probe["construct"]: probe for probe in probes}
    projected = by_construct["path projection"]

    assert frozen["entry_count"] == len(frozen["entries"]) == 97
    assert raw["probe_count"] == len(probes) == 87
    assert raw["engine_accepted"] == 78
    assert raw["engine_refused"] == 9
    assert raw["contract_refused"] == 14
    assert sum(probe["contract_disposition"] == "allowed" for probe in probes) == 73
    assert sum(probe["contract_disposition"] == "refused" for probe in probes) == 14
    assert {
        probe["construct"] for probe in probes if probe["engine_verdict"] == "refused"
    } == STILL_REFUSED
    assert [
        probe["construct"]
        for probe in probes
        if probe["contract_disposition"] == "allowed"
        and probe["engine_verdict"] == "refused"
    ] == ["UNION"]
    assert by_construct["UNION"]["acceptance_phase"] == "analysis_error"
    assert "same column names" in by_construct["UNION"]["error"]
    assert projected["probe"] == ADMITTED
    assert projected["contract_disposition"] == "allowed"
    assert projected["engine_verdict"] == "accepted"
    assert projected["acceptance_phase"] == "planned"
    assert projected["error"] is None

    # The path projection is a raw grammar capability, not one of the 97 extracted callsites.
    # Later chained OPTIONAL MATCH support admits I64 independently of this raw path probe.
    scoring = next(entry for entry in frozen["entries"] if entry["id"] == "I64")
    assert scoring["classification"] == "already_supported"
    assert scoring["expected"]["error"] is None
    assert frozen["counts"]["classification:already_supported"] == 83
    assert frozen["counts"]["classification:generic_gap"] == 12
