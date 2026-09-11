"""The frozen Pulse query corpus must stay true to the baselines it claims to describe.

A corpus is only worth having if it cannot drift.  These tests re-derive it from the pinned
Pulse commits and refuse anything the freezer could not prove: an unclassified receiver, a
statement built from a name it cannot resolve, or one template carrying two verdicts.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import pulse_query_corpus as freezer  # noqa: E402 - path is prepared immediately above

VALID_CLASSIFICATIONS = {
    "already_supported",
    "structured_primitive",
    "generic_gap",
    "runtime_fragment",
    "duplicate_text",
    "fragment",
}
VALID_CLASSES = {"read", "write", "mutation", "control", "fragment"}

PUBLIC_NAMED_TEMPLATE_IDS = {
    "COUNT_ALL_NODES",
    "COUNT_ALL_NODES_BY_TYPE",
    "EXPLAIN_CONSTRAINT",
    "EXPLAIN_CONSTRAINT_ORIGINS",
    "EXPLAIN_CONSTRAINT_VIOLATIONS",
    "FIND_CONTRADICTIONS_ALL",
    "FIND_CONTRADICTIONS_BY_NODE",
    "FIND_SIMILAR_DECISIONS_TEXT_FALLBACK",
    "GET_ALL_NODES",
    "GET_ALL_NODES_AFTER_CURSOR",
    "GET_ALL_NODES_BY_TYPE",
    "GET_ALL_NODES_BY_TYPE_AFTER_CURSOR",
    "GET_DECISION_HISTORY",
    "GET_LEARNING_FROM_BUGS",
    "GET_RELATED_CONTEXT",
    "GET_SUPERSEDENCE_CHAIN",
    "LIST_ALTERNATIVES",
}

RAW_GRAMMAR_GROUPS = {
    "root": {"MATCH", "OPTIONAL MATCH", "UNWIND", "WITH", "RETURN"},
    "clause": {"WHERE", "UNION", "DISTINCT", "ORDER BY", "LIMIT", "AS"},
    "boolean": {"AND", "OR", "NOT", "IN"},
    "null": {"IS NULL", "IS NOT NULL"},
    "literal": {"TRUE", "FALSE", "quoted string"},
    "parameter": {"scalar", "list", "map batch"},
    "string": {"CONTAINS", "STARTS WITH", "ENDS WITH"},
    "aggregation": {"COUNT", "COLLECT", "SUM", "AVG", "MIN", "MAX"},
    "expression": {
        "CASE searched",
        "CASE simple",
        "list index",
        "map access",
    },
    "function": {
        "label",
        "coalesce",
        "string_split",
        "size",
        "timestamp",
        "similarity",
        "similarity_score",
    },
    "pattern": {
        "anonymous node",
        "typed relationship",
        "incoming",
        "outgoing",
        "undirected",
        "untyped relationship",
        "polymorphic node",
        "fixed hop 2",
        "variable length depth 1",
        "variable length depth 2",
        "named path",
    },
    "result": {"node projection", "relationship projection", "path projection"},
    "limits": {
        "path cap 20",
        "beyond path cap",
        "unbounded variable length",
        "explicit LIMIT present",
        "LIMIT only inside a string literal",
    },
    "security": {
        "root operation as a homoglyph",
        "write keyword as a homoglyph",
        "write keyword in a line comment",
        "write keyword in a block comment",
        "write keyword in a string literal",
        "write keyword inside an identifier",
    },
    "taxonomy": {
        "empty query",
        "non-mutating unsupported root",
        "unsupported clause after a supported root",
    },
    "forbidden_write": {
        "CREATE",
        "MERGE",
        "SET",
        "DELETE",
        "DETACH DELETE",
        "REMOVE",
        "DROP",
        "ALTER",
        "LOAD CSV",
        "COPY",
    },
}

CONTRACT_BEHAVIOUR_VALUES = {
    "node label domain": 11,
    "relationship domain": 16,
    "endpoint pairs": 69,
    "root operations": ["MATCH", "OPTIONAL", "UNWIND", "WITH", "RETURN"],
    "publicly unsupported trailing tokens": ["CALL", "YIELD"],
    "related-context directions": ["both", "incoming", "outgoing"],
    "related-context depths": [1, 2],
    "graph layers": ["canonical", "working", "all"],
    "default row cap": 1000,
    "maximum row cap": 10000,
    "default timeout": 5000,
    "maximum timeout": 30000,
    "traversal depth cap": 20,
    "write attempt": "unsafe_cypher",
    "unsupported root": "unsupported_operation",
    "unenforceable canonical filter": "canonical_filter_unenforceable",
    "paired execution": "execute_read_only_pair",
    "columns": "columns",
    "row count": "row_count",
    "truncation": "truncated",
    "omitted row count": "working_omitted_count",
}

RESULT_ENVELOPE_BEHAVIOURS = {
    "paired execution",
    "projection applied to every result",
    "columns",
    "row count",
    "truncation",
    "omitted row count",
}

SINGLE_ROW_ID_READS = {
    "I09",
    "I31",
    "I35",
    "I36",
    "I39",
    "I64",
    "public:core:cypher_templates.EXPLAIN_CONSTRAINT",
}


def _baselines_available() -> tuple[bool, str]:
    """Both pinned commits must be readable, or there is nothing to verify against."""

    for name, spec in freezer.BASELINES.items():
        root = freezer._baseline_root(name)
        if not (root / ".git").exists() and not root.is_dir():
            return False, f"baseline {name!r} is not present at {root}"
        try:
            subprocess.run(
                ["git", "-C", str(root), "cat-file", "-e", f"{spec['sha']}^{{commit}}"],
                capture_output=True,
                check=True,
            )
        except (subprocess.CalledProcessError, OSError):
            return False, (
                f"baseline {name!r} does not contain {spec['sha']}; set "
                f"{spec['env']} to a checkout that does"
            )
    return True, ""


requires_baselines = pytest.mark.skipif(
    not _baselines_available()[0],
    reason=_baselines_available()[1] or "baselines available",
)


@pytest.fixture(scope="module")
def frozen() -> dict:
    if not freezer.CORPUS_PATH.exists():
        pytest.fail(f"the frozen corpus is missing at {freezer.CORPUS_PATH}")
    return json.loads(freezer.CORPUS_PATH.read_text(encoding="utf-8"))


def test_the_frozen_corpus_is_internally_complete(frozen: dict) -> None:
    """Every entry carries the provenance and verdict that make it auditable."""

    assert frozen["descriptor"] == freezer.CORPUS_ID
    assert frozen["kg_query_contract_version"] == freezer.QUERY_CONTRACT_VERSION
    assert frozen["entry_count"] == len(frozen["entries"])
    assert frozen["entries"], "a corpus with no entries describes nothing"

    identifiers: set[str] = set()
    for entry in frozen["entries"]:
        assert entry["id"] not in identifiers, f"duplicate entry id {entry['id']}"
        identifiers.add(entry["id"])
        assert entry["classification"] in VALID_CLASSIFICATIONS, entry["id"]
        assert entry["class"] in VALID_CLASSES, entry["id"]
        origins = entry["origins"] if "origins" in entry else [entry["origin"]]
        assert origins, entry["id"]
        for origin in origins:
            assert origin["baseline"] in freezer.BASELINES, entry["id"]
            assert origin["path"].endswith(".py"), entry["id"]
            assert origin["symbol"], entry["id"]
        assert entry["template"], entry["id"]
        if entry["classification"] == "generic_gap":
            assert entry["parse_error"], (
                f"{entry['id']} is called a gap without saying what was refused"
            )


def test_the_descriptor_pins_both_baselines(frozen: dict) -> None:
    for name, spec in freezer.BASELINES.items():
        recorded = frozen["baselines"][name]["sha"]
        assert recorded == spec["sha"], (
            f"{name}: corpus says {recorded}, freezer pins {spec['sha']}"
        )
        assert len(recorded) == 40
        assert all(character in "0123456789abcdef" for character in recorded)


def test_the_digest_covers_the_entire_payload(frozen: dict) -> None:
    asserted = {key: value for key, value in frozen.items() if key != "digest"}
    digest = hashlib.sha256(
        json.dumps(asserted, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    assert frozen["digest"] == digest


def test_only_public_raw_engine_probes_apply_the_pulse_nfkc_execution_step() -> None:
    text = "ＭＡＴＣＨ (n:Decision) RETURN n.id"

    internal_phase, internal_error = freezer._try_accept(text)
    assert internal_phase == "parse_error"
    assert internal_error is not None

    public_phase, public_error = freezer._try_accept(text, normalize_unicode=True)
    assert public_phase == "analysed_only"
    assert public_error is None


@requires_baselines
def test_regenerating_the_corpus_reproduces_it_exactly(frozen: dict) -> None:
    """The freeze is deterministic, or it is not a freeze.

    This is also what makes the corpus fail-closed: rebuilding it re-runs every refusal, so
    a form that stopped being provable, a receiver nobody classified, or a template that
    gained a second verdict all surface here rather than in a later milestone.
    """

    rebuilt = freezer.build_corpus()
    assert rebuilt == frozen, (
        "the frozen corpus no longer matches the baselines; rerun "
        "`python tools/pulse_query_corpus.py --write` and review the diff"
    )


def test_the_allowlist_is_empty_and_any_entry_would_need_a_reason() -> None:
    """Nothing is tolerated today, and anything added later must say why.

    The freeze stands on resolution alone: statements are followed through their active
    wrappers, and a form that cannot be followed breaks the freeze instead of being waved
    through.  An entry here would be a form the corpus does not describe.
    """

    allowlist = json.loads(freezer.ALLOWLIST_PATH.read_text(encoding="utf-8"))
    assert allowlist["dynamic_statements"] == {}, allowlist["dynamic_statements"]
    assert "why" in allowlist["_comment"].lower()


@requires_baselines
def test_a_new_or_moved_callsite_fails_the_freeze() -> None:
    """The audited surface is an invariant: drift must fail, not redefine itself."""

    original = dict(freezer.AUDITED_FAMILY_COUNTS)
    try:
        freezer.AUDITED_FAMILY_COUNTS["okto_pulse/core/kg/scoring.py"] = (2, 3)
        with pytest.raises(freezer.FreezeError, match="audited table"):
            freezer.build_corpus()
    finally:
        freezer.AUDITED_FAMILY_COUNTS.clear()
        freezer.AUDITED_FAMILY_COUNTS.update(original)


@requires_baselines
def test_a_preventive_family_may_not_drift_onto_another_statement() -> None:
    """Marked by origin and checked against the audited id, so it cannot slide."""

    original = dict(freezer.PREVENTIVE_ORIGINS)
    try:
        freezer.PREVENTIVE_ORIGINS[
            ("okto_pulse/core/kg/canonical_stale_reconciler.py", 761)
        ] = "I44"
        with pytest.raises(freezer.FreezeError, match="preventive family"):
            freezer.build_corpus()
    finally:
        freezer.PREVENTIVE_ORIGINS.clear()
        freezer.PREVENTIVE_ORIGINS.update(original)


@requires_baselines
def test_a_statement_the_freezer_cannot_follow_fails_the_freeze() -> None:
    """Turning off wrapper resolution must refuse, not silently drop the forms."""

    original = freezer._forwarded_arguments
    try:
        freezer._forwarded_arguments = lambda *args, **kwargs: []
        with pytest.raises(freezer.FreezeError, match="cannot follow"):
            freezer.build_corpus()
    finally:
        freezer._forwarded_arguments = original


@requires_baselines
def test_a_baseline_at_the_wrong_commit_fails_the_freeze() -> None:
    """The corpus describes exact commits, so a different one is a different corpus."""

    original = dict(freezer.BASELINES["core"])
    try:
        freezer.BASELINES["core"]["sha"] = "9f6f37d"
        with pytest.raises(freezer.FreezeError):
            freezer.build_corpus()
    finally:
        freezer.BASELINES["core"].update(original)


@requires_baselines
def test_one_template_may_not_carry_two_verdicts() -> None:
    """Duplicate templates are fine; duplicate templates with different answers are not."""

    findings = [
        freezer.Finding(
            entry_id="synthetic:a",
            baseline="core",
            path="synthetic.py",
            line=1,
            symbol="synthetic",
            surface="internal_execute",
            receiver="scope",
            template_kind="literal",
            template="MATCH (n) RETURN n",
            statement_class="read",
            classification="already_supported",
        ),
        freezer.Finding(
            entry_id="synthetic:b",
            baseline="core",
            path="synthetic.py",
            line=2,
            symbol="synthetic",
            surface="internal_execute",
            receiver="scope",
            template_kind="literal",
            template="MATCH (n) RETURN n",
            statement_class="read",
            classification="generic_gap",
        ),
    ]
    with pytest.raises(freezer.FreezeError, match="classified two ways"):
        freezer._check_duplicates(findings)


def _by_id(frozen: dict, entry_id: str) -> dict:
    for entry in frozen["entries"]:
        if entry.get("id") == entry_id:
            return entry
    pytest.fail(f"{entry_id} is not in the frozen corpus")
    raise AssertionError  # unreachable, keeps the type checker honest


def test_no_statement_with_a_projection_is_recorded_without_one(frozen: dict) -> None:
    """An empty expected is worse than none: it reads as a fact and asserts nothing.

    The first version of this corpus shipped every read with ``columns: []`` because a
    regex silently failed to match, and nothing noticed -- the field was present, so it
    looked answered.
    """

    for entry in frozen["entries"]:
        expected = entry.get("expected") or {}
        template = entry["template"].upper()
        if "RETURN" in template and entry.get("class") != "fragment":
            assert expected.get("columns") or expected.get("returns"), (
                f"{entry['id']} has a RETURN but records no projection"
            )
        if " SET " in template:
            assert expected.get("targets"), f"{entry['id']} has a SET but no targets"


def test_the_audited_family_inventory_holds(frozen: dict) -> None:
    """The per-module table is the audited surface; drift must fail, not redefine."""

    families = [e for e in frozen["entries"] if e["surface"] == "internal_family"]
    assert len(families) == 68, len(families)
    reads = [e for e in families if e["class"] == "read"]
    writes = [e for e in families if e["class"] == "write"]
    assert (len(reads), len(writes)) == (47, 21), (len(reads), len(writes))

    per_module: dict[str, list[int]] = {}
    for family in families:
        pair = per_module.setdefault(family["origins"][0]["path"], [0, 0])
        pair[0 if family["class"] == "read" else 1] += 1
    assert {path: tuple(pair) for path, pair in per_module.items()} == (
        freezer.AUDITED_FAMILY_COUNTS
    )

    preventive = sorted(
        (e["id"], e["origins"][0]["line"])
        for e in families
        if e["reachability"] == "preventive"
    )
    assert preventive == [("I21", 761), ("I22", 874)], preventive
    current = [e for e in families if e["reachability"] == "runtime_current"]
    assert len(current) == 66, len(current)


def test_the_public_template_inventory_is_exact(frozen: dict) -> None:
    prefix = "public:core:cypher_templates."
    public = [
        entry
        for entry in frozen["entries"]
        if entry["surface"] == "public_named_template"
    ]
    named = [entry for entry in public if entry["template_kind"] != "generated"]
    generated = [entry for entry in public if entry["template_kind"] == "generated"]

    assert {entry["id"].removeprefix(prefix) for entry in named} == (
        PUBLIC_NAMED_TEMPLATE_IDS
    )
    labels = frozen["structural_authorities"]["node_label"]["members"]
    assert {entry["id"].removeprefix(prefix) for entry in generated} == {
        f"supersedence_chain_template({label})" for label in labels
    }
    assert len(named) == 17
    assert len(generated) == 11
    assert len(public) == 28

    normalized = {" ".join(entry["template"].split()) for entry in public}
    assert len(normalized) == 27
    duplicates = [
        entry for entry in public if entry["classification"] == "duplicate_text"
    ]
    assert [entry["id"] for entry in duplicates] == [
        f"{prefix}supersedence_chain_template(Decision)"
    ]


def test_the_frozen_surface_contains_no_runtime_fragment(frozen: dict) -> None:
    fragments = [
        entry["id"]
        for entry in frozen["entries"]
        if entry["classification"] == "runtime_fragment"
    ]
    assert fragments == []
    assert frozen["counts"].get("classification:runtime_fragment", 0) == 0


def test_every_entry_has_a_structured_expected_outcome(frozen: dict) -> None:
    """Expected outcomes distinguish rows, effects and typed refusal semantics."""

    for entry in frozen["entries"]:
        if entry["class"] == "fragment":
            continue
        expected = entry["expected"]
        error = expected.get("error")
        if entry["acceptance_phase"] == "planned":
            assert error is None, entry["id"]
        elif entry["acceptance_phase"] == "not_evaluated":
            assert entry["classification"] == "duplicate_text", entry["id"]
            assert error is None, entry["id"]
        else:
            assert isinstance(error, dict), entry["id"]
            assert error.get("phase") == entry["acceptance_phase"], entry["id"]
            assert error.get("code"), entry["id"]
            assert error.get("type") or error.get("message"), entry["id"]

        if entry["class"] == "read":
            assert expected["kind"] == "rows", entry["id"]
            assert expected["cardinality"], entry["id"]
            assert expected["column_count"] == len(expected["columns"]), entry["id"]
            if entry["acceptance_phase"] != "planned":
                continue
            metadata = expected.get("column_metadata")
            assert isinstance(metadata, list), entry["id"]
            assert len(metadata) == expected["column_count"], entry["id"]
            assert [item.get("expression") for item in metadata] == expected[
                "columns"
            ], entry["id"]
            for item in metadata:
                assert isinstance(item.get("type"), str) and item["type"], entry["id"]
                assert isinstance(item.get("nullable"), bool), entry["id"]
            continue

        assert expected["kind"] == "effect", entry["id"]
        assert expected["effect"] in {
            "create",
            "set_properties",
            "delete",
            "detach_delete",
        }, entry["id"]
        assert expected["expected_count"], entry["id"]
        assert expected["atomicity"], entry["id"]
        assert expected["targets"], entry["id"]
        if entry["acceptance_phase"] == "planned" and expected["returns"]:
            metadata = expected.get("return_metadata")
            assert isinstance(metadata, list), entry["id"]
            assert [item.get("expression") for item in metadata] == expected[
                "returns"
            ], entry["id"]
            for item in metadata:
                assert isinstance(item.get("type"), str) and item["type"], entry["id"]
                assert isinstance(item.get("nullable"), bool), entry["id"]


def test_identity_bound_reads_do_not_claim_unbounded_cardinality(frozen: dict) -> None:
    for entry_id in SINGLE_ROW_ID_READS:
        entry = _by_id(frozen, entry_id)
        assert entry["class"] == "read", entry_id
        assert entry["expected"]["cardinality"] == "at_most_one_row", entry_id


def test_representative_families_carry_the_shape_the_oracle_needs(frozen: dict) -> None:
    """Spot-checks that would each have passed vacuously against an empty expected."""

    count_all = _by_id(frozen, "public:core:cypher_templates.COUNT_ALL_NODES")
    assert count_all["expected"]["cardinality"] == "exactly_one_row"
    assert count_all["expected"]["ordering"] == "multiset"

    explain = _by_id(frozen, "public:core:cypher_templates.EXPLAIN_CONSTRAINT")
    assert explain["expected"]["columns"], explain["expected"]

    # A label-wide rewrite is not "at most one row".
    relabel = _by_id(frozen, "I05")
    assert relabel["expected"]["effect"] == "set_properties"
    assert relabel["expected"]["expected_count"] == "all_matched_rows"

    families = {
        e["id"]: e for e in frozen["entries"] if e["surface"] == "internal_family"
    }
    batched = [
        e
        for e in families.values()
        if "UNWIND" in e["template"].upper()
        and e["origins"][0]["path"].endswith("scoring.py")
    ]
    assert batched, "the scoring batch writes are missing"
    for entry in batched:
        assert entry["expected"]["expected_count"] == "one_per_batch_row", entry["id"]

    cancellation = [
        e
        for e in families.values()
        if e["origins"][0]["path"].endswith("cancellation_decay.py")
    ]
    assert len(cancellation) == 2, [e["id"] for e in cancellation]
    for entry in cancellation:
        assert entry["expected"]["targets"], entry["id"]

    dedup_writes = [
        e
        for e in families.values()
        if e["origins"][0]["path"].endswith("dedup_migration.py")
        and e["class"] == "write"
    ]
    effects = {entry["expected"]["effect"] for entry in dedup_writes}
    assert {"create", "delete", "detach_delete"} <= effects, effects


def test_every_hole_is_bound_to_an_enumerated_domain(frozen: dict) -> None:
    """An authority names the values a hole may take, not the kind of name it has.

    Classifying a hole as "a label" bounds nothing; ranging it over the eleven declared node
    types does. A domain with neither members nor a named builder is a bucket, and the freeze
    refuses one.
    """

    authorities = frozen["structural_authorities"]
    assert authorities, "no holes were bound at all"
    for domain, info in authorities.items():
        assert info["holes"] > 0, domain
        assert info["members"] or info["builder_source"], (
            f"{domain} enumerates nothing and names no builder"
        )
    assert authorities["node_label"]["member_count"] == 11, authorities["node_label"]
    assert "Decision" in authorities["node_label"]["members"]
    assert len(set(authorities["node_label"]["members"])) == 11
    relationship = authorities["relationship_type"]
    assert relationship["member_count"] == 16, relationship
    assert len(set(relationship["members"])) == 16
    assert relationship["endpoint_pair_count"] == 69, relationship
    assert len(set(relationship["endpoint_pairs"])) == 69


@requires_baselines
def test_an_unbound_hole_fails_the_freeze() -> None:
    """Removing a hole from its domain must refuse, which is what makes the binding real."""

    original = dict(freezer.HOLE_DOMAINS)
    try:
        freezer.HOLE_DOMAINS["node_label"] = tuple(
            name for name in original["node_label"] if name != "node_type"
        )
        with pytest.raises(freezer.FreezeError, match="bound to no closed domain"):
            freezer.build_corpus()
    finally:
        freezer.HOLE_DOMAINS.clear()
        freezer.HOLE_DOMAINS.update(original)


def test_the_raw_matrix_keeps_contract_and_engine_apart(frozen: dict) -> None:
    """A write is refused by the contract and accepted by the engine; both must be visible."""

    raw = frozen["public_raw_contract"]
    writes = [p for p in raw["probes"] if p["category"] == "forbidden_write"]
    assert writes, "the matrix records no forbidden writes"
    for probe in writes:
        assert probe["contract_disposition"] == "refused", probe["construct"]
    assert any(probe["engine_verdict"] == "accepted" for probe in writes), (
        "if the engine refused every write the two answers would be indistinguishable"
    )

    owed = [
        p["construct"]
        for p in raw["probes"]
        if p["contract_disposition"] == "allowed" and p["engine_verdict"] == "refused"
    ]
    functions = {
        probe["construct"]: probe
        for probe in raw["probes"]
        if probe["category"] == "function"
    }
    # M-PULSE-2B took the three scalar helpers and M-PULSE-2D took the last two, so no
    # function is owed any more.  The claim is made over EVERY function probe rather than a
    # written list, because a list is what lets a new function arrive already forgotten.
    for name, probe in functions.items():
        assert probe["contract_disposition"] == "allowed", name
        assert probe["engine_verdict"] == "accepted", name
        assert probe["acceptance_phase"] == "planned", name
        assert probe["error"] is None, name
        assert name not in owed, owed
    for name in ("coalesce", "string_split", "size", "label", "timestamp"):
        assert name in functions, name

    by_construct = {probe["construct"]: probe for probe in raw["probes"]}
    # M-PULSE-2C closes the standalone list-index and both CASE grammar probes, M-PULSE-2D
    # closes the last two functions, M-PULSE-2E closes the one leading UNWIND source,
    # M-PULSE-2F closes the leading WITH and M-PULSE-2G closes the label-free node, which
    # takes the six all-node templates and I19 with it. M-PULSE-2I adds the decorative path,
    # M-PULSE-2J bounds the otherwise unbounded traversal, and M-PULSE-2K adds the narrow root
    # OPTIONAL MATCH. The two UNWIND probes with identical text remain distinct grammar keys,
    # so both have to ratchet beside map access rather than being deduplicated by their spelling.
    for name in (
        "list index",
        "CASE searched",
        "CASE simple",
        "label",
        "timestamp",
        "UNWIND",
        "WITH",
        "polymorphic node",
        "named path",
        "unbounded variable length",
        "OPTIONAL MATCH",
        "untyped relationship",
        "path projection",
        "map batch",
        "map access",
    ):
        probe = by_construct[name]
        assert probe["contract_disposition"] == "allowed", name
        assert probe["engine_verdict"] == "accepted", name
        assert probe["acceptance_phase"] == "planned", name
        assert probe["error"] is None, name
        assert name not in owed, owed

    accepted = [
        probe for probe in raw["probes"] if probe["engine_verdict"] == "accepted"
    ]
    refused = [probe for probe in raw["probes"] if probe["engine_verdict"] == "refused"]
    # M-PULSE-2H moved two ENTRIES and no probe; M-PULSE-2I moved two PROBES and no entry;
    # M-PULSE-2J and 2K each move one more probe. M-PULSE-2L then makes the engine verdict
    # follow the public NFKC execution path and closes the unsupported trailing-clause hole in
    # the Core authority, and M-PULSE-2M moves UNION. M-PULSE-2N then moves the untyped hop and
    # M-PULSE-2O moves the one exact path projection, closing the finite raw-contract debt.
    # The authorized language replacement now requires identical output names.
    # Preserve the historical probe text (n.id vs m.id) and record its refusal,
    # rather than rewriting the baseline to hide this intentional breaking change.
    assert len(accepted) == 78
    assert len(refused) == 9
    assert owed == ["UNION"]
    assert by_construct["UNION"]["acceptance_phase"] == "analysis_error"
    assert "same column names" in by_construct["UNION"]["error"]
    assert raw["contract_refused"] == 14
    assert raw["contract_error_codes"] == {
        "unsafe_cypher": 10,
        "unsupported_operation": 4,
    }

    assert frozen["counts"]["classification:already_supported"] == 83
    assert frozen["counts"]["classification:generic_gap"] == 12


def test_every_raw_probe_has_one_contract_and_engine_verdict(frozen: dict) -> None:
    raw = frozen["public_raw_contract"]
    probes = raw["probes"]
    assert raw["probe_count"] == len(probes)
    keys = [(probe["category"], probe["construct"]) for probe in probes]
    assert len(keys) == len(set(keys)), "a raw construct is recorded twice"

    for probe in probes:
        assert probe["contract_disposition"] in {"allowed", "refused"}, probe
        assert probe["engine_verdict"] in {"accepted", "refused"}, probe
        assert probe["acceptance_phase"], probe
        if probe["contract_disposition"] == "allowed":
            assert probe["contract_error_code"] is None, probe
            assert probe["contract_reason"] is None, probe
        else:
            assert probe["contract_error_code"], probe
            assert probe["contract_reason"], probe
        if probe["engine_verdict"] == "accepted":
            assert probe["error"] is None, probe
        else:
            assert probe["error"], probe


def test_the_raw_matrix_names_every_frozen_grammar_group(frozen: dict) -> None:
    probes = frozen["public_raw_contract"]["probes"]
    per_category: dict[str, set[str]] = {}
    for probe in probes:
        per_category.setdefault(probe["category"], set()).add(probe["construct"])

    for category, required in RAW_GRAMMAR_GROUPS.items():
        missing = required - per_category.get(category, set())
        assert not missing, f"{category} is missing raw probes for {sorted(missing)}"

    # These used to be collapsed into superficially similar probes.  Keep each semantic
    # question independent so one accepted form cannot stand in for another refused one.
    assert "TRUE/FALSE" not in per_category.get("literal", set())
    by_construct = {probe["construct"]: probe for probe in probes}
    assert "FALSE" in by_construct
    assert "map access" in by_construct
    assert "named path" in by_construct
    assert "path projection" in by_construct
    assert "outgoing" in by_construct
    assert "fixed hop 2" in by_construct

    for root in RAW_GRAMMAR_GROUPS["root"]:
        probe = " ".join(by_construct[root]["probe"].split()).upper()
        assert probe.startswith(root), (
            f"the {root} probe does not exercise it as a root"
        )


def test_the_raw_matrix_separates_security_normalization_and_error_codes(
    frozen: dict,
) -> None:
    probes = {
        probe["construct"]: probe for probe in frozen["public_raw_contract"]["probes"]
    }
    assert probes["write keyword as a homoglyph"]["contract_error_code"] == (
        "unsafe_cypher"
    )
    assert probes["root operation as a homoglyph"]["contract_disposition"] == "allowed"
    for construct in ("write keyword as a homoglyph", "root operation as a homoglyph"):
        assert probes[construct]["engine_verdict"] == "accepted", construct
        assert probes[construct]["acceptance_phase"] == "planned", construct
        assert probes[construct]["error"] is None, construct
    assert probes["write keyword in a line comment"]["contract_disposition"] == (
        "allowed"
    )
    assert probes["write keyword in a block comment"]["contract_disposition"] == (
        "allowed"
    )
    assert probes["write keyword in a string literal"]["contract_disposition"] == (
        "allowed"
    )
    assert probes["write keyword inside an identifier"]["contract_disposition"] == (
        "allowed"
    )
    assert probes["non-mutating unsupported root"]["contract_error_code"] == (
        "unsupported_operation"
    )
    assert (
        probes["unsupported clause after a supported root"]["contract_disposition"]
        == "refused"
    )
    assert (
        probes["unsupported clause after a supported root"]["contract_error_code"]
        == "unsupported_operation"
    )
    assert probes["unsupported clause after a supported root"]["engine_verdict"] == (
        "refused"
    )
    assert frozen["public_raw_contract"]["publicly_unsupported_tokens"] == [
        "CALL",
        "YIELD",
    ]

    writes = {
        probe["construct"]: probe
        for probe in frozen["public_raw_contract"]["probes"]
        if probe["category"] == "forbidden_write"
    }
    assert set(writes) == RAW_GRAMMAR_GROUPS["forbidden_write"]
    assert all(item["contract_disposition"] == "refused" for item in writes.values())
    for construct in {"CREATE", "MERGE", "SET", "DELETE", "DETACH DELETE"}:
        assert writes[construct]["engine_verdict"] == "accepted", construct


def test_the_public_contract_behaviour_matrix_is_complete(frozen: dict) -> None:
    raw = frozen["public_raw_contract"]
    matrix = raw["behaviour"]
    required_groups = {
        "schema_domain",
        "context_shape",
        "defaults_and_limits",
        "security_normalization",
        "error_taxonomy",
        "layer_enforcement",
        "result_envelope",
    }
    assert set(matrix["groups"]) == required_groups
    assert matrix["behaviour_count"] == len(matrix["behaviours"])
    assert matrix["per_group"] == {
        group: sum(item["group"] == group for item in matrix["behaviours"])
        for group in matrix["groups"]
    }

    behaviours = {item["name"]: item for item in matrix["behaviours"]}
    for name, expected in CONTRACT_BEHAVIOUR_VALUES.items():
        assert name in behaviours, name
        actual = behaviours[name]["value"]
        if name in {"node label domain", "relationship domain", "endpoint pairs"}:
            assert len(actual) == expected, name
        else:
            assert actual == expected, name

    required_evidence = {
        "LIMIT injection": "_auto_inject_limit",
        "path bounding runs before the layer rewrite": "_auto_bound_var_length_path",
        "comments stripped before tokenizing": "_strip_comments",
        "NFKC normalization": "_normalize_unicode",
        "string literals blanked": "_strip_string_literals",
        "canonical predicate injected per named variable": ".graph_layer = 'canonical'",
        "existing WHERE is parenthesised": "WHERE {canonical_filter} AND ({original_where}) ",
        "include_working skips the rewrite": "_rewrite_cypher_canonical_only",
    }
    for name, value in required_evidence.items():
        assert behaviours[name]["value"] == value, name

    result_names = {
        item["name"]
        for item in matrix["behaviours"]
        if item["group"] == "result_envelope"
    }
    assert RESULT_ENVELOPE_BEHAVIOURS <= result_names


def test_path_caps_and_serialization_have_dedicated_probes(frozen: dict) -> None:
    probes = {
        probe["construct"]: probe for probe in frozen["public_raw_contract"]["probes"]
    }
    assert "*1..20" in probes["path cap 20"]["probe"]
    assert "*1..21" in probes["beyond path cap"]["probe"]
    path_probe = " ".join(probes["path projection"]["probe"].split())
    returned = path_probe.rpartition("RETURN")[2].strip()
    assert returned
    assert f"{returned} =" in path_probe or f"{returned}=" in path_probe


def test_specialized_probes_exercise_the_observed_shape(frozen: dict) -> None:
    probes = {
        probe["construct"]: " ".join(probe["probe"].split())
        for probe in frozen["public_raw_contract"]["probes"]
    }
    assert "timestamp($created_at)" in probes["timestamp"]
    assert "space => 'pulse_corpus'" in probes["similarity"]
    assert "WHERE similarity(" in probes["similarity"]
    assert "space => 'pulse_corpus'" in probes["similarity_score"]
    assert "RETURN similarity_score()" in probes["similarity_score"]
    assert probes["fixed hop 2"].count("->") == 2
    assert "->" in probes["outgoing"]
    assert "RETURN r.id" in probes["map access"]
    assert " false " in f" {probes['FALSE'].lower()} "


def test_the_freezer_module_defines_each_name_once() -> None:
    """A duplicated definition silently shadows the first and is invisible in review.

    This happened: a bad splice left nineteen functions defined twice, and the later copies
    won. Nothing failed, and the corpus was built by code nobody had read.
    """

    import ast as _ast
    import collections

    tree = _ast.parse(Path(freezer.__file__).read_text(encoding="utf-8"))
    names: collections.Counter[str] = collections.Counter()
    for node in tree.body:
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
            names[node.name] += 1
        elif isinstance(node, _ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, _ast.Name):
                names[target.id] += 1
        elif isinstance(node, _ast.AnnAssign) and isinstance(node.target, _ast.Name):
            names[node.target.id] += 1
    duplicated = {name: count for name, count in names.items() if count > 1}
    assert not duplicated, duplicated


@requires_baselines
def test_the_planning_catalog_answers_what_it_must_before_any_verdict() -> None:
    """The oracle is proven, not assumed: typed reads must plan against this catalog."""

    sources = freezer._baseline_sources(
        freezer._baseline_root("core"), freezer.BASELINES["core"]["sha"]
    )
    for probe in freezer.CATALOG_SENTINELS:
        phase, error = freezer._try_accept(probe, sources)
        assert error is None, f"{probe} failed at {phase}: {error}"
        assert phase == "planned", (probe, phase)


def test_a_new_local_variable_originator_fails_through_the_public_check() -> None:
    """The independent discovery sweep follows a local into ``scope.execute``."""

    synthetic_path = "okto_pulse/core/kg/synthetic_new_originator.py"
    sources = {
        synthetic_path: """
def run_query(scope):
    statement = "MATCH (n:Decision) RETURN n.id"
    return scope.execute(statement)
"""
    }
    with pytest.raises(freezer.FreezeError) as raised:
        freezer.check_inventory_completeness("core", sources)
    message = str(raised.value)
    assert synthetic_path in message
    assert "neither the audited inventory" in message


def test_an_unresolved_statement_on_an_explicit_graph_scope_fails_closed() -> None:
    synthetic_path = "okto_pulse/core/kg/synthetic_dynamic_originator.py"
    sources = {
        synthetic_path: """
def run_query(scope, statement):
    return scope.execute(statement)
"""
    }
    with pytest.raises(freezer.FreezeError) as raised:
        freezer.check_inventory_completeness("core", sources)
    message = str(raised.value)
    assert synthetic_path in message
    assert "neither the audited inventory" in message


@requires_baselines
def test_a_checkout_away_from_the_pin_fails_the_freeze() -> None:
    """The corpus names the tree it describes; a different HEAD is a different tree."""

    original = dict(freezer.BASELINES["core"])
    try:
        freezer.BASELINES["core"]["sha"] = "9f6f37d0000000000000000000000000000000000"
        with pytest.raises(freezer.FreezeError):
            freezer.build_corpus()
    finally:
        freezer.BASELINES["core"].update(original)


def test_the_relationship_authority_is_exactly_the_pinned_schema(frozen: dict) -> None:
    """Sixteen names over sixty-nine endpoint pairs, not a floor."""

    relationship = frozen["structural_authorities"]["relationship_type"]
    assert relationship["member_count"] == 16, relationship["members"]
    assert relationship["endpoint_pair_count"] == 69, relationship[
        "endpoint_pair_count"
    ]
    assert "supersedes(Decision->Decision)" in relationship["endpoint_pairs"]
    assert frozen["excluded_modules"], "exclusions must be recorded, not merely absent"


@requires_baselines
def test_no_family_is_left_as_an_unresolved_fragment(frozen: dict) -> None:
    """A hole the freezer could not fill is a question about this file, not about Pulse.

    ``runtime_fragment`` means the freezer never presented the real text, so its refusal is
    evidence about the freezer.  At zero, every recorded verdict is about the engine.
    """

    unresolved = [
        entry["id"]
        for entry in frozen["entries"]
        if entry["classification"] == "runtime_fragment"
    ]
    assert unresolved == [], unresolved


@requires_baselines
def test_every_probe_disposition_is_the_contract_s_own_answer(frozen: dict) -> None:
    """Recompute each verdict from the pin rather than trusting the frozen field."""

    sources = freezer._baseline_sources(
        freezer._baseline_root("core"), freezer.BASELINES["core"]["sha"]
    )
    for probe in frozen["public_raw_contract"]["probes"]:
        verdict = freezer._contract_verdict(probe["probe"], sources)
        expected = "allowed" if verdict["admitted"] else "refused"
        assert probe["contract_disposition"] == expected, probe["construct"]
        assert probe["contract_error_code"] == verdict["error_code"], probe["construct"]


@requires_baselines
def test_the_two_refusals_are_not_interchangeable() -> None:
    """A write attempt and an unsupported root are different answers to the caller.

    Collapsing them into one "refused" would tell a client to stop using a query it is
    allowed to fix.
    """

    sources = freezer._baseline_sources(
        freezer._baseline_root("core"), freezer.BASELINES["core"]["sha"]
    )
    write = freezer._contract_verdict("MATCH (n:Decision) DELETE n", sources)
    unsupported = freezer._contract_verdict("COPY Decision FROM 'x.csv'", sources)
    assert write["error_code"] == "unsafe_cypher"
    assert unsupported["error_code"] == "unsupported_operation"


@requires_baselines
def test_masking_makes_a_keyword_in_a_comment_or_a_literal_invisible() -> None:
    """The blacklist reads tokens, and comments and literals are not tokens."""

    sources = freezer._baseline_sources(
        freezer._baseline_root("core"), freezer.BASELINES["core"]["sha"]
    )
    for text_ in (
        "MATCH (n:Decision) RETURN n.id // DELETE everything",
        "MATCH (n:Decision) RETURN n.id // CALL db.index() YIELD value",
        "MATCH (n:Decision) /* DROP TABLE */ RETURN n.id",
        "MATCH (n:Decision) /* CALL YIELD */ RETURN n.id",
        "MATCH (n:Decision) WHERE n.title = 'DELETE' RETURN n.id",
        "MATCH (n:Decision) WHERE n.title = 'CALL YIELD' RETURN n.id",
        "MATCH (n:Decision) WHERE n.deleted_at IS NULL RETURN n.id",
    ):
        assert freezer._contract_verdict(text_, sources)["admitted"], text_


@requires_baselines
def test_a_homoglyph_write_is_still_a_write() -> None:
    """NFKC runs before the blacklist, so a fullwidth DELETE is caught as one."""

    sources = freezer._baseline_sources(
        freezer._baseline_root("core"), freezer.BASELINES["core"]["sha"]
    )
    homoglyph = "MATCH (n:Decision) \uff24\uff25\uff2c\uff25\uff34\uff25 n"
    verdict = freezer._contract_verdict(homoglyph, sources)
    assert verdict["error_code"] == "unsafe_cypher", verdict


@requires_baselines
def test_a_validator_that_stops_masking_fails_the_freeze() -> None:
    """The reproduction is pinned to the original; a step that vanishes must break it."""

    original = freezer._VALIDATOR_STEPS
    try:
        freezer._VALIDATOR_STEPS = (*original, "_strip_something_that_does_not_exist")
        with pytest.raises(freezer.FreezeError, match="in that order"):
            freezer.build_corpus()
    finally:
        freezer._VALIDATOR_STEPS = original


@requires_baselines
def test_a_renamed_refusal_code_fails_the_freeze() -> None:
    """If the contract renames a code, the matrix must stop rather than publish the old one."""

    original = freezer._VALIDATOR_CODES
    try:
        freezer._VALIDATOR_CODES = ("unsafe_cypher", "totally_different_code")
        with pytest.raises(freezer.FreezeError, match="no longer the one"):
            freezer.build_corpus()
    finally:
        freezer._VALIDATOR_CODES = original


@requires_baselines
def test_a_reordered_execute_pipeline_fails_the_freeze() -> None:
    """Limit injection, path bounding and the layer rewrite are order-dependent."""

    original = freezer._EXECUTE_STEPS
    try:
        freezer._EXECUTE_STEPS = (
            "_rewrite_cypher_canonical_only",
            "validate_cypher_read_only",
        )
        with pytest.raises(freezer.FreezeError, match="order-dependent"):
            freezer.build_corpus()
    finally:
        freezer._EXECUTE_STEPS = original


@requires_baselines
def test_every_published_behaviour_group_carries_a_proven_fact(frozen: dict) -> None:
    """A group with no facts reads as covered while saying nothing."""

    behaviour = frozen["public_raw_contract"]["behaviour"]
    for group in behaviour["groups"]:
        assert behaviour["per_group"][group] > 0, group
    for item in behaviour["behaviours"]:
        assert item["evidence_kind"] in {
            "constant",
            "domain",
            "calls",
            "attribute",
            "error_code",
            "literal",
            "lookup_key",
            "mapping_key",
        }, item
        assert item["statement"].strip(), item


@requires_baselines
def test_a_behaviour_group_with_no_facts_fails_the_freeze() -> None:
    """Declaring a group is a promise to prove something about it."""

    original = freezer.CONTRACT_BEHAVIOUR_GROUPS
    try:
        freezer.CONTRACT_BEHAVIOUR_GROUPS = (*original, "a_group_nobody_proved")
        with pytest.raises(freezer.FreezeError, match="covers no behaviour"):
            freezer.build_corpus()
    finally:
        freezer.CONTRACT_BEHAVIOUR_GROUPS = original


@requires_baselines
def test_a_behaviour_whose_evidence_moved_fails_the_freeze() -> None:
    """Evidence that no longer exists cannot keep proving the statement it was cited for."""

    original = freezer.CONTRACT_BEHAVIOURS
    try:
        freezer.CONTRACT_BEHAVIOURS = (
            *original,
            (
                "error_taxonomy",
                "a code nobody raises",
                "error_code",
                "validate_cypher_read_only:no_such_code",
                "This statement has no evidence behind it.",
                None,
            ),
        )
        with pytest.raises(freezer.FreezeError, match="no longer raises"):
            freezer.build_corpus()
    finally:
        freezer.CONTRACT_BEHAVIOURS = original


@requires_baselines
def test_an_explicit_upper_bound_is_not_clamped_to_the_depth_cap(frozen: dict) -> None:
    """The cap fills in a MISSING bound; it does not lower one the caller wrote.

    Recorded because it is the opposite of what "traversal depth cap" suggests, and a
    milestone that assumed clamping would plan for a bound the contract never applies.
    """

    behaviour = frozen["public_raw_contract"]["behaviour"]["behaviours"]
    cap = [item for item in behaviour if item["name"] == "traversal depth cap"]
    assert len(cap) == 1, behaviour
    assert cap[0]["value"] == 20
    assert "not clamped to 20" in cap[0]["statement"]

    probes = {
        item["construct"]: item
        for item in frozen["public_raw_contract"]["probes"]
        if item["category"] == "limits"
    }
    assert probes["beyond path cap"]["contract_disposition"] == "allowed"


@requires_baselines
def test_a_sibling_baseline_is_used_only_when_its_head_is_the_pin() -> None:
    """Several checkouts of the same repository sit side by side; only one is described."""

    import os

    for name, spec in freezer.BASELINES.items():
        root = freezer._baseline_root(name)
        head = freezer._git(root, "rev-parse", "HEAD").strip()
        assert head == spec["sha"], f"{name}: {root} is at {head}, not {spec['sha']}"

    saved = {
        spec["env"]: os.environ.pop(spec["env"], None)
        for spec in freezer.BASELINES.values()
    }
    original = dict(freezer.BASELINES["core"])
    try:
        freezer.BASELINES["core"] = {
            **original,
            "sha": "0" * 40,
            "env": "PULSE_CORE_BASELINE_NO_SUCH_VAR",
        }
        with pytest.raises(
            freezer.FreezeError, match="PULSE_CORE_BASELINE_NO_SUCH_VAR"
        ):
            freezer._baseline_root("core")
    finally:
        freezer.BASELINES["core"] = original
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value
