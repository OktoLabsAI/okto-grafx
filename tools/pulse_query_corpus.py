"""Freeze the Pulse query corpus for KG query contract 1.0.

The corpus answers one question with evidence rather than opinion: which query forms does
Pulse actually hand to a graph, and which of them does this engine already accept?

Two surfaces are frozen.  The PUBLIC surface is the read-only template set the contract
exposes.  The INTERNAL surface is every statement Pulse delivers to
``GraphTransactionScope.execute`` -- the endpoint the Grafx provider still refuses, and
therefore the exact list M-PULSE-2 has to close.

Nothing here parses Cypher itself, implements clauses or touches a provider.  It reads two
immutable Pulse baselines, extracts forms, and classifies each one by asking this engine's own
parser whether it accepts it.  That keeps the classification auditable: rerunning the freezer
on the same SHAs must produce the same corpus, byte for byte.

Fail-closed is the point.  A statement the freezer cannot prove -- built from a name it cannot
resolve, or assembled at runtime -- is never guessed at.  It either resolves, or it appears in
the allowlist with a reason, or the freeze fails.

Usage:
    python tools/pulse_query_corpus.py --write     # regenerate the frozen corpus
    python tools/pulse_query_corpus.py --check     # verify the frozen corpus is current
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
CORPUS_PATH = REPO_ROOT / "tests" / "corpus" / "pulse_query_corpus_1_0.json"
ALLOWLIST_PATH = REPO_ROOT / "tests" / "corpus" / "pulse_query_allowlist.json"

# The two immutable baselines.  Overridable so the corpus can be regenerated from any
# checkout of the same commits, and never from the dirty working repositories.
BASELINES: dict[str, dict[str, Any]] = {
    "community": {
        # Sibling directory names tried next to this repository.  No absolute path: one
        # machine's layout is not part of the corpus, and a default that resolves only here
        # would make regeneration unreproducible everywhere else.
        "siblings": ("okto-pulse-grafx-mpulse1", "okto-pulse"),
        "env": "PULSE_COMMUNITY_BASELINE",
        # The milestone's final head.  Its src/ is byte-identical to 36c2fc6 -- the delta is
        # documentation and one test file, checked with `git diff 36c2fc6 befaf1e -- src/`
        # rather than taken on trust -- so pinning the final head changes no entry here.
        "sha": "befaf1e4f9da9d0cff7cfc0f4aee177ef0a3e595",
        "code_identical_to": "36c2fc6",
    },
    "core": {
        "siblings": ("okto-pulse-core-grafx-contract", "okto-pulse-core"),
        "env": "PULSE_CORE_BASELINE",
        "sha": "ab61b9a785f2018312fc91541a580877fd068bbb",
    },
}

CORPUS_ID = "pulse-1"
QUERY_CONTRACT_VERSION = "1.0"

# The surface is the audited set of originators that reach GraphTransactionScope.execute on
# the transactional port.  Statement content can find CANDIDATES -- and finding them by
# receiver name alone missed whole modules, because `conn` is a SQL connection in Community
# and a graph connection in these Core handlers -- but content does not define the surface.
# Provider internals, schema/runtime bootstrap, the SQL projection and the other ports belong
# to later milestones and are excluded here by name, not by guess.
PORT_ORIGINATORS: dict[str, tuple[str, ...]] = {
    "core": (
        "okto_pulse/core/application/kg_tick.py",
        "okto_pulse/core/events/handlers/cancellation_decay.py",
        "okto_pulse/core/events/handlers/card_boost_recompute.py",
        "okto_pulse/core/events/handlers/kg_decay_tick.py",
        "okto_pulse/core/kg/scoring.py",
        "okto_pulse/core/kg/canonical_learning_partition.py",
        "okto_pulse/core/kg/canonical_stale_reconciler.py",
        "okto_pulse/core/kg/dedup_migration.py",
        "okto_pulse/core/kg/governance.py",
        "okto_pulse/core/kg/primitives.py",
        "okto_pulse/core/kg/global_discovery/clustering.py",
        "okto_pulse/core/kg/kg_service.py",
    ),
    "community": ("okto_pulse/community/api/kg_routes.py",),
}

# Modules that DO send graph statements and are deliberately not in this corpus, each with
# the surface that owns it.  Recorded rather than merely absent: an exclusion nobody wrote
# down is indistinguishable from an omission, and the discovery sweep below turns any module
# in neither list into a freeze failure.
EXCLUDED_MODULES: dict[str, dict[str, str]] = {
    "community": {
        "okto_pulse/community/adapters/kg_runtime.py": "schema/runtime bootstrap (M-PULSE-3)",
        "okto_pulse/community/adapters/global_discovery_runtime.py": (
            "global-discovery port (M-PULSE-4)"
        ),
        "okto_pulse/community/adapters/global_discovery_schema.py": (
            "global-discovery schema (M-PULSE-4)"
        ),
        "okto_pulse/community/adapters/global_discovery_recovery.py": (
            "global-discovery recovery (M-PULSE-4)"
        ),
        "okto_pulse/community/adapters/kuzu_graph_store.py": "provider internals (Kuzu)",
        "okto_pulse/community/adapters/kuzu_graph_transaction.py": (
            "provider internals (Kuzu)"
        ),
        "okto_pulse/community/adapters/grafx_graph_transaction.py": (
            "provider internals (Grafx)"
        ),
        "okto_pulse/community/adapters/sqlalchemy_policy_constraint_projection.py": (
            "SQL projection surface (M-PULSE-6)"
        ),
    },
    "core": {
        "okto_pulse/core/application/processors/global_outbox.py": (
            "global-discovery outbox (M-PULSE-4)"
        ),
        "okto_pulse/core/kg/orphan_integrity.py": (
            "audited orphan: the write branch has no caller"
        ),
        "okto_pulse/core/kg/transaction.py": (
            "audited orphan: TransactionOrchestrator.execute_graph has no caller"
        ),
        "okto_pulse/core/kg/canonical_cognitive_preservation.py": (
            "audited orphan: _execute_write_has_row has no caller"
        ),
        "okto_pulse/core/kg/global_discovery/layer_parity.py": (
            "global-discovery parity (M-PULSE-4)"
        ),
        "okto_pulse/core/kg/health.py": "health surface (M-PULSE-3)",
    },
}

# Statement heads that make a call a graph candidate at all.  SQL is excluded by its own
# heads rather than by receiver, so a graph statement sent over a connection named `conn`
# is still seen.
CYPHER_HEADS: tuple[str, ...] = (
    "MATCH",
    "OPTIONAL",
    "CREATE",
    "MERGE",
    "DETACH",
    "UNWIND",
    "WITH",
    "RETURN",
    "CALL",
    "ALTER",
    "COPY",
    "SET",
    "DELETE",
)
SQL_HEADS: tuple[str, ...] = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE FROM",
    "PRAGMA",
    "ALTER TABLE",
    "CREATE TABLE",
    "CREATE INDEX",
    "DROP TABLE",
)

# Audited orphans: reachable in syntax, unreachable in the call graph.  Excluded by name so
# the exclusion is reviewable rather than an unexplained absence.
# Receivers that carry a command object rather than a statement.  `conn` is deliberately
# absent: it is a SQL connection across Community and a graph connection inside these audited
# handlers, which is exactly why the statement decides and the name only disposes.
# Receivers that reach a DIFFERENT port.  `global_runtime` carries global-discovery
# statements, which belong to their own milestone; counting them here would describe a
# surface this corpus does not claim.
OTHER_PORT_RECEIVERS: frozenset[str] = frozenset({"global_runtime", "native_scope"})

NON_STATEMENT_RECEIVERS: frozenset[str] = frozenset(
    {
        "context",
        "uow",
        "session",
        "self._session",
        "db",
        "self.db",
        "use_case",
        "issuer",
    }
)


ORPHAN_SYMBOLS: frozenset[str] = frozenset(
    {
        "_execute_write_has_row",
        "execute_graph",
    }
)


# Receivers whose ``.execute`` is a graph statement.  Named explicitly rather than inferred:
# Pulse also calls ``.execute`` on SQL connections, sessions and cursors, and a heuristic that
# confused the two would silently pull SQL into a Cypher corpus.
GRAPH_RECEIVERS: frozenset[str] = frozenset(
    {
        "scope",
        "native_scope",
        "graph_scope",
        "self.graph_scope",
        "self._transaction",
        "self._scope",
        "self._raw_scope",
    }
)
# Modules whose ``self.execute`` IS a graph scope: the providers implement the scope itself.
GRAPH_SELF_MODULES: tuple[str, ...] = (
    "community/adapters/kuzu_graph_transaction.py",
    "community/adapters/grafx_graph_transaction.py",
)
# Receivers known to be non-graph.  Listed so an unrecognised receiver is a freeze failure
# rather than a silent omission.
NON_GRAPH_RECEIVERS: frozenset[str] = frozenset(
    {
        "conn",
        "connection",
        "cursor",
        "db",
        "self.db",
        "session",
        "self._session",
        "context",
        "use_case",
        "sync_conn",
        "runtime",
        "verification",
        "facade",
        "executor",
        "gconn",
        "global_runtime",
        "self._conn",
        "issuer",
        "<call:_processor>",
        "<call:use_case_type>",
    }
)


def _is_use_case_receiver(receiver: str) -> bool:
    """Application use-cases also expose ``execute``, and it never takes Cypher.

    Matched by shape rather than by listing dozens of class names, because the list would go
    stale the moment somebody adds a use-case and the freeze would fail for no real reason.
    """

    return receiver.endswith(("UseCase", "UseCase>"))


# Provider methods whose statement IS a structured GraphTransactionScope primitive.  A form
# built here needs no generic dialect: a backend that implements the primitive implements the
# effect, which is exactly what "structured primitive" is meant to say.
STRUCTURED_PRIMITIVE_SYMBOLS: frozenset[str] = frozenset(
    {
        "create_node",
        "update_node",
        "create_edge",
        "edge_exists",
        "replace_node_payload",
        "replace_with_source_deleted_tombstone",
        "mark_superseded",
        "snapshot_node_properties",
        "restore_node_properties",
        "increment_attestation",
        "find_node_types",
        "delete_edges_by_session",
        "delete_edges_by_session_preserving_spec_lineage",
        "delete_nodes_by_session",
        "reconcile_spec_lineage_parent",
        "compensate_spec_lineage_parent",
        "clear_spec_lineage_parent",
        "reconcile_projection_active_set",
        "compensate_projection_active_set",
        "_restore_incident_edges",
        "_restore_node_before_image",
        "_snapshot_node_before_image",
        "_snapshot_incident_edges",
        "_delete_spec_lineage_edge",
        "_replace_node_payload_from_before_image",
        "_projection_owned_nodes",
        "_restore_projection_edges",
        "_delete_projection_incident_edges",
        "_reconcile_spec_dependency_edges",
    }
)

MUTATION_TOKENS = (
    "CREATE",
    "SET",
    "DELETE",
    "MERGE",
    "DROP",
    "ALTER",
    "COPY",
    "REMOVE",
)
CONTROL_STATEMENTS = frozenset({"BEGIN TRANSACTION", "COMMIT", "ROLLBACK"})

# Holes are marked with << >> rather than braces because Cypher's own inline property maps
# ARE braces.  Conflating the two made real syntax look like a hole, and the materialized
# template then failed to parse -- a gap reported for a reason that did not exist.
_HOLE_OPEN = "<<"
_HOLE_CLOSE = ">>"
_PLACEHOLDER = re.compile(r"<<([^<>]*)>>")


@dataclass
class Finding:
    """One statement Pulse hands to a graph, with everything needed to judge it."""

    entry_id: str
    baseline: str
    path: str
    line: int
    symbol: str
    surface: str
    receiver: str
    template_kind: str
    template: str
    placeholders: list[str] = field(default_factory=list)
    params: list[str] = field(default_factory=list)
    statement_class: str = "unknown"
    classification: str = "unclassified"
    parse_error: str | None = None
    acceptance_phase: str = "not_evaluated"


class FreezeError(RuntimeError):
    """The corpus could not be proven; freezing is refused rather than guessed."""


def _baseline_root(name: str) -> Path:
    """Locate a baseline by environment variable, then by portable sibling checkout.

    Absence is explicit: the caller decides whether that is a skip or an error, but it is
    never a silent pass -- a corpus verified against a baseline that was not there would be
    verified against nothing.
    """

    import os

    spec = BASELINES[name]
    configured = os.environ.get(spec["env"])
    if configured:
        return Path(configured)
    for sibling in spec["siblings"]:
        candidate = REPO_ROOT.parent / sibling
        if not (candidate / ".git").exists():
            continue
        try:
            head = _git(candidate, "rev-parse", "HEAD").strip()
        except Exception:  # noqa: BLE001 - an unreadable candidate is simply not the one
            continue
        # The first sibling that EXISTS is not necessarily the one this corpus describes;
        # several checkouts of the same repository sit side by side here.
        if head == spec["sha"]:
            return candidate
    message = (
        f"baseline {name!r} was not found. Set {spec['env']} to a checkout that contains "
        f"{spec['sha']}, or place one beside this repository as one of "
        f"{', '.join(spec['siblings'])}."
    )
    raise FreezeError(message)


def _git(root: Path, *args: str) -> str:
    # UTF-8 explicitly: the platform default is cp1252 here, and a file it cannot decode
    # comes back as None rather than as an error -- which would drop a whole module from the
    # corpus without anyone noticing.
    out = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        encoding="utf-8",
        errors="strict",
        check=True,
    )
    if not isinstance(out.stdout, str):
        message = f"git {' '.join(args)} in {root} produced no readable output"
        raise FreezeError(message)
    return out.stdout


def _resolved_sha(root: Path, revision: str) -> str:
    """Resolve the pin and require it to BE a full commit hash, not a prefix of one.

    A prefix match would let the corpus be rebuilt against a different commit that happens
    to share seven characters, and the corpus would change meaning without saying so.  The
    pin is the identity of the thing being described, so it is compared whole.
    """

    head = _git(root, "rev-parse", "HEAD").strip()
    if head != revision:
        message = (
            f"{root} is checked out at {head}, but the corpus is frozen against "
            f"{revision}. Content is read at the pin either way, so this is not about what "
            "was read -- it is so an auditor reproducing the freeze is looking at the same "
            "tree the corpus describes."
        )
        raise FreezeError(message)
    resolved = _git(root, "rev-parse", revision).strip()
    if resolved != revision:
        message = (
            f"baseline pin {revision!r} is not a full commit hash for {root}: it resolves "
            f"to {resolved!r}. The corpus describes one exact commit, so the pin must be "
            "that commit's complete hash."
        )
        raise FreezeError(message)
    return resolved


def _baseline_sources(root: Path, revision: str) -> dict[str, str]:
    """Read every ``src/**.py`` AT the pinned commit, not from the working tree.

    The baselines are other agents' worktrees and they move.  Reading the checkout would
    make the corpus describe whatever happened to be checked out when it was frozen, which
    is the opposite of a frozen corpus -- and it would silently change meaning rather than
    fail.  Reading blobs at the SHA makes regeneration reproducible from any clone.
    """

    # One archive process rather than one ``git show`` process per source file.  The old
    # implementation launched hundreds of child processes on every deterministic rebuild;
    # tests that intentionally rebuild the corpus then spent their timeout in process setup,
    # not in validation.  ``git archive`` reads the same immutable tree at the same SHA and
    # nothing is extracted to disk.
    archived = subprocess.run(
        ["git", "-C", str(root), "archive", "--format=tar", revision, "src/"],
        capture_output=True,
        check=True,
    )
    sources: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(archived.stdout), mode="r:") as archive:
        for member in archive.getmembers():
            path = member.name
            if (
                not member.isfile()
                or not path.startswith("src/")
                or not path.endswith(".py")
            ):
                continue
            extracted = archive.extractfile(member)
            if extracted is None:
                message = f"git archive exposed no bytes for {path} at {revision}"
                raise FreezeError(message)
            try:
                sources[path[len("src/") :]] = extracted.read().decode(
                    "utf-8", errors="strict"
                )
            except UnicodeDecodeError as exc:
                message = f"{path} at {revision} is not strict UTF-8"
                raise FreezeError(message) from exc
    if not sources:
        message = f"no src/**.py found at {revision} in {root}"
        raise FreezeError(message)
    return sources


def _receiver_name(node: ast.Call) -> str | None:
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != "execute":
        return None
    value = func.value
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
        return f"{value.value.id}.{value.attr}"
    if isinstance(value, ast.Attribute):
        return f"?.{value.attr}"
    if isinstance(value, ast.Call):
        # Name the callee, not merely "a call": `get_session().execute(...)` has to be
        # classifiable, and "<Call>" would lump every such receiver into one bucket.
        callee = value.func
        if isinstance(callee, ast.Name):
            return f"<call:{callee.id}>"
        if isinstance(callee, ast.Attribute):
            return f"<call:.{callee.attr}>"
        return "<call:?>"
    if isinstance(value, ast.Await):
        return "<await>"
    return f"<{type(value).__name__}>"


def _evaluated_hole(value: ast.FormattedValue, evaluator: Any) -> str | None:
    """Fill a hole that calls a pure clause builder with constant arguments."""

    if evaluator is None or not isinstance(value.value, ast.Call):
        return None
    callee = value.value.func
    # `tpl.build_clause(...)` is the same builder as `build_clause(...)`; Pulse imports the
    # template module under an alias in places, and matching only bare names left those
    # holes unresolved for a reason that is about spelling, not about provability.
    name = (
        callee.id
        if isinstance(callee, ast.Name)
        else callee.attr
        if isinstance(callee, ast.Attribute)
        else None
    )
    if name is None:
        return None
    arguments = [
        item.value for item in value.value.args if isinstance(item, ast.Constant)
    ]
    if len(arguments) != len(value.value.args):
        return None
    rendered = evaluator.call(name, arguments)
    return rendered if isinstance(rendered, str) else None


def _normalize_fstring(
    node: ast.JoinedStr,
    evaluator: Any = None,
) -> tuple[str, list[str]]:
    """Render an f-string as a template, filling the holes that can be proven.

    A hole calling a pure clause builder is not a hole in the query Pulse sends -- it is
    where the query gets its ``coalesce`` -- so it is evaluated rather than named.  Anything
    the evaluator cannot prove stays a named hole, so nothing is invented.
    """

    parts: list[str] = []
    holes: list[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
            continue
        if not isinstance(value, ast.FormattedValue):
            continue
        rendered = _evaluated_hole(value, evaluator)
        if rendered is not None:
            parts.append(rendered)
            continue
        source = ast.unparse(value.value)
        holes.append(source)
        parts.append(_HOLE_OPEN + source + _HOLE_CLOSE)
    return "".join(parts), holes


def _enclosing_symbol(tree: ast.AST, target: ast.AST) -> str:
    """Name the function (and class) the statement lives in, for auditable provenance."""

    best = "<module>"
    stack: list[tuple[ast.AST, str]] = [(tree, "")]
    while stack:
        node, prefix = stack.pop()
        for child in ast.iter_child_nodes(node):
            name = prefix
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}.{child.name}" if prefix else child.name
                start = getattr(child, "lineno", 0)
                end = getattr(child, "end_lineno", start)
                if start <= getattr(target, "lineno", -1) <= end and len(name) > len(
                    best
                ):
                    best = name
            stack.append((child, name))
    return best


def _local_assignments(tree: ast.AST, target: ast.AST) -> dict[str, list[ast.expr]]:
    """Every assignment to each name visible in the callsite's enclosing function.

    All of them, not one: a statement chosen in an if/else is two statements Pulse really
    sends, and a corpus that recorded only one branch would be describing a program that
    does not exist.  Augmented assignment is recorded as unprovable, because what it appends
    is not visible here.
    """

    key = (id(tree), getattr(target, "lineno", -1), getattr(target, "col_offset", -1))
    cached = _LOCAL_ASSIGNMENT_CACHE.get(key)
    if cached is not None:
        return cached[1]

    # EVERY enclosing function, not only the innermost: these handlers assign the clause in
    # the outer function and put the port call inside a nested coroutine that closes over it,
    # so looking only at the inner scope found nothing at all.
    holders = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.lineno
        <= getattr(target, "lineno", -1)
        <= getattr(node, "end_lineno", node.lineno)
    ]
    if not holders:
        _LOCAL_ASSIGNMENT_CACHE[key] = (tree, {})
        return {}
    values: dict[str, list[ast.expr]] = {}
    for holder in sorted(holders, key=lambda item: item.lineno):
        _collect_assignments(holder, values)
    _LOCAL_ASSIGNMENT_CACHE[key] = (tree, values)
    return values


def _collect_assignments(holder: ast.AST, values: dict[str, list[ast.expr]]) -> None:
    for node in ast.walk(holder):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            name_node = node.targets[0]
            if isinstance(name_node, ast.Name):
                values.setdefault(name_node.id, []).append(node.value)
            elif isinstance(name_node, ast.Tuple):
                # `clause, params = builder(...)`: the clause is the half that reaches the
                # query, and refusing the whole statement because the other half is a dict
                # would leave a hole where a provable clause lives.
                for position, element in enumerate(name_node.elts):
                    if not isinstance(element, ast.Name):
                        continue
                    values.setdefault(element.id, []).append(
                        ast.Subscript(
                            value=node.value,
                            slice=ast.Constant(value=position),
                            ctx=ast.Load(),
                        )
                    )
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            values.setdefault(node.target.id, []).append(node)


def _parameter_names(tree: ast.AST, target: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        start = node.lineno
        end = getattr(node, "end_lineno", start)
        if start <= getattr(target, "lineno", -1) <= end:
            args = node.args
            for group in (args.posonlyargs, args.args, args.kwonlyargs):
                names.update(argument.arg for argument in group)
            if args.vararg:
                names.add(args.vararg.arg)
            if args.kwarg:
                names.add(args.kwarg.arg)
    return names


_MAX_ALTERNATIVES = 12


def _expand(
    argument: ast.expr,
    tree: ast.AST,
    call: ast.Call,
    depth: int = 0,
) -> list[tuple[str, str, list[str]]]:
    """Every concrete form this expression can take, or a single unprovable marker.

    Returns a list because one callsite may send more than one statement.  Depth is bounded
    so a cyclic or deeply chained assignment ends as unprovable rather than as a hang.
    """

    if depth > 4:
        return [("unresolved", "<depth-exceeded>", [])]
    if _MODULE_EVALUATOR is not None and not isinstance(
        argument, (ast.Constant, ast.JoinedStr, ast.Name, ast.IfExp)
    ):
        # A join over a closed constant is provable, just not by pattern-matching AST shapes.
        # The locals it reads have to travel with it: evaluating with an empty scope asks the
        # evaluator about names it was never told, and gets "unknown" for a value that is
        # perfectly determined a few lines up.
        rendered = _MODULE_EVALUATOR._value(
            argument, _resolvable_locals(tree, call, depth)
        )
        if isinstance(rendered, str) and rendered:
            return [("evaluated", rendered, [])]
    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
        return [("literal", argument.value, [])]
    if isinstance(argument, ast.JoinedStr):
        return _expand_fstring(argument, tree, call, depth)
    if isinstance(argument, ast.IfExp):
        # A ternary is a closed choice, exactly like an if/else assignment: both arms are
        # statements the code really sends, and picking one would describe half the program.
        return _dedupe(
            [
                *_expand(argument.body, tree, call, depth + 1),
                *_expand(argument.orelse, tree, call, depth + 1),
            ]
        )
    if isinstance(argument, ast.Name):
        if argument.id in _parameter_names(tree, call):
            return [("parameter", f"<parameter:{argument.id}>", [])]
        assignments = _local_assignments(tree, call).get(argument.id)
        if not assignments:
            return [("unresolved", f"<name:{argument.id}>", [])]
        expanded: list[tuple[str, str, list[str]]] = []
        for assigned in assignments:
            if isinstance(assigned, ast.AugAssign):
                return [("unresolved", f"<augmented:{argument.id}>", [])]
            for kind, template, holes in _expand(assigned, tree, call, depth + 1):
                if kind in {"parameter", "unresolved"}:
                    return [(kind, template, holes)]
                expanded.append((f"local:{kind}", template, holes))
        return _dedupe(expanded)
    return [("unresolved", f"<{type(argument).__name__}>", [])]


# Set for the duration of one baseline scan.  The internal callsites interpolate the same
# pure clause builders the public templates do -- that is where their `coalesce` comes from --
# so resolving them only on the public side would report the internal families as supported
# for a reason that is not true of the query Pulse sends.
_CLAUSE_EVALUATOR: Any = None
# Bound to the module being scanned, so a local built from that module's own constants can
# be evaluated rather than left as a hole.
_MODULE_EVALUATOR: Any = None

# Parsing is the expensive part of this freezer, and both of these were paying for it once
# per OCCURRENCE of a name rather than once per name.  Keyed by identity of the sources dict,
# so a second baseline never reads the first one's answers.
_EVALUATOR_CACHE: dict[tuple[int, str], Any] = {}
_RESOLVABLE_CACHE: dict[tuple[int, int, int], Any] = {}
_LOCAL_ASSIGNMENT_CACHE: dict[tuple[int, int, int], Any] = {}


def _reset_caches() -> None:
    """Start every freeze from nothing, so a run can never read a previous run's answers."""

    _EVALUATOR_CACHE.clear()
    _RESOLVABLE_CACHE.clear()
    _LOCAL_ASSIGNMENT_CACHE.clear()


def _resolvable_locals(
    tree: ast.AST,
    call: ast.AST,
    depth: int,
) -> dict[str, Any]:
    """Locals around the callsite whose value this freezer can actually determine.

    Only the ones with a single assignment that evaluates: a name written twice, or written
    from something unprovable, is left out so the evaluator refuses instead of guessing.
    """

    if _MODULE_EVALUATOR is None or depth > 3:
        return {}
    key = (id(tree), getattr(call, "lineno", -1), getattr(call, "col_offset", -1))
    cached = _RESOLVABLE_CACHE.get(key)
    if cached is not None:
        return cached[1]
    resolved: dict[str, Any] = {}
    for name, assignments in _local_assignments(tree, call).items():
        if len(assignments) != 1:
            continue
        value = _MODULE_EVALUATOR._value(assignments[0], resolved)
        if value is not None:
            resolved[name] = value
    # The tree travels with the answer for the same reason the sources dict does above.
    _RESOLVABLE_CACHE[key] = (tree, resolved)
    return resolved


def _expand_fstring(
    node: ast.JoinedStr,
    tree: ast.AST,
    call: ast.Call,
    depth: int,
) -> list[tuple[str, str, list[str]]]:
    """Render an f-string, expanding holes that are themselves provable local names.

    A hole is not always an identifier.  Some carry a whole clause, so leaving them as a
    placeholder would produce a template that is not Cypher at all and would be judged a gap
    for the wrong reason.  Where the hole resolves to literal alternatives it is expanded;
    where it does not, it stays a named placeholder.
    """

    variants: list[tuple[list[str], list[str]]] = [([], [])]
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            variants = [(parts + [value.value], holes) for parts, holes in variants]
            continue
        if not isinstance(value, ast.FormattedValue):
            continue
        rendered = _evaluated_hole(value, _CLAUSE_EVALUATOR)
        if rendered is not None:
            variants = [(parts + [rendered], holes) for parts, holes in variants]
            continue
        source = ast.unparse(value.value)
        options: list[str] | None = None
        if isinstance(value.value, ast.Name) and depth <= 3:
            resolved = _expand(value.value, tree, call, depth + 1)
            if resolved and any(kind == "parameter" for kind, _t, _h in resolved):
                # The hole is this function's parameter, so what fills it is decided by the
                # callers.  Following them here is the same hop the statement itself takes;
                # not following it left a named hole where a provable clause lives.
                forwarded: list[tuple[str, str, list[str]]] = []
                for supplied, _line in _forwarded_arguments(tree, call, value.value.id):
                    forwarded.extend(_expand(supplied, tree, supplied, depth + 1))
                if forwarded and all(
                    kind not in {"parameter", "unresolved"}
                    for kind, _t, _h in forwarded
                ):
                    resolved = _dedupe(forwarded)
            # A hole often carries a CLAUSE, not an identifier -- an optional WHERE, a
            # visibility filter, a self-loop exclusion.  Expanding those to their concrete
            # alternatives is the difference between describing the statements Pulse sends
            # and describing a shape that was never sent.
            # Expanded even when the filling itself still carries a hole: the closed choice
            # is the CT predicate being on or off, or a scoring SET being the normal or the
            # cancelled shape, and those are different statements whatever else remains
            # parametrized inside them.
            if resolved and all(
                kind not in {"parameter", "unresolved"} for kind, _t, _h in resolved
            ):
                options = [template for _k, template, _h in resolved]
        if options is not None and len(variants) * len(options) <= _MAX_ALTERNATIVES:
            variants = [
                (parts + [option], holes)
                for parts, holes in variants
                for option in options
            ]
            continue
        variants = [
            (parts + [_HOLE_OPEN + source + _HOLE_CLOSE], holes + [source])
            for parts, holes in variants
        ]
    return _dedupe([("fstring", "".join(parts), holes) for parts, holes in variants])


def _dedupe(
    items: list[tuple[str, str, list[str]]],
) -> list[tuple[str, str, list[str]]]:
    seen: set[str] = set()
    unique: list[tuple[str, str, list[str]]] = []
    for kind, template, holes in items:
        if template in seen:
            continue
        seen.add(template)
        unique.append((kind, template, holes))
    return unique


def _param_names(call: ast.Call) -> list[str]:
    """The $-parameters the callsite supplies, when they are a literal mapping."""

    if len(call.args) < 2:
        return []
    mapping = call.args[1]
    if not isinstance(mapping, ast.Dict):
        return []
    names: list[str] = []
    for key in mapping.keys:
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            names.append(key.value)
    return sorted(names)


def _classify_statement_class(template: str) -> str:
    stripped = template.strip().upper()
    if stripped in CONTROL_STATEMENTS:
        return "control"
    for token in MUTATION_TOKENS:
        if re.search(rf"\b{token}\b", stripped):
            return "mutation"
    return "read"


# One representative member per closed domain, used when a hole has to be filled so the
# engine can be asked about the query.  Substituting a real label is what lets the PLANNER
# answer at all: it resolves labels against the catalog, so an invented identifier would be
# refused for a reason that belongs to this file rather than to the engine.
# One triple that the catalog really carries, checked by a sentinel test before any verdict
# is read: `supports` starts at Entity, so pairing it with a Decision label produced a
# plan_error that belonged to this file rather than to the engine.
_REPRESENTATIVE_LABEL = "Decision"
_REPRESENTATIVE_RELATIONSHIP = "supersedes"
_DOMAIN_REPRESENTATIVES: dict[str, str] = {
    "node_label": _REPRESENTATIVE_LABEL,
    "relationship_type": _REPRESENTATIVE_RELATIONSHIP,
}


def _representative(source: str) -> str | None:
    return _DOMAIN_REPRESENTATIVES.get(_domain_of(source) or "")


def _materialize(template: str, *, style: str) -> str:
    """Give every hole a concrete filling so the parser judges the query, not the hole.

    Two fillings, because a hole is not one thing.  Some sit where an identifier goes -- a
    label, a relationship type -- and some carry an optional clause.  Filling a clause hole
    with an identifier produces text that was never sent, and the parse error that follows
    says nothing about the engine.
    """

    counter = {"n": 0}

    def _fill(match: re.Match[str]) -> str:
        representative = _representative(match.group(1))
        if representative is not None:
            return representative
        counter["n"] += 1
        return f"Placeholder{counter['n']}" if style == "identifier" else ""

    return _PLACEHOLDER.sub(_fill, template)


# Node properties the corpus actually references, plus the vector payload.  A closed shape:
# planning has to resolve a property to answer at all, so a schema that omitted one would
# report a gap that belongs to this file rather than to the engine.
_PULSE_NODE_PROPERTIES: tuple[str, ...] = (
    "source_session_id STRING",
    "title STRING",
    "content STRING",
    "context STRING",
    "justification STRING",
    "kind_of STRING",
    "revocation_reason STRING",
    "source_artifact_ref STRING",
    "source_content_hash STRING",
    "source_span_quote STRING",
    "created_by_agent STRING",
    "graph_layer STRING",
    "maturity_status STRING",
    "superseded_by STRING",
    "superseded_at TIMESTAMP",
    "created_at TIMESTAMP",
    "last_attested_at TIMESTAMP",
    "last_queried_at TIMESTAMP",
    "last_recomputed_at TIMESTAMP",
    "source_confidence DOUBLE",
    "relevance_score DOUBLE",
    "pre_cancellation_relevance_score DOUBLE",
    "priority_boost DOUBLE",
    "query_hits INT64",
    "attestation_count INT64",
    "generation INT64",
    "human_curated BOOL",
    "embedding VECTOR(pulse_corpus)",
)
_PULSE_EDGE_PROPERTIES: tuple[str, ...] = (
    "confidence DOUBLE",
    "created_by_session_id STRING",
    "created_at TIMESTAMP",
    "layer STRING",
    "rule_id STRING",
    "created_by STRING",
    "fallback_reason STRING",
    "note STRING",
)

_PULSE_CATALOG: Any = None

# Column types by name, so the schema below reads as the Pulse shape rather than as engine
# plumbing.  Only the kinds Pulse actually declares appear here.
_COLUMN_TYPES: dict[str, str] = {
    "id": "STRING",
    "source_session_id": "STRING",
    "title": "STRING",
    "content": "STRING",
    "context": "STRING",
    "justification": "STRING",
    "kind_of": "STRING",
    "revocation_reason": "STRING",
    "source_artifact_ref": "STRING",
    "source_content_hash": "STRING",
    "source_span_quote": "STRING",
    "created_by_agent": "STRING",
    "graph_layer": "STRING",
    "maturity_status": "STRING",
    "superseded_by": "STRING",
    "superseded_at": "TIMESTAMP",
    "created_at": "TIMESTAMP",
    "last_attested_at": "TIMESTAMP",
    "last_queried_at": "TIMESTAMP",
    "last_recomputed_at": "TIMESTAMP",
    "source_confidence": "DOUBLE",
    "relevance_score": "DOUBLE",
    "pre_cancellation_relevance_score": "DOUBLE",
    "priority_boost": "DOUBLE",
    "query_hits": "INT64",
    "attestation_count": "INT64",
    "generation": "INT64",
    "human_curated": "BOOL",
}
_EDGE_COLUMN_TYPES: dict[str, str] = {
    "confidence": "DOUBLE",
    "created_by_session_id": "STRING",
    "created_at": "TIMESTAMP",
    "layer": "STRING",
    "rule_id": "STRING",
    "created_by": "STRING",
    "fallback_reason": "STRING",
    "note": "STRING",
}
_VECTOR_SPACE = "pulse_corpus"


def _relationship_domain(sources: dict[str, str]) -> tuple[list[str], list[list[str]]]:
    """The 16 logical relationship names and their 69 distinct endpoint pairs.

    Read whole from the pinned schema, and de-duplicated across the single-pair and
    multi-pair declarations, because `supersedes(Decision, Decision)` is declared in both.
    """

    contract = _PureEvaluator(sources, "okto_pulse/core/kg/schema_contract.py")
    single = contract._value(contract._constants.get("REL_TYPES"), {}) or []
    multi = contract._value(contract._constants.get("MULTI_REL_TYPES"), {}) or []
    triples: list[list[str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in single:
        triple = (str(item[0]), str(item[1]), str(item[2]))
        if triple in seen:
            continue
        seen.add(triple)
        triples.append(list(triple))
    for name, pairs in multi:
        for endpoints in pairs:
            triple = (str(name), str(endpoints[0]), str(endpoints[1]))
            if triple in seen:
                continue
            seen.add(triple)
            triples.append(list(triple))
    names = sorted({triple[0] for triple in triples})
    if len(names) != 16 or len(triples) != 69:
        message = (
            f"the pinned schema declares {len(names)} relationship names over "
            f"{len(triples)} endpoint pairs; the corpus is frozen against 16 and 69. A "
            "different schema is a different corpus."
        )
        raise FreezeError(message)
    return names, triples


def _pulse_catalog(sources: dict[str, str]) -> Any:
    """A minimal closed Pulse schema, built once in memory, so queries can be planned.

    Parsing says the text is well formed and analysis says the vocabulary is known; neither
    says this engine can answer the query.  ``MATCH (n)`` and an untyped relationship parse
    and analyse cleanly and are refused by the PLANNER, so a corpus that stopped earlier
    called the two broadest public reads supported.

    One relationship table per LOGICAL type, on its first declared endpoint pair: the plan
    verdict is about whether the construct can be planned at all, not about which endpoint
    pairs a particular board carries.
    """

    global _PULSE_CATALOG
    if _PULSE_CATALOG is not None:
        return _PULSE_CATALOG

    sys.path.insert(0, str(REPO_ROOT / "src"))
    from okto_grafx.domain.model.catalog import (
        Catalog,  # noqa: PLC0415 - local by design
    )
    from okto_grafx.domain.model.schema import (  # noqa: PLC0415 - same
        ColumnDef,
        EmbeddingSpaceDef,
        TableDef,
    )
    from okto_grafx.domain.model.value import ValueType  # noqa: PLC0415 - same
    from okto_grafx.domain.ports.vectormath import (
        DistanceMetric,  # noqa: PLC0415 - same
    )

    labels = _node_types(sources)
    names, triples = _relationship_domain(sources)
    pairs: list[tuple[str, str, str]] = []
    taken: set[str] = set()
    for name, from_type, to_type in triples:
        if name in taken or from_type not in labels or to_type not in labels:
            continue
        taken.add(name)
        pairs.append((name, from_type, to_type))
    if len(labels) != 11 or len(pairs) != 16:
        message = (
            f"the pinned schema yields {len(labels)} labels and {len(pairs)} relationship "
            "tables; the corpus is frozen against 11 and 16."
        )
        raise FreezeError(message)

    catalog = Catalog()
    catalog.add_space(
        EmbeddingSpaceDef(
            space_id=1,
            name=_VECTOR_SPACE,
            dimension=4,
            metric=DistanceMetric.COSINE,
            normalized=True,
        )
    )
    table_id = 0
    for label in labels:
        table_id += 1
        columns = [
            ColumnDef(
                name=name,
                type=getattr(ValueType, kind),
                nullable=name != "id",
            )
            for name, kind in _COLUMN_TYPES.items()
        ]
        columns.append(
            ColumnDef(
                name="embedding",
                type=ValueType.VECTOR_F32,
                vector_space=_VECTOR_SPACE,
            )
        )
        catalog.add_table(
            TableDef(
                table_id=table_id,
                name=label,
                kind="node",
                columns=tuple(columns),
                primary_key="id",
            )
        )
    for edge_type, from_type, to_type in pairs:
        table_id += 1
        catalog.add_table(
            TableDef(
                table_id=table_id,
                name=edge_type,
                kind="rel",
                columns=tuple(
                    ColumnDef(name=name, type=getattr(ValueType, kind))
                    for name, kind in _EDGE_COLUMN_TYPES.items()
                ),
                from_table=from_type,
                to_table=to_type,
            )
        )
    _PULSE_CATALOG = catalog
    return _PULSE_CATALOG


def _try_accept(
    text: str, sources: dict[str, str] | None = None
) -> tuple[str, str | None]:
    """Ask the engine every question it can answer, and stop at the first refusal.

    Three phases, because each answers something different: the text is well formed, its
    vocabulary is known, and this engine can actually plan it.  ``already_supported`` means
    the last provable phase succeeded -- anything less would call a query supported on the
    strength of its AST.
    """

    sys.path.insert(0, str(REPO_ROOT / "src"))
    from okto_grafx.domain.query.analysis import (
        analyze,  # noqa: PLC0415 - local by design
    )
    from okto_grafx.domain.query.parser import parse  # noqa: PLC0415 - same
    from okto_grafx.domain.query.planner import build_plan  # noqa: PLC0415 - same

    try:
        statement = parse(text)
    except Exception as exc:  # noqa: BLE001 - any refusal is evidence, whatever its shape
        return "parse_error", f"{type(exc).__name__}: {exc}"[:220]
    try:
        analysis = analyze(statement)
    except Exception as exc:  # noqa: BLE001 - same
        return "analysis_error", f"{type(exc).__name__}: {exc}"[:220]
    if sources is None:
        return "analysed_only", None
    try:
        catalog = _pulse_catalog(sources)
    except Exception as exc:  # noqa: BLE001 - a catalog we cannot build proves nothing
        return "plan_unproven", f"{type(exc).__name__}: {exc}"[:220]
    try:
        build_plan(statement, catalog=catalog, analysis=analysis)
    except Exception as exc:  # noqa: BLE001 - same
        return "plan_error", f"{type(exc).__name__}: {exc}"[:220]
    return "planned", None


def _classify(finding: Finding, sources: dict[str, str] | None = None) -> None:
    if finding.statement_class == "control":
        finding.classification = "structured_primitive"
        return
    leaf = finding.symbol.rsplit(".", 1)[-1]
    if leaf in STRUCTURED_PRIMITIVE_SYMBOLS:
        # Judged by what builds it, not by whether the engine happens to accept the shape:
        # the effect already has a structured route, so no dialect is owed.
        finding.classification = "structured_primitive"
        return
    phase_identifier, as_identifier = _try_accept(
        _materialize(finding.template, style="identifier"), sources
    )
    if as_identifier is None:
        finding.acceptance_phase = phase_identifier
        finding.classification = "already_supported"
        return
    phase_empty, as_empty = _try_accept(
        _materialize(finding.template, style="empty"), sources
    )
    if as_empty is None:
        finding.acceptance_phase = phase_empty
        finding.classification = "already_supported"
        return
    finding.acceptance_phase = phase_identifier
    if finding.placeholders and (
        "Placeholder" in as_identifier or "Placeholder" in as_empty
    ):
        # The refusal is about a hole this freezer could not fill faithfully, so it is not
        # evidence about the engine.  Saying "gap" here would put work on a milestone that
        # may not need it; saying "supported" would hide work that may.  Neither is known.
        finding.parse_error = as_identifier
        finding.classification = "runtime_fragment"
        return
    finding.parse_error = as_identifier
    finding.classification = "generic_gap"


def _enclosing_function(tree: ast.AST, target: ast.AST) -> ast.AST | None:
    best: ast.AST | None = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        start_line = node.lineno
        end_line = getattr(node, "end_lineno", start_line)
        if start_line <= getattr(target, "lineno", -1) <= end_line:
            if best is None or node.lineno > best.lineno:
                best = node
    return best


def _forwarded_arguments(
    tree: ast.AST,
    call: ast.Call,
    parameter: str,
) -> list[tuple[ast.expr, int]]:
    """What callers pass into a wrapper for the parameter it forwards.

    Some originators do not call the port directly; they hand a statement to a small active
    wrapper that adds a lease or a thread hop.  The statement is still theirs, so following
    one hop back to the callers is what keeps the corpus about the code that decides the
    query rather than about the plumbing that carries it.
    """

    holder: ast.AST | None = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        start_line = node.lineno
        end_line = getattr(node, "end_lineno", start_line)
        if not start_line <= getattr(call, "lineno", -1) <= end_line:
            continue
        declared = [
            argument.arg
            for argument in (
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            )
        ]
        if parameter in declared:
            holder = node
            break
    if holder is None:
        return []
    names = {holder.name}
    index: int | None = None
    args = holder.args
    ordered = [argument.arg for argument in (*args.posonlyargs, *args.args)]
    if parameter in ordered:
        index = ordered.index(parameter)
    found: list[tuple[ast.expr, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        callee_name = (
            callee.id
            if isinstance(callee, ast.Name)
            else callee.attr
            if isinstance(callee, ast.Attribute)
            else None
        )
        if callee_name not in names or node is call:
            continue
        for keyword in node.keywords:
            if keyword.arg == parameter:
                found.append((keyword.value, node.lineno))
        if index is not None and len(node.args) > index:
            found.append((node.args[index], node.lineno))
    return found


def _is_graph_head(template: str) -> bool:
    head = template.strip().upper()
    if head.startswith(SQL_HEADS):
        return False
    return head.startswith(CYPHER_HEADS)


def _discovery_prefixes(
    argument: ast.expr,
    tree: ast.AST,
    call: ast.Call,
    depth: int = 0,
) -> list[str]:
    """Resolve only enough leading text to classify a discovery candidate.

    The full inventory resolver follows wrapper arguments and evaluates clause builders;
    that precision is required to freeze exact query families.  Discovery needs only the
    statement head.  Reusing the full resolver here made every test rebuild recursively walk
    whole modules for each callsite.  This bounded resolver follows local literals,
    f-strings, concatenation and branches without doing the inventory's work twice.
    """

    if depth > 4:
        return []
    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
        return [argument.value]
    if isinstance(argument, ast.Name):
        assignments = _local_assignments(tree, call).get(argument.id, [])
        prefixes = [
            prefix
            for assigned in assignments
            if not isinstance(assigned, ast.AugAssign)
            for prefix in _discovery_prefixes(assigned, tree, call, depth + 1)
        ]
        return list(dict.fromkeys(prefixes))[:_MAX_ALTERNATIVES]
    if isinstance(argument, ast.IfExp):
        prefixes = [
            *_discovery_prefixes(argument.body, tree, call, depth + 1),
            *_discovery_prefixes(argument.orelse, tree, call, depth + 1),
        ]
        return list(dict.fromkeys(prefixes))[:_MAX_ALTERNATIVES]
    if isinstance(argument, ast.BinOp) and isinstance(argument.op, ast.Add):
        left = _discovery_prefixes(argument.left, tree, call, depth + 1)
        if not left or any(_is_graph_head(prefix) for prefix in left):
            return left
        right = _discovery_prefixes(argument.right, tree, call, depth + 1)
        if not right:
            return left
        return list(dict.fromkeys(a + b for a in left for b in right))[
            :_MAX_ALTERNATIVES
        ]
    if isinstance(argument, ast.JoinedStr):
        variants = [""]
        for value in argument.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                variants = [prefix + value.value for prefix in variants]
                continue
            if not isinstance(value, ast.FormattedValue):
                continue
            options = _discovery_prefixes(value.value, tree, call, depth + 1)
            if not options:
                # Leading text already proves the head; later dynamic content cannot change
                # it. An unknown leading hole, however, cannot safely be skipped.
                return (
                    variants
                    if any(_is_graph_head(prefix) for prefix in variants)
                    else []
                )
            variants = [prefix + option for prefix in variants for option in options][
                :_MAX_ALTERNATIVES
            ]
        return list(dict.fromkeys(variants))
    if (
        isinstance(argument, ast.Call)
        and isinstance(argument.func, ast.Attribute)
        and argument.func.attr == "format"
    ):
        return _discovery_prefixes(argument.func.value, tree, call, depth + 1)
    if isinstance(argument, ast.NamedExpr):
        return _discovery_prefixes(argument.value, tree, call, depth + 1)
    return []


def _discover_port_candidates(name: str, sources: dict[str, str]) -> set[str]:
    """Modules that send graph statements over a scope-like receiver, found independently.

    The inventory is a list, and a list cannot notice what was added after it was written.
    This sweep is deliberately NOT the inventory: it looks for the shape -- a graph statement
    handed to a scope-like receiver -- so a new originator surfaces as a difference instead
    of as a silence.
    """

    del name  # the sweep is deliberately independent of the audited inventory
    candidates: set[str] = set()
    for relative, source in sources.items():
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            receiver = _receiver_name(node)
            if receiver is None or receiver in OTHER_PORT_RECEIVERS:
                continue
            if _is_use_case_receiver(receiver) or receiver in NON_STATEMENT_RECEIVERS:
                continue
            prefixes = _discovery_prefixes(node.args[0], tree, node)
            explicit_graph_receiver = receiver in GRAPH_RECEIVERS or (
                receiver == "self"
                and any(relative.endswith(suffix) for suffix in GRAPH_SELF_MODULES)
            )
            if any(_is_graph_head(prefix) for prefix in prefixes) or (
                not prefixes and explicit_graph_receiver
            ):
                candidates.add(relative)
    return candidates


def _check_inventory_completeness(
    name: str,
    sources: dict[str, str],
) -> list[str]:
    """Every discovered candidate is either inventoried or excluded on the record."""

    discovered = _discover_port_candidates(name, sources)
    inventoried = set(PORT_ORIGINATORS.get(name, ()))
    excluded = EXCLUDED_MODULES.get(name, {})
    unaccounted = sorted(
        candidate
        for candidate in discovered
        if candidate not in inventoried and candidate not in excluded
    )
    if unaccounted:
        message = (
            f"baseline {name!r} contains modules that send graph statements and appear in "
            f"neither the audited inventory nor the recorded exclusions: {unaccounted}. "
            "Add each to PORT_ORIGINATORS or to EXCLUDED_MODULES with the milestone that "
            "owns it."
        )
        raise FreezeError(message)
    return sorted(discovered)


def check_inventory_completeness(
    name: str,
    sources: dict[str, str],
) -> list[str]:
    """Public test seam for proving that a synthetic new originator fails closed."""

    return _check_inventory_completeness(name, sources)


def _scan_baseline(
    name: str,
    sources: dict[str, str],
    allowlist: dict[str, Any],
    core_sources: dict[str, str] | None = None,
) -> list[Finding]:
    """Freeze the statements the audited originators send to the transactional port."""

    core_sources = core_sources if core_sources is not None else sources
    findings: list[Finding] = []
    for relative in PORT_ORIGINATORS.get(name, ()):
        if relative not in sources:
            message = (
                f"audited originator {relative!r} is missing from baseline {name!r}"
            )
            raise FreezeError(message)
        tree = ast.parse(sources[relative])
        global _MODULE_EVALUATOR
        _MODULE_EVALUATOR = _PureEvaluator(sources, relative)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _receiver_name(node) is None or not node.args:
                continue
            symbol = _enclosing_symbol(tree, node)
            base_id = f"internal:{name}:{relative}:{node.lineno}"
            if symbol.rsplit(".", 1)[-1] in ORPHAN_SYMBOLS:
                continue
            receiver = _receiver_name(node) or "?"
            if receiver in OTHER_PORT_RECEIVERS:
                continue
            alternatives = _expand(node.args[0], tree, node)
            unprovable = [
                (kind, template)
                for kind, template, _holes in alternatives
                if kind in {"parameter", "unresolved"}
            ]
            if unprovable and (
                _is_use_case_receiver(receiver) or receiver in NON_STATEMENT_RECEIVERS
            ):
                # Not a statement at all: an application use-case and a unit of work also
                # expose ``execute``, and what they take is a command object.  Skipped by
                # receiver only when the argument could not be read, so a real statement is
                # never dismissed on the strength of a name.
                continue
            if unprovable and symbol.rsplit(".", 1)[-1] == "execute":
                # A decorating scope: a method called ``execute`` that adds a lease or a
                # thread hop and forwards the caller's statement unchanged.  Its forms are
                # already frozen at the originators that pass through it, so counting it
                # again would duplicate families rather than describe new ones.
                continue
            if unprovable:
                parameter = unprovable[0][1].partition(":")[2].rstrip(">")
                forwarded: list[tuple[str, str, list[str], int]] = []
                for supplied, caller_line in _forwarded_arguments(
                    tree, node, parameter
                ):
                    for item in _expand(supplied, tree, supplied):
                        forwarded.append((*item, caller_line))
                provable = [
                    item
                    for item in forwarded
                    if item[0] not in {"parameter", "unresolved"}
                ]
                if not provable:
                    reason = allowlist.get("dynamic_statements", {}).get(base_id)
                    if not reason:
                        message = (
                            f"{base_id} forwards a statement the freezer cannot follow "
                            f"({unprovable[0][1]}). Resolve its callers or add it to "
                            f"{ALLOWLIST_PATH.name} with a reason."
                        )
                        raise FreezeError(message)
                    continue
                # Attributed to the caller that built the statement, not to the wrapper that
                # carried it: the wrapper is one line and the decisions are many, and a
                # corpus that pointed at the wrapper would name the plumbing as the author.
                for kind, template, holes, caller_line in provable:
                    if not _is_graph_head(template):
                        continue
                    findings.append(
                        Finding(
                            entry_id=f"internal:{name}:{relative}:{caller_line}",
                            baseline=name,
                            path=relative,
                            line=caller_line,
                            symbol=_enclosing_symbol(tree, node),
                            surface="internal_execute",
                            receiver=receiver,
                            template_kind=kind,
                            template=template,
                            placeholders=holes,
                            params=_param_names(node),
                            statement_class=_classify_statement_class(template),
                        )
                    )
                    _classify(findings[-1], core_sources)
                continue
            graph_alternatives = [
                item for item in alternatives if _is_graph_head(item[1])
            ]
            if not graph_alternatives:
                continue
            params = _param_names(node)
            multiple = len(graph_alternatives) > 1
            for position, (kind, template, holes) in enumerate(graph_alternatives):
                finding = Finding(
                    entry_id=f"{base_id}#{position}" if multiple else base_id,
                    baseline=name,
                    path=relative,
                    line=node.lineno,
                    symbol=symbol,
                    surface="internal_execute",
                    receiver=receiver,
                    template_kind=kind,
                    template=template,
                    placeholders=holes,
                    params=params,
                )
                finding.statement_class = _classify_statement_class(template)
                _classify(finding, core_sources)
                findings.append(finding)
    return findings


NODE_TYPES_MODULE = "okto_pulse/core/domain/kg_ontology.py"
TEMPLATE_GENERATORS: tuple[str, ...] = ("supersedence_chain_template",)


def _node_types(sources: dict[str, str]) -> tuple[str, ...]:
    """Read the label allowlist at the pinned commit, never by importing Pulse."""

    tree = ast.parse(sources[NODE_TYPES_MODULE])
    for node in tree.body:
        if not isinstance(node, ast.AnnAssign | ast.Assign):
            continue
        target = node.target if isinstance(node, ast.AnnAssign) else node.targets[0]
        if not isinstance(target, ast.Name) or target.id != "NODE_TYPES":
            continue
        value = node.value
        if isinstance(value, ast.Tuple):
            return tuple(
                element.value
                for element in value.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            )
    message = f"NODE_TYPES not found in {NODE_TYPES_MODULE}"
    raise FreezeError(message)


def _generated_templates(
    tree: ast.AST,
    sources: dict[str, str],
    constants: dict[str, str],
    evaluator: Any,
) -> list[tuple[str, str, str, int]]:
    """Expand each label-parametrized generator over the whole label allowlist.

    A generator is a public template too.  Leaving it out described the contract as sixteen
    fixed texts when it really exposes one per label, and the label set is closed and known,
    so there is nothing to guess.  A branch that returns a named constant is recorded as the
    duplicate it is rather than as a new form.
    """

    labels = _node_types(sources)
    generated: list[tuple[str, str, str, int]] = []
    for node in tree.body:
        if (
            not isinstance(node, ast.FunctionDef)
            or node.name not in TEMPLATE_GENERATORS
        ):
            continue
        returns = [item for item in ast.walk(node) if isinstance(item, ast.Return)]
        for label in labels:
            text: str | None = None
            duplicate_of: str | None = None
            for statement in returns:
                value = statement.value
                if isinstance(value, ast.Name) and value.id in constants:
                    # The Decision branch hands back the shared named template.
                    if label == "Decision":
                        text = constants[value.id]
                        duplicate_of = value.id
                        break
                    continue
                if isinstance(value, ast.JoinedStr) and label != "Decision":
                    parts: list[str] = []
                    for piece in value.values:
                        if isinstance(piece, ast.Constant) and isinstance(
                            piece.value, str
                        ):
                            parts.append(piece.value)
                        elif isinstance(piece, ast.FormattedValue):
                            source = ast.unparse(piece.value)
                            if source == "node_type":
                                parts.append(label)
                                continue
                            rendered = _evaluated_hole(piece, evaluator)
                            parts.append(
                                rendered
                                if rendered is not None
                                else _HOLE_OPEN + source + _HOLE_CLOSE
                            )
                    text = "".join(parts)
                    break
            if text is None:
                continue
            generated.append((node.name, label, text, duplicate_of is not None))
    return generated


class _PureEvaluator:
    """Evaluate the pure string builders a public template interpolates.

    These helpers decide what the contract really sends -- an active-read filter is where
    ``coalesce`` enters the query -- so leaving them as holes hides the very constructs this
    corpus exists to classify.  They are evaluated from the AST at the pinned commit rather
    than imported or exec'd: nothing from the baseline runs, and an expression this evaluator
    does not understand leaves the hole intact instead of inventing text for it.
    """

    @classmethod
    def shared(cls, sources: dict[str, str], module: str) -> _PureEvaluator:
        """One evaluator per module, because building one re-parses that module.

        Resolving an imported constant used to construct a fresh evaluator -- a full parse of
        the other module -- for every occurrence of the name. The corpus is thousands of
        expressions deep, so the same handful of modules were parsed over and over.
        """

        key = (id(sources), module)
        cached = _EVALUATOR_CACHE.get(key)
        if cached is None:
            # The sources dict is stored ALONGSIDE the evaluator, not merely keyed by its
            # id: a dict that is garbage collected can have its id handed to a different
            # one, and the cache would then answer a question nobody asked. Holding the
            # reference makes that impossible rather than unlikely.
            cached = (sources, cls(sources, module))
            _EVALUATOR_CACHE[key] = cached
        return cached[1]

    def __init__(self, sources: dict[str, str], module: str) -> None:
        self._sources = sources
        self._module = module
        self._tree = ast.parse(sources[module])
        self._import_cache: dict[str, Any] = {}
        # Walked ONCE here rather than once per lookup.  ast.walk rather than a scan of the
        # module body on purpose: Pulse imports some constants INSIDE the function that uses
        # them, and a module-level-only map left those unresolvable for a reason that is
        # about where the import sits rather than about the constant.
        self._import_modules: dict[str, str] = {}
        for node in ast.walk(self._tree):
            if not isinstance(node, ast.ImportFrom) or node.module is None:
                continue
            relative = node.module.replace(".", "/") + ".py"
            for alias in node.names:
                self._import_modules.setdefault(alias.name, relative)
        self._constants: dict[str, Any] = {}
        self._functions: dict[str, ast.FunctionDef] = {}
        for node in self._tree.body:
            if isinstance(node, ast.FunctionDef):
                self._functions[node.name] = node
            elif isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name):
                    self._constants[target.id] = node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                # Annotated constants are constants too; collecting only bare assignments
                # left half the contract's tuples unresolvable.
                if isinstance(node.target, ast.Name):
                    self._constants[node.target.id] = node.value

    def call(self, name: str, arguments: list[Any]) -> Any:
        function = self._functions.get(name)
        if function is None:
            return None
        bindings: dict[str, Any] = {}
        names = [item.arg for item in function.args.args]
        for position, value in enumerate(arguments):
            if position < len(names):
                bindings[names[position]] = value
        for keyword, default in zip(
            names[len(names) - len(function.args.defaults) :],
            function.args.defaults,
            strict=False,
        ):
            bindings.setdefault(keyword, self._value(default, {}))
        # Keyword-only parameters carry their defaults in a separate list; missing them left
        # every helper with a `*,` signature unresolvable, which is most of them.
        for argument, default in zip(
            function.args.kwonlyargs, function.args.kw_defaults, strict=False
        ):
            if default is not None:
                bindings.setdefault(argument.arg, self._value(default, {}))
        # Walk the body in order rather than jumping to the return: these helpers build the
        # clause into a local first, and evaluating only the return leaves that local
        # unbound -- which reads as "cannot evaluate" for a helper that is perfectly pure.
        for statement in function.body:
            if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
                target = statement.targets[0]
                if isinstance(target, ast.Name):
                    bindings[target.id] = self._value(statement.value, bindings)
                elif isinstance(target, ast.Tuple):
                    unpacked = self._value(statement.value, bindings)
                    names = [
                        element.id
                        for element in target.elts
                        if isinstance(element, ast.Name)
                    ]
                    if isinstance(unpacked, (list, tuple)) and len(unpacked) == len(
                        names
                    ):
                        bindings.update(dict(zip(names, unpacked, strict=True)))
                    else:
                        for name in names:
                            bindings[name] = None
                continue
            if isinstance(statement, ast.Return) and statement.value is not None:
                # Returned as-is: these helpers build clauses AND endpoint-pair tuples, and
                # insisting on a string here nulled every structural one -- which then
                # collapsed the whole constant that referenced it.
                return self._value(statement.value, bindings)
        return None

    def _value(self, node: ast.AST, bindings: dict[str, Any]) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in bindings:
                return bindings[node.id]
            if node.id in self._constants:
                return self._value(self._constants[node.id], bindings)
            return self._imported(node.id)
        if isinstance(node, ast.JoinedStr):
            parts: list[str] = []
            for piece in node.values:
                if isinstance(piece, ast.Constant):
                    parts.append(str(piece.value))
                elif isinstance(piece, ast.FormattedValue):
                    rendered = self._value(piece.value, bindings)
                    if rendered is None:
                        return None
                    parts.append(str(rendered))
            return "".join(parts)
        if isinstance(node, ast.Dict):
            rendered: dict[Any, Any] = {}
            for key, value in zip(node.keys, node.values, strict=True):
                if key is None:
                    return None
                rendered[self._value(key, bindings)] = self._value(value, bindings)
            return rendered
        if isinstance(node, ast.Set):
            return {self._value(item, bindings) for item in node.elts}
        if isinstance(node, (ast.Tuple, ast.List)):
            items: list[Any] = []
            for element in node.elts:
                if isinstance(element, ast.Starred):
                    inner = self._value(element.value, bindings)
                    if inner is None:
                        return None
                    items.extend(inner)
                    continue
                value = self._value(element, bindings)
                if value is None:
                    return None
                items.append(value)
            return items
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = self._value(node.left, bindings)
            right = self._value(node.right, bindings)
            if isinstance(left, str) and isinstance(right, str):
                return left + right
            if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
                return [*left, *right]
            return None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            left = self._value(node.left, bindings)
            right = self._value(node.right, bindings)
            if isinstance(left, (set, frozenset)) and isinstance(
                right, (set, frozenset)
            ):
                return set(left) | set(right)
            return None
        if isinstance(node, ast.Subscript):
            container = self._value(node.value, bindings)
            index = self._value(node.slice, bindings)
            if isinstance(container, (list, tuple)) and isinstance(index, int):
                return container[index] if index < len(container) else None
            return None
        if isinstance(node, ast.Call):
            return self._call_value(node, bindings)
        if isinstance(node, ast.GeneratorExp):
            return self._generator(node, bindings)
        return None

    def _call_value(self, node: ast.Call, bindings: dict[str, Any]) -> Any:
        function = node.func
        if isinstance(function, ast.Name):
            if function.id == "sorted":
                inner = self._value(node.args[0], bindings)
                return sorted(inner) if inner is not None else None
            if function.id == "tuple":
                inner = self._value(node.args[0], bindings) if node.args else []
                return list(inner) if inner is not None else None
            if function.id == "frozenset":
                inner = self._value(node.args[0], bindings) if node.args else set()
                return frozenset(inner) if inner is not None else None
            arguments = [self._value(item, bindings) for item in node.args]
            # An unresolvable argument is not a dead end: many of these builders decide only
            # their PARAMETERS from it and return a constant clause.  The call is attempted
            # with the argument unbound, and fails only if the text actually needs it.
            return self.call(function.id, arguments)
        if isinstance(function, ast.Attribute) and function.attr == "join":
            separator = self._value(function.value, bindings)
            items = self._value(node.args[0], bindings)
            if isinstance(separator, str) and isinstance(items, list):
                return separator.join(str(item) for item in items)
        return None

    def _generator(self, node: ast.GeneratorExp, bindings: dict[str, Any]) -> Any:
        """Evaluate a comprehension, including nested loops and non-string elements.

        The schema builds its endpoint pairs with a two-level comprehension over two closed
        constants; handling only the single-loop, string-valued case silently returned
        nothing for it, and the whole tuple it lived in collapsed to None.  An
        under-enumerated domain is worse than a missing one: it reads as complete.
        """

        scopes: list[dict[str, Any]] = [dict(bindings)]
        for comprehension in node.generators:
            target = comprehension.target
            expanded: list[dict[str, Any]] = []
            for scope in scopes:
                iterable = self._value(comprehension.iter, scope)
                if iterable is None:
                    return None
                for item in iterable:
                    if isinstance(target, ast.Name):
                        expanded.append({**scope, target.id: item})
                        continue
                    if isinstance(target, ast.Tuple) and isinstance(
                        item, (list, tuple)
                    ):
                        names = [
                            element.id
                            for element in target.elts
                            if isinstance(element, ast.Name)
                        ]
                        if len(names) != len(item):
                            return None
                        expanded.append(
                            {**scope, **dict(zip(names, item, strict=True))}
                        )
                        continue
                    return None
            kept: list[dict[str, Any]] = []
            for scope in expanded:
                verdicts = [
                    self._predicate(condition, scope) for condition in comprehension.ifs
                ]
                if any(verdict is None for verdict in verdicts):
                    # One unprovable filter poisons the WHOLE comprehension.  Treating it as
                    # true would drop or keep elements on no evidence and freeze text Pulse
                    # never sends -- a wrong answer is worse here than an unresolved hole.
                    return None
                if all(verdicts):
                    kept.append(scope)
            scopes = kept
        rendered: list[Any] = []
        for scope in scopes:
            value = self._value(node.elt, scope)
            if value is None:
                return None
            rendered.append(value)
        return rendered

    def _predicate(self, node: ast.expr, bindings: dict[str, Any]) -> bool | None:
        """A comprehension filter's answer, or None when this evaluator cannot prove it.

        Only membership and equality over values already resolved: those are what Pulse's
        builders actually use, and a wider evaluator here would be guessing about text that
        goes to a database.
        """

        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left = self._value(node.left, bindings)
            right = self._value(node.comparators[0], bindings)
            if left is None or right is None:
                return None
            operator = node.ops[0]
            if isinstance(operator, ast.Eq):
                return bool(left == right)
            if isinstance(operator, ast.NotEq):
                return bool(left != right)
            if isinstance(operator, ast.In):
                return bool(left in right)
            if isinstance(operator, ast.NotIn):
                return bool(left not in right)
            return None
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            inner = self._predicate(node.operand, bindings)
            return None if inner is None else not inner
        if isinstance(node, ast.BoolOp):
            verdicts = [self._predicate(item, bindings) for item in node.values]
            if any(verdict is None for verdict in verdicts):
                return None
            if isinstance(node.op, ast.And):
                return all(verdicts)
            return any(verdicts)
        return None

    def _imported(self, name: str) -> Any:
        """Follow a constant this module imports, reading it at the pinned commit too.

        ``ast.walk`` rather than a scan of the module body on purpose: Pulse imports some
        constants INSIDE the function that uses them, and a module-level-only lookup left
        those unresolvable for a reason that is about where the import sits.
        """

        if name in self._import_cache:
            return self._import_cache[name]
        resolved = None
        relative = self._import_modules.get(name)
        if relative is not None and relative in self._sources:
            other = _PureEvaluator.shared(self._sources, relative)
            value = other._constants.get(name)
            resolved = other._value(value, {}) if value is not None else None
        self._import_cache[name] = resolved
        return resolved


def _public_templates(sources: dict[str, str]) -> list[Finding]:
    """Freeze the read-only templates the public contract exposes as module constants."""

    relative = "okto_pulse/core/kg/cypher_templates.py"
    if relative not in sources:
        message = f"the public template module is missing at {relative}"
        raise FreezeError(message)
    tree = ast.parse(sources[relative])
    evaluator = _PureEvaluator(sources, relative)
    findings: list[Finding] = []
    constants: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or not target.id.isupper():
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            kind, template, holes = "literal", node.value.value, []
        elif isinstance(node.value, ast.JoinedStr):
            template, holes = _normalize_fstring(node.value, evaluator)
            kind = "fstring"
        else:
            continue
        finding = Finding(
            entry_id=f"public:core:cypher_templates.{target.id}",
            baseline="core",
            path="okto_pulse/core/kg/cypher_templates.py",
            line=node.lineno,
            symbol=target.id,
            surface="public_named_template",
            receiver="<contract>",
            template_kind=kind,
            template=template,
            placeholders=holes,
            params=sorted(set(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", template))),
        )
        if not _looks_like_statement(template):
            # A WHERE fragment is not a query.  Recorded rather than skipped, so the corpus
            # accounts for every public constant it saw instead of quietly ignoring some.
            finding.surface = "public_fragment"
            finding.statement_class = "fragment"
            finding.classification = "fragment"
            findings.append(finding)
            continue
        constants[target.id] = template
        finding.statement_class = _classify_statement_class(template)
        _classify(finding, sources)
        findings.append(finding)

    seen_texts = {item.template for item in findings}
    for generator, label, text, duplicate in _generated_templates(
        tree, sources, constants, evaluator
    ):
        finding = Finding(
            entry_id=f"public:core:cypher_templates.{generator}({label})",
            baseline="core",
            path=relative,
            line=0,
            symbol=f"{generator}({label})",
            surface="public_named_template",
            receiver="<contract>",
            template_kind="generated",
            template=text,
            placeholders=sorted(set(_PLACEHOLDER.findall(text))),
            params=sorted(set(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", text))),
        )
        finding.statement_class = _classify_statement_class(text)
        if duplicate or text in seen_texts:
            # Recorded, not dropped: the label is part of the contract even when the text it
            # produces is one the named constants already carry.
            finding.classification = "duplicate_text"
            findings.append(finding)
            continue
        seen_texts.add(text)
        _classify(finding, sources)
        findings.append(finding)
    return findings


def _looks_like_statement(template: str) -> bool:
    head = template.strip().split(None, 1)
    return bool(head) and head[0].upper() in {
        "MATCH",
        "OPTIONAL",
        "UNWIND",
        "WITH",
        "RETURN",
        "CREATE",
        "MERGE",
        "CALL",
        "ALTER",
        "COPY",
    }


def _check_duplicates(findings: list[Finding]) -> None:
    """One template may appear many times, but never with two different verdicts."""

    by_template: dict[str, Finding] = {}
    for finding in findings:
        if finding.classification in {"pass_through", "duplicate_text"}:
            # A duplicate is declared, not discovered: its whole point is that the text is
            # already carried by another entry, so it is not a second verdict on it.
            continue
        seen = by_template.get(finding.template)
        if seen is None:
            by_template[finding.template] = finding
            continue
        if (seen.classification, seen.statement_class) != (
            finding.classification,
            finding.statement_class,
        ):
            message = (
                "the same template was classified two ways:\n"
                f"  {seen.entry_id}: {seen.statement_class}/{seen.classification}\n"
                f"  {finding.entry_id}: "
                f"{finding.statement_class}/{finding.classification}"
            )
            raise FreezeError(message)


# The audited per-module family counts.  Kept here as an invariant so a scanner change that
# silently moves the surface fails loudly instead of redefining what the corpus describes.
AUDITED_FAMILY_COUNTS: dict[str, tuple[int, int]] = {
    "okto_pulse/core/application/kg_tick.py": (0, 1),
    "okto_pulse/core/events/handlers/cancellation_decay.py": (0, 2),
    "okto_pulse/core/events/handlers/card_boost_recompute.py": (4, 2),
    "okto_pulse/core/events/handlers/kg_decay_tick.py": (3, 0),
    "okto_pulse/core/kg/scoring.py": (1, 4),
    "okto_pulse/core/kg/canonical_learning_partition.py": (2, 0),
    "okto_pulse/core/kg/canonical_stale_reconciler.py": (2, 3),
    "okto_pulse/core/kg/dedup_migration.py": (9, 5),
    "okto_pulse/core/kg/governance.py": (1, 2),
    "okto_pulse/core/kg/primitives.py": (21, 0),
    "okto_pulse/core/kg/global_discovery/clustering.py": (0, 1),
    "okto_pulse/core/kg/kg_service.py": (0, 1),
    "okto_pulse/community/api/kg_routes.py": (4, 0),
}

# The two families the audit classifies as preventive rather than runtime-current: fallbacks
# that exist for a provider without the vector tombstone primitive.  Keyed by ORIGIN and
# checked against the audited ids, so a reordering of the inventory cannot move the marking
# onto a different family without failing the freeze.
PREVENTIVE_ORIGINS: dict[tuple[str, int], str] = {
    # Both sit in the `else` of `if callable(replace_tombstone)`: they run only for a
    # provider without the vector tombstone primitive.  :899 is NOT one of them -- it is the
    # `else` of `if source_deleted`, the demotion of a source that still exists but lost
    # canonical eligibility, and current providers take it.
    ("okto_pulse/core/kg/canonical_stale_reconciler.py", 761): "I21",
    ("okto_pulse/core/kg/canonical_stale_reconciler.py", 874): "I22",
}


def _re_compile(pattern: str):
    """Compile with the flags every clause pattern here needs."""

    return re.compile(pattern, re.IGNORECASE | re.DOTALL)


_RETURN_CLAUSE = _re_compile(r"\bRETURN\s+(.*?)(?:\s+ORDER\s+BY|\s+LIMIT\b|$)")
_ORDER_BY = _re_compile(r"\bORDER\s+BY\s+(.+?)(?:\s+LIMIT\b|$)")
_LIMIT = _re_compile(r"\bLIMIT\s+(\S+)")
_DELETE_TARGETS = _re_compile(r"\b(?:DETACH\s+)?DELETE\s+(.+?)(?:\s+RETURN\b|$)")
_CREATE_PATTERN = _re_compile(r"\bCREATE\s+(.+?)(?:\s+RETURN\b|$)")
_SET_TARGETS = _re_compile(r"\bSET\s+(.+?)(?:\s+RETURN\b|$)")
_ALIAS_SUFFIX = _re_compile(r"\s+AS\s+[A-Za-z_][A-Za-z0-9_]*\s*$")
_PROPERTY_EXPRESSION = _re_compile(
    r"^[A-Za-z_][A-Za-z0-9_]*\.([A-Za-z_][A-Za-z0-9_]*)$"
)
_ERROR_CODE = re.compile(r"\[code=([A-Za-z0-9_]+)")


def _split_top_level(body: str) -> list[str]:
    """Split on commas that are not inside brackets.

    A naive split breaks `COALESCE(n.query_hits, 0)` into two assignments that do not exist,
    which would put a made-up target into the corpus and make the oracle wrong in a way that
    reads as detail.
    """

    items: list[str] = []
    depth = 0
    current = ""
    for character in " ".join(body.split()):
        if character in "([{":
            depth += 1
        elif character in ")]}":
            depth -= 1
        if character == "," and depth == 0:
            items.append(current.strip())
            current = ""
            continue
        current += character
    if current.strip():
        items.append(current.strip())
    return items


def _projection_columns(template: str) -> list[str]:
    match = _RETURN_CLAUSE.search(template)
    if match is None:
        return []
    return _split_top_level(match.group(1))


_BOUND_BY_ID = _re_compile(r"\{\s*id\s*:\s*\$|\.id\s*=\s*\$")
_RELATIONSHIP_PATTERN = _re_compile(r"(?:<-)?-\s*\[")
_AGGREGATING_WITH = _re_compile(r"\bWITH\b.*\b(?:COUNT|SUM|AVG|MIN|MAX)\s*\(")


def _unalias(expression: str) -> str:
    match = _ALIAS_SUFFIX.search(expression)
    value = expression[: match.start()] if match else expression
    value = value.strip()
    if value.upper().startswith("DISTINCT "):
        value = value[9:].strip()
    return value


def _property_type(name: str) -> str | None:
    if name == "embedding":
        return "VECTOR_F32"
    return _COLUMN_TYPES.get(name) or _EDGE_COLUMN_TYPES.get(name)


def _projection_metadata(expression: str, template: str) -> dict[str, Any]:
    """Conservative static type/nullability oracle for one returned expression.

    ``dynamic`` is deliberate: it says the corpus knows a value is returned but the pinned
    query text does not prove one concrete type.  Inventing a precise type there would make
    a future differential runner reject a valid result for an assertion the corpus never
    had enough evidence to make.
    """

    value = _unalias(expression)
    upper = " ".join(value.upper().split())

    if upper == "NULL":
        return {"expression": expression, "type": "NULL", "nullable": True}
    if upper in {"TRUE", "FALSE"}:
        return {"expression": expression, "type": "BOOL", "nullable": False}
    if re.fullmatch(r"[-+]?\d+", value):
        return {"expression": expression, "type": "INT64", "nullable": False}
    if re.fullmatch(r"[-+]?(?:\d+\.\d*|\d*\.\d+)", value):
        return {"expression": expression, "type": "DOUBLE", "nullable": False}
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return {"expression": expression, "type": "STRING", "nullable": False}

    if upper.startswith("COUNT("):
        return {"expression": expression, "type": "INT64", "nullable": False}
    if upper.startswith("COLLECT("):
        return {
            "expression": expression,
            "type": "LIST<dynamic>",
            "nullable": False,
        }
    if upper.startswith("AVG("):
        return {"expression": expression, "type": "DOUBLE", "nullable": True}
    for aggregate in ("SUM", "MIN", "MAX"):
        if not upper.startswith(f"{aggregate}("):
            continue
        inner = value[value.find("(") + 1 : value.rfind(")")].strip()
        inferred = _projection_metadata(inner, template)["type"]
        if aggregate == "SUM" and inferred not in {"INT64", "DOUBLE"}:
            inferred = "dynamic"
        return {"expression": expression, "type": inferred, "nullable": True}

    function_types = {
        "LABEL(": ("STRING", False),
        "SIZE(": ("INT64", True),
        "STRING_SPLIT(": ("LIST<STRING>", True),
        "TIMESTAMP(": ("TIMESTAMP", True),
        "SIMILARITY(": ("DOUBLE", True),
        "SIMILARITY_SCORE(": ("DOUBLE", True),
    }
    for prefix, (kind, nullable) in function_types.items():
        if upper.startswith(prefix):
            return {
                "expression": expression,
                "type": kind,
                "nullable": nullable,
            }
    if upper.startswith("COALESCE("):
        body = value[value.find("(") + 1 : value.rfind(")")]
        arguments = _split_top_level(body)
        metadata = [_projection_metadata(argument, template) for argument in arguments]
        kind = next(
            (item["type"] for item in metadata if item["type"] != "NULL"),
            "dynamic",
        )
        return {
            "expression": expression,
            "type": kind,
            "nullable": all(item["nullable"] for item in metadata),
        }

    property_match = _PROPERTY_EXPRESSION.fullmatch(value)
    if property_match:
        name = property_match.group(1)
        return {
            "expression": expression,
            "type": _property_type(name) or "dynamic",
            "nullable": name != "id",
        }

    if upper.startswith("CASE "):
        arms = re.findall(
            r"\b(?:THEN|ELSE)\s+(.+?)(?=\s+WHEN\b|\s+ELSE\b|\s+END\b)",
            value,
            flags=re.IGNORECASE | re.DOTALL,
        )
        arm_metadata = [_projection_metadata(arm.strip(), template) for arm in arms]
        kinds = {item["type"] for item in arm_metadata if item["type"] != "NULL"}
        kind = (
            "DOUBLE"
            if kinds and kinds <= {"INT64", "DOUBLE"} and "DOUBLE" in kinds
            else next(iter(kinds))
            if len(kinds) == 1
            else "dynamic"
        )
        return {
            "expression": expression,
            "type": kind,
            "nullable": " ELSE " not in f" {upper} "
            or any(item["nullable"] for item in arm_metadata),
        }

    if upper.endswith(" IS NULL") or upper.endswith(" IS NOT NULL"):
        return {"expression": expression, "type": "BOOL", "nullable": False}

    if any(operator in value for operator in (" + ", " - ", " * ", " / ")):
        property_types = {
            _property_type(name)
            for name in re.findall(
                r"[A-Za-z_][A-Za-z0-9_]*\.([A-Za-z_][A-Za-z0-9_]*)",
                value,
            )
        }
        kind = "DOUBLE" if "DOUBLE" in property_types or "." in value else "INT64"
        return {"expression": expression, "type": kind, "nullable": True}

    # Entity projections are distinct from arbitrary row/map variables.  They matter for
    # serialization tests even though their property shapes are catalog-driven.
    escaped = re.escape(value)
    if re.search(rf"\b{escaped}\s*=\s*\(", template):
        return {"expression": expression, "type": "PATH", "nullable": False}
    if re.search(rf"\[\s*{escaped}(?:\s*:|\s*\])", template):
        return {"expression": expression, "type": "RELATIONSHIP", "nullable": False}
    if re.search(rf"\(\s*{escaped}(?:\s*:|\s*\)|\s*\{{)", template):
        return {"expression": expression, "type": "NODE", "nullable": False}

    return {"expression": expression, "type": "dynamic", "nullable": True}


def _expected_error(phase: str, detail: str | None) -> dict[str, str] | None:
    if phase in {"planned", "not_evaluated"}:
        return None
    rendered = detail or f"the query was refused during {phase}"
    code_match = _ERROR_CODE.search(rendered)
    type_name, separator, message = rendered.partition(":")
    return {
        "phase": phase,
        "code": code_match.group(1) if code_match else phase,
        "type": type_name.strip() if separator else "QueryError",
        "message": message.strip() if separator else rendered,
    }


def _read_cardinality(
    template: str,
    aggregate_columns: list[str],
    bare_columns: list[str],
    limit: re.Match[str] | None,
) -> str:
    if aggregate_columns and not bare_columns:
        # An ungrouped aggregate answers with exactly one row, even over no matches.
        return "exactly_one_row"
    if limit is not None:
        bound = limit.group(1).strip()
        return "at_most_one_row" if bound == "1" else f"at_most_{bound}_rows"
    if _BOUND_BY_ID.search(template):
        has_fanout = _RELATIONSHIP_PATTERN.search(template) is not None
        folds_fanout = _AGGREGATING_WITH.search(template) is not None
        if not has_fanout or folds_fanout:
            return "at_most_one_row"
    return "many_rows"


def _write_cardinality(template: str) -> str:
    """How many rows the write is expected to touch, which is not always one.

    A statement that matches a label with no identity predicate rewrites every node under
    that label; calling that "at most one" would understate the blast radius of exactly the
    families a differential run most needs to bound.
    """

    upper = " ".join(template.upper().split())
    if "UNWIND" in upper or "$rows" in template:
        return "one_per_batch_row"
    if "DELETE" in upper and "DETACH DELETE" not in upper:
        # An id predicate binds an ENDPOINT, not the relationship: two nodes may be joined
        # more than once, so this statement can remove several edges.  Calling it "at most
        # one" understates exactly the blast radius a differential run needs bounded.
        return "all_matched_relationships"
    if _BOUND_BY_ID.search(template):
        return "at_most_one"
    return "all_matched_rows"


def _expected_shape(
    template: str,
    statement_class: str,
    acceptance_phase: str,
    error_detail: str | None,
) -> dict[str, Any]:
    """State what the family is for, not merely what verb it uses.

    A corpus that recorded only the RETURN list would not say whether order matters, and a
    later differential run would either compare orderings that the query never promised or
    miss one it did.  Ordering is claimed only where ORDER BY claims it; everything else is
    compared as a multiset.
    """

    ordered = _ORDER_BY.search(template)
    limit = _LIMIT.search(template)
    if statement_class == "read":
        columns = _projection_columns(template)
        aggregate_columns = [
            column
            for column in columns
            if column.strip()
            .upper()
            .startswith(("COUNT(", "COLLECT(", "SUM(", "AVG(", "MIN(", "MAX("))
        ]
        bare_columns = [column for column in columns if column not in aggregate_columns]
        cardinality = _read_cardinality(
            template, aggregate_columns, bare_columns, limit
        )
        return {
            "kind": "rows",
            "columns": columns,
            "column_count": len(columns),
            "column_metadata": [
                _projection_metadata(column, template) for column in columns
            ],
            "cardinality": cardinality,
            "ordering": "ordered" if ordered else "multiset",
            "order_by": " ".join(ordered.group(1).split()) if ordered else None,
            "limit": limit.group(1).strip() if limit else None,
            "notes": (
                "ungrouped aggregate: one null-free scalar row"
                if aggregate_columns and not bare_columns
                else "aggregate over a grouping key"
                if aggregate_columns
                else "nullable columns follow the stored node/edge properties"
            ),
            "error": _expected_error(acceptance_phase, error_detail),
        }
    targets = _SET_TARGETS.search(template)
    # Plain text tests rather than patterns: these tokens are unambiguous, and an escaping
    # slip in a regex here fails silently -- it reports "set_properties" for a DELETE and
    # nothing looks wrong.
    upper = " ".join(template.upper().split())
    detach = "DETACH DELETE" in upper
    deletes = "DELETE" in upper
    creates = "CREATE " in upper or upper.endswith("CREATE")
    effect = (
        "detach_delete"
        if detach
        else "delete"
        if deletes
        else "create"
        if creates
        else "set_properties"
    )
    if effect == "set_properties":
        recorded = _split_top_level(targets.group(1)) if targets else []
    elif effect in {"delete", "detach_delete"}:
        # What a DELETE targets is the bound variable it removes, and a corpus that left
        # that empty would say a write has no effect -- the one thing a write always has.
        deleted = _DELETE_TARGETS.search(template)
        recorded = _split_top_level(deleted.group(1)) if deleted else []
    else:
        created = _CREATE_PATTERN.search(template)
        recorded = [" ".join(created.group(1).split())] if created else []
    returns = _projection_columns(template)
    return {
        "kind": "effect",
        "effect": effect,
        "targets": recorded,
        "returns": returns,
        "return_metadata": [
            _projection_metadata(column, template) for column in returns
        ],
        "expected_count": _write_cardinality(template),
        "atomicity": (
            "one statement inside the caller's transaction; the scope rolls it back with "
            "the rest of the session when compensation runs"
        ),
        "error": _expected_error(acceptance_phase, error_detail),
    }


# ---------------------------------------------------------------------------
# The public raw contract, as the pinned commit enforces it
# ---------------------------------------------------------------------------
#
# The contract's answer is NOT a matter of grammar.  ``validate_cypher_read_only`` strips
# comments, NFKC-normalizes, blanks string literals, then tokenizes with ``[A-Z_]+`` over the
# UPPERCASED text -- so every identifier becomes a token too.  A blacklisted token anywhere is
# ``unsafe_cypher``; otherwise the FIRST token must be a supported root operation or the
# refusal is ``unsupported_operation``.  Those are different answers, and the matrix records
# which one each construct earns rather than a single "refused".
CONTRACT_MODULE = "okto_pulse/core/kg/tier_power.py"
CONTRACT_VOCABULARY_MODULE = "okto_pulse/core/kg/query_contract.py"

# Built by concatenation rather than escaping: the pattern contains both quote characters, and
# an escaped copy is one transcription error away from matching something else.
_STRING_LITERAL_PATTERN = "'[^']*'" + '|"[^"]*"'

# The pipeline this freezer reproduces, named in the order the pinned source applies it, and
# re-verified against that source on every freeze.
_VALIDATOR_STEPS: tuple[str, ...] = (
    "_strip_comments",
    "_normalize_unicode",
    "_strip_string_literals",
)
_EXECUTE_STEPS: tuple[str, ...] = (
    "clamp_max_rows",
    "_normalize_unicode",
    "validate_cypher_read_only",
    "_auto_inject_limit",
    "_auto_bound_var_length_path",
    "_rewrite_cypher_canonical_only",
)
_VALIDATOR_CODES: tuple[str, ...] = ("unsafe_cypher", "unsupported_operation")


def _called_names(function: ast.AST) -> list[str]:
    """Every function this body calls, in source order."""

    found: list[tuple[int, str]] = []
    for node in ast.walk(function):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name):
                found.append((node.lineno, target.id))
            elif isinstance(target, ast.Attribute):
                found.append((node.lineno, target.attr))
    return [name for _line, name in sorted(found)]


def _function_named(tree: ast.AST, name: str) -> ast.AST | None:
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ):
            return node
    return None


def _raised_codes(function: ast.AST) -> list[str]:
    """The literal error codes this body raises, in source order."""

    codes: list[tuple[int, str]] = []
    for node in ast.walk(function):
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            for argument in node.exc.args[:1]:
                if isinstance(argument, ast.Constant) and isinstance(
                    argument.value, str
                ):
                    codes.append((node.lineno, argument.value))
    return [code for _line, code in sorted(codes)]


def _check_contract_pipeline(sources: dict[str, str]) -> dict[str, Any]:
    """Refuse to freeze unless the pinned source still performs the pipeline reproduced here.

    This freezer never imports or executes the baseline, so the contract verdicts below are a
    REPRODUCTION.  A reproduction that is not pinned to its original is a second implementation
    waiting to disagree in silence, so the steps and the error codes are re-read at the commit
    and any drift fails the freeze instead of quietly being reported as a contract change.
    """

    tree = ast.parse(sources[CONTRACT_MODULE])
    validator = _function_named(tree, "validate_cypher_read_only")
    executor = _function_named(tree, "execute_cypher_read_only")
    if validator is None or executor is None:
        message = (
            f"{CONTRACT_MODULE} no longer defines validate_cypher_read_only and "
            "execute_cypher_read_only; the raw contract matrix has lost its authority."
        )
        raise FreezeError(message)

    validator_calls = [
        name for name in _called_names(validator) if name in _VALIDATOR_STEPS
    ]
    if tuple(dict.fromkeys(validator_calls)) != _VALIDATOR_STEPS:
        message = (
            "validate_cypher_read_only no longer applies "
            f"{list(_VALIDATOR_STEPS)} in that order (found {validator_calls}). The "
            "reproduction in this freezer would silently describe a different validator."
        )
        raise FreezeError(message)

    codes = tuple(dict.fromkeys(_raised_codes(validator)))
    if codes != _VALIDATOR_CODES:
        message = (
            f"validate_cypher_read_only now raises {list(codes)} rather than "
            f"{list(_VALIDATOR_CODES)}; the unsafe/unsupported split this matrix records is "
            "no longer the one the contract implements."
        )
        raise FreezeError(message)

    executor_calls = [
        name for name in _called_names(executor) if name in _EXECUTE_STEPS
    ]
    if tuple(dict.fromkeys(executor_calls)) != _EXECUTE_STEPS:
        message = (
            "execute_cypher_read_only no longer applies "
            f"{list(_EXECUTE_STEPS)} in that order (found {executor_calls}). Limit "
            "injection, path bounding and the layer rewrite are order-dependent."
        )
        raise FreezeError(message)

    return {
        "module": CONTRACT_MODULE,
        "validator_steps": list(_VALIDATOR_STEPS),
        "validator_error_codes": list(_VALIDATOR_CODES),
        "execute_steps": list(_EXECUTE_STEPS),
    }


def _contract_tokens(text: str) -> list[str]:
    """The tokens the contract inspects, by the pinned pipeline's own steps and order."""

    cleaned = re.sub(r"//[^\n]*", "", text)
    cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)
    cleaned = unicodedata.normalize("NFKC", cleaned)
    cleaned = re.sub(_STRING_LITERAL_PATTERN, "'__STR__'", cleaned)
    return re.findall(r"[A-Z_]+", cleaned.upper())


def _contract_vocabulary(sources: dict[str, str]) -> tuple[set[str], tuple[str, ...]]:
    """The blacklist and the supported root operations, read at the pin."""

    enforcement = _PureEvaluator(sources, CONTRACT_MODULE)
    blacklist = (
        enforcement._value(enforcement._constants.get("CYPHER_BLACKLIST"), {}) or []
    )
    vocabulary = _PureEvaluator(sources, CONTRACT_VOCABULARY_MODULE)
    roots = (
        vocabulary._value(
            vocabulary._constants.get("CYPHER_SUPPORTED_ROOT_OPERATIONS"), {}
        )
        or []
    )
    if not blacklist or not roots:
        message = (
            "the raw contract vocabulary could not be read at the pinned commit "
            f"(blacklist={len(blacklist)}, roots={len(roots)}). A verdict computed from an "
            "empty vocabulary would admit everything."
        )
        raise FreezeError(message)
    return {str(item) for item in blacklist}, tuple(str(item) for item in roots)


def _contract_verdict(text: str, sources: dict[str, str]) -> dict[str, Any]:
    """What the PUBLIC raw endpoint does with this text, reproduced from the pinned commit."""

    blacklist, roots = _contract_vocabulary(sources)
    tokens = _contract_tokens(text)
    for token in tokens:
        if token in blacklist:
            return {
                "admitted": False,
                "error_code": "unsafe_cypher",
                "reason": f"blacklisted keyword {token}",
            }
    root = tokens[0] if tokens else ""
    if root not in roots:
        return {
            "admitted": False,
            "error_code": "unsupported_operation",
            "reason": f"root operation {root or '<empty>'}",
        }
    return {"admitted": True, "error_code": None, "reason": None}


# The raw read-only endpoint is a GRAMMAR, not a list of queries.  Each construct gets one
# minimal probe so the matrix says what the engine does with that construct today; the probes
# are representative cases, never a claim that these are the queries Pulse sends.
# (category, construct, probe, contract_disposition).  The disposition is the PUBLIC
# contract's answer; the verdict recorded beside it is this ENGINE's.  They are
# different questions and the matrix must not blur them: the raw endpoint refuses every
# write, while the engine accepts writes because the internal port needs them.
# (category, construct, probe, contract_disposition).  The disposition is the PUBLIC
# contract's answer; the verdict beside it is this ENGINE's.  Every probe but the one
# ABOUT untyped relationships names a type from a triple the catalog really carries, so
# each measures its own construct instead of re-measuring the untyped-relationship gap.
RAW_CONTRACT_PROBES: tuple[tuple[str, str, str, str], ...] = (
    ("root", "MATCH", "MATCH (n:Decision) RETURN n.id", "allowed"),
    (
        "root",
        "OPTIONAL MATCH",
        "OPTIONAL MATCH (n:Decision) RETURN n.id",
        "allowed",
    ),
    ("root", "UNWIND", "UNWIND $rows AS r RETURN r", "allowed"),
    ("root", "WITH", "WITH 1 AS value RETURN value", "allowed"),
    ("root", "RETURN", "RETURN 1 AS value", "allowed"),
    ("clause", "WHERE", "MATCH (n:Decision) WHERE n.id = $id RETURN n.id", "allowed"),
    ("clause", "ORDER BY", "MATCH (n:Decision) RETURN n.id ORDER BY n.id", "allowed"),
    ("clause", "ASC", "MATCH (n:Decision) RETURN n.id ORDER BY n.id ASC", "allowed"),
    ("clause", "DESC", "MATCH (n:Decision) RETURN n.id ORDER BY n.id DESC", "allowed"),
    ("clause", "LIMIT", "MATCH (n:Decision) RETURN n.id LIMIT 10", "allowed"),
    ("clause", "DISTINCT", "MATCH (n:Decision) RETURN DISTINCT n.id", "allowed"),
    (
        "clause",
        "UNION",
        "MATCH (n:Decision) RETURN n.id UNION MATCH (m:Bug) RETURN m.id",
        "allowed",
    ),
    ("clause", "AS", "MATCH (n:Decision) RETURN n.id AS identifier", "allowed"),
    (
        "boolean",
        "AND",
        "MATCH (n:Decision) WHERE n.id = $a AND n.title = $b RETURN n.id",
        "allowed",
    ),
    (
        "boolean",
        "OR",
        "MATCH (n:Decision) WHERE n.id = $a OR n.title = $b RETURN n.id",
        "allowed",
    ),
    ("boolean", "NOT", "MATCH (n:Decision) WHERE NOT n.id = $a RETURN n.id", "allowed"),
    ("boolean", "IN", "MATCH (n:Decision) WHERE n.id IN $ids RETURN n.id", "allowed"),
    (
        "comparison",
        "equality",
        "MATCH (n:Decision) WHERE n.id = $a RETURN n.id",
        "allowed",
    ),
    (
        "comparison",
        "inequality",
        "MATCH (n:Decision) WHERE n.id <> $a RETURN n.id",
        "allowed",
    ),
    (
        "comparison",
        "ordering",
        "MATCH (n:Decision) WHERE n.relevance_score >= $a RETURN n.id",
        "allowed",
    ),
    ("arithmetic", "addition", "MATCH (n:Decision) RETURN n.query_hits + 1", "allowed"),
    (
        "null",
        "IS NULL",
        "MATCH (n:Decision) WHERE n.superseded_by IS NULL RETURN n.id",
        "allowed",
    ),
    (
        "null",
        "IS NOT NULL",
        "MATCH (n:Decision) WHERE n.superseded_by IS NOT NULL RETURN n.id",
        "allowed",
    ),
    (
        "literal",
        "TRUE",
        "MATCH (n:Decision) WHERE n.human_curated = true RETURN n.id",
        "allowed",
    ),
    (
        "literal",
        "FALSE",
        "MATCH (n:Decision) WHERE n.human_curated = false RETURN n.id",
        "allowed",
    ),
    (
        "literal",
        "quoted string",
        "MATCH (n:Decision) WHERE n.title = 'x' RETURN n.id",
        "allowed",
    ),
    (
        "parameter",
        "scalar",
        "MATCH (n:Decision) WHERE n.id = $id RETURN n.id",
        "allowed",
    ),
    (
        "parameter",
        "list",
        "MATCH (n:Decision) WHERE n.id IN $ids RETURN n.id",
        "allowed",
    ),
    ("parameter", "map batch", "UNWIND $rows AS r RETURN r", "allowed"),
    ("expression", "list index", "MATCH (n:Decision) RETURN $ids[1]", "allowed"),
    ("expression", "map access", "UNWIND $rows AS r RETURN r.id", "allowed"),
    (
        "expression",
        "CASE searched",
        "MATCH (n:Decision) RETURN CASE WHEN n.id = $a THEN 1 ELSE 0 END",
        "allowed",
    ),
    (
        "expression",
        "CASE simple",
        "MATCH (n:Decision) RETURN CASE n.id WHEN $a THEN 1 ELSE 0 END",
        "allowed",
    ),
    (
        "string",
        "CONTAINS",
        "MATCH (n:Decision) WHERE n.title CONTAINS $q RETURN n.id",
        "allowed",
    ),
    (
        "string",
        "STARTS WITH",
        "MATCH (n:Decision) WHERE n.title STARTS WITH $q RETURN n.id",
        "allowed",
    ),
    (
        "string",
        "ENDS WITH",
        "MATCH (n:Decision) WHERE n.title ENDS WITH $q RETURN n.id",
        "allowed",
    ),
    ("aggregation", "COUNT", "MATCH (n:Decision) RETURN count(n)", "allowed"),
    ("aggregation", "COLLECT", "MATCH (n:Decision) RETURN collect(n.id)", "allowed"),
    (
        "aggregation",
        "SUM",
        "MATCH (n:Decision) RETURN sum(n.relevance_score)",
        "allowed",
    ),
    (
        "aggregation",
        "AVG",
        "MATCH (n:Decision) RETURN avg(n.relevance_score)",
        "allowed",
    ),
    (
        "aggregation",
        "MIN",
        "MATCH (n:Decision) RETURN min(n.relevance_score)",
        "allowed",
    ),
    (
        "aggregation",
        "MAX",
        "MATCH (n:Decision) RETURN max(n.relevance_score)",
        "allowed",
    ),
    ("function", "label", "MATCH (n:Decision) RETURN label(n)", "allowed"),
    (
        "function",
        "coalesce",
        "MATCH (n:Decision) RETURN coalesce(n.title, '')",
        "allowed",
    ),
    (
        "function",
        "string_split",
        "MATCH (n:Decision) RETURN string_split(n.title, ',')",
        "allowed",
    ),
    ("function", "size", "MATCH (n:Decision) RETURN size(n.title)", "allowed"),
    (
        "function",
        "timestamp",
        "MATCH (n:Decision) RETURN timestamp($created_at)",
        "allowed",
    ),
    (
        "function",
        "similarity",
        (
            "MATCH (n:Decision) WHERE similarity(n.embedding, $embedding, "
            "space => 'pulse_corpus') > 0 RETURN n.id"
        ),
        "allowed",
    ),
    (
        "function",
        "similarity_score",
        (
            "MATCH (n:Decision) WHERE similarity(n.embedding, $embedding, "
            "space => 'pulse_corpus') > 0 RETURN similarity_score()"
        ),
        "allowed",
    ),
    (
        "pattern",
        "anonymous node",
        "MATCH (n:Decision)-[r:supersedes]->() RETURN n.id",
        "allowed",
    ),
    (
        "pattern",
        "typed relationship",
        "MATCH (a:Decision)-[r:supersedes]->(b:Decision) RETURN a.id",
        "allowed",
    ),
    (
        "pattern",
        "incoming",
        "MATCH (a:Decision)<-[r:supersedes]-(b:Decision) RETURN a.id",
        "allowed",
    ),
    (
        "pattern",
        "outgoing",
        "MATCH (a:Decision)-[:supersedes]->(b:Decision) RETURN b.id",
        "allowed",
    ),
    (
        "pattern",
        "undirected",
        "MATCH (a:Decision)-[r:supersedes]-(b:Decision) RETURN a.id",
        "allowed",
    ),
    (
        "pattern",
        "untyped relationship",
        "MATCH (a:Decision)-[r]->(b) RETURN a.id",
        "allowed",
    ),
    ("pattern", "polymorphic node", "MATCH (n) RETURN n", "allowed"),
    (
        "pattern",
        "fixed hop 2",
        (
            "MATCH (a:Decision)-[:supersedes]->(m:Decision)"
            "-[:supersedes]->(b:Decision) RETURN b.id"
        ),
        "allowed",
    ),
    (
        "pattern",
        "variable length depth 1",
        "MATCH (a:Decision)-[r:supersedes*1..1]->(b:Decision) RETURN a.id",
        "allowed",
    ),
    (
        "pattern",
        "variable length depth 2",
        "MATCH (a:Decision)-[r:supersedes*1..2]->(b:Decision) RETURN a.id",
        "allowed",
    ),
    (
        "pattern",
        "named path",
        "MATCH path = (a:Decision)-[r:supersedes]->(b:Decision) RETURN a.id",
        "allowed",
    ),
    (
        "limits",
        "path cap 20",
        "MATCH (a:Decision)-[r:supersedes*1..20]->(b:Decision) RETURN a.id",
        "allowed",
    ),
    (
        "limits",
        "beyond path cap",
        "MATCH (a:Decision)-[r:supersedes*1..21]->(b:Decision) RETURN a.id",
        "allowed",
    ),
    # -- security: what the masking steps make invisible to the blacklist -------------
    (
        "security",
        "write keyword in a line comment",
        "MATCH (n:Decision) RETURN n.id // DELETE everything",
        "allowed",
    ),
    (
        "security",
        "write keyword in a block comment",
        "MATCH (n:Decision) /* DROP TABLE */ RETURN n.id",
        "allowed",
    ),
    (
        "security",
        "write keyword in a string literal",
        "MATCH (n:Decision) WHERE n.title = 'DELETE' RETURN n.id",
        "allowed",
    ),
    (
        "security",
        "write keyword as a homoglyph",
        "MATCH (n:Decision) \uff24\uff25\uff2c\uff25\uff34\uff25 n",
        "refused",
    ),
    (
        "security",
        "root operation as a homoglyph",
        "\uff2d\uff21\uff34\uff23\uff28 (n:Decision) RETURN n.id",
        "allowed",
    ),
    (
        "security",
        "write keyword inside an identifier",
        "MATCH (n:Decision) WHERE n.deleted_at IS NULL RETURN n.id",
        "allowed",
    ),
    # -- taxonomy: the two refusals are different answers ----------------------------
    (
        "taxonomy",
        "empty query",
        "",
        "refused",
    ),
    (
        "taxonomy",
        "non-mutating unsupported root",
        "CALL db.schema() YIELD value RETURN value",
        "refused",
    ),
    (
        "taxonomy",
        "unsupported clause after a supported root",
        "MATCH (n:Decision) CALL db.index() YIELD value RETURN value",
        "allowed",
    ),
    # -- limits: what injection does and does not touch -------------------------------
    (
        "limits",
        "explicit LIMIT present",
        "MATCH (n:Decision) RETURN n.id LIMIT 5",
        "allowed",
    ),
    (
        "limits",
        "LIMIT only inside a string literal",
        "MATCH (n:Decision) WHERE n.title = 'LIMIT 5' RETURN n.id",
        "allowed",
    ),
    (
        "limits",
        "unbounded variable length",
        "MATCH (a:Decision)-[r:supersedes*]->(b:Decision) RETURN a.id",
        "allowed",
    ),
    ("result", "node projection", "MATCH (n:Decision) RETURN n", "allowed"),
    (
        "result",
        "relationship projection",
        "MATCH (a:Decision)-[r:supersedes]->(b:Decision) RETURN r",
        "allowed",
    ),
    (
        "result",
        "path projection",
        "MATCH path = (a:Decision)-[r:supersedes]->(b:Decision) RETURN path",
        "allowed",
    ),
    ("forbidden_write", "CREATE", "CREATE (n:Decision {id: $id})", "refused"),
    ("forbidden_write", "MERGE", "MERGE (n:Decision {id: $id})", "refused"),
    ("forbidden_write", "SET", "MATCH (n:Decision) SET n.title = $t", "refused"),
    ("forbidden_write", "DELETE", "MATCH (n:Decision) DELETE n", "refused"),
    (
        "forbidden_write",
        "DETACH DELETE",
        "MATCH (n:Decision) DETACH DELETE n",
        "refused",
    ),
    ("forbidden_write", "REMOVE", "MATCH (n:Decision) REMOVE n.title", "refused"),
    ("forbidden_write", "DROP", "DROP TABLE Decision", "refused"),
    ("forbidden_write", "ALTER", "ALTER TABLE Decision ADD note STRING", "refused"),
    (
        "forbidden_write",
        "LOAD CSV",
        "LOAD CSV FROM 'x.csv' AS row RETURN row",
        "refused",
    ),
    ("forbidden_write", "COPY", "COPY Decision FROM 'x.csv'", "refused"),
)


# Every hole a frozen template still carries is bound to a CLOSED DOMAIN OF VALUES, read
# from the pinned commit -- not merely to a kind of name.  Knowing that a hole "is a label"
# says nothing about what may appear there; knowing it ranges over the eleven declared node
# types does.  Business values never appear in a template: they are always $parameters.
HOLE_DOMAINS: dict[str, tuple[str, ...]] = {
    "node_label": (
        "node_type",
        "ntype",
        "nt",
        "from_type",
        "to_type",
        "target_type",
        "from_t",
        "to_t",
        "record.node_type",
        "before_image.node_type",
    ),
    "relationship_type": ("rel_name", "edge_type"),
    "edge_metadata_columns": ("attr_cols",),
    "code_traceability_projection": ("code_traceability_node_projection('n')",),
    "owner_predicate": ("owner_clause",),
    "projection_clause": ("return_clause",),
}

# A builder hole must name the finite source that produces it.  "A clause goes here" is the
# pass-through this replaces; naming the symbol makes the domain reviewable.
BUILDER_SOURCES: dict[str, str] = {
    "edge_metadata_columns": (
        "okto_pulse/core/kg/schema_contract.py:EDGE_METADATA_COLUMNS"
    ),
    "code_traceability_projection": (
        "okto_pulse/core/kg/cypher_templates.py:code_traceability_node_projection"
    ),
    "owner_predicate": (
        "okto_pulse/core/events/handlers/cancellation_decay.py:_owner_clause"
    ),
    "projection_clause": ("okto_pulse/core/kg/primitives.py:_return_clause"),
}


def _domain_members(sources: dict[str, str]) -> dict[str, list[str]]:
    """Enumerate each domain from the baselines, so the authority is values, not vocabulary."""

    ontology = _PureEvaluator(sources, NODE_TYPES_MODULE)
    contract = _PureEvaluator(sources, "okto_pulse/core/kg/schema_contract.py")
    templates = _PureEvaluator(sources, "okto_pulse/core/kg/cypher_templates.py")

    labels = list(_node_types(sources))
    relationships, endpoint_triples = _relationship_domain(sources)
    metadata = (
        contract._value(contract._constants.get("EDGE_METADATA_COLUMNS"), {}) or []
    )
    traceability = templates._imported("CODE_TRACEABILITY_READ_PROPERTIES") or []

    del ontology
    return {
        "node_label": sorted(set(labels)),
        "relationship_type": sorted(set(relationships)),
        "relationship_endpoint_pair": [
            f"{name}({source}->{target})" for name, source, target in endpoint_triples
        ],
        "edge_metadata_columns": sorted(
            str(item[0]) if isinstance(item, (list, tuple)) else str(item)
            for item in metadata
        ),
        "code_traceability_projection": sorted(str(item) for item in traceability),
        "owner_predicate": [],
        "projection_clause": [],
    }


def _domain_of(placeholder: str) -> str | None:
    for domain, members in HOLE_DOMAINS.items():
        if placeholder in members:
            return domain
    return None


def _check_authorities(
    entries: list[dict[str, Any]],
    sources: dict[str, str],
) -> dict[str, Any]:
    """Bind every hole to an enumerated domain, or refuse to freeze."""

    members = _domain_members(sources)
    tally: dict[str, int] = {}
    unbounded: dict[str, str] = {}
    for entry in entries:
        for placeholder in entry.get("placeholders", []):
            domain = _domain_of(placeholder)
            if domain is None:
                unbounded[placeholder] = entry["id"]
                continue
            tally[domain] = tally.get(domain, 0) + 1
    if unbounded:
        listed = ", ".join(
            f"{placeholder!r} (first seen in {entry_id})"
            for placeholder, entry_id in sorted(unbounded.items())
        )
        message = (
            "these template holes are bound to no closed domain: "
            f"{listed}. Give each a domain in HOLE_DOMAINS, with the source that "
            "enumerates it, or resolve the hole."
        )
        raise FreezeError(message)

    empty = [
        domain
        for domain in tally
        if not members.get(domain) and domain not in BUILDER_SOURCES
    ]
    if empty:
        message = (
            f"these domains are used but enumerate nothing: {sorted(empty)}. A domain "
            "without members is a bucket, not an authority."
        )
        raise FreezeError(message)

    authorities = {
        domain: {
            "holes": count,
            "member_count": len(members.get(domain, [])),
            "members": members.get(domain, []),
            "builder_source": BUILDER_SOURCES.get(domain),
        }
        for domain, count in sorted(tally.items())
    }
    if "relationship_type" in authorities:
        # The endpoint pairs belong WITH the relationship names: a type alone does not bound
        # a pattern, because which endpoints it may join is half of what the hole ranges over.
        pairs = members.get("relationship_endpoint_pair", [])
        authorities["relationship_type"]["endpoint_pairs"] = pairs
        authorities["relationship_type"]["endpoint_pair_count"] = len(pairs)
    return authorities


# Queries that MUST plan against the catalog this freezer builds.  If one fails, the catalog
# or the representative triple is wrong and every verdict below is noise -- so the freeze
# stops rather than publishing an inventory of my own mistakes.
CATALOG_SENTINELS: tuple[str, ...] = (
    "MATCH (n:Decision) RETURN n.id",
    "MATCH (a:Decision)-[r:supersedes]->(b:Decision) RETURN a.id",
    "MATCH (a:Decision)<-[r:supersedes]-(b:Decision) RETURN a.id",
    "MATCH (a:Decision)-[r:supersedes]-(b:Decision) RETURN a.id",
    "MATCH (a:Decision)-[r:supersedes*1..2]->(b:Decision) RETURN a.id",
    "MATCH (a:Decision)-[r:supersedes]->(b:Decision) RETURN r",
    "MATCH (n:Decision) WHERE n.id = $id RETURN n.id ORDER BY n.id LIMIT 10",
)


def _check_catalog_sentinels(sources: dict[str, str]) -> None:
    """Prove the oracle before reading it."""

    for probe in CATALOG_SENTINELS:
        phase, error = _try_accept(probe, sources)
        if error is not None:
            message = (
                f"the planning catalog cannot answer a query it must answer: {probe!r} "
                f"failed at {phase} with {error}. Fix the catalog or the representative "
                "triple; until then no verdict in this corpus means anything."
            )
            raise FreezeError(message)


# ---------------------------------------------------------------------------
# Contract behaviour, beyond grammar
# ---------------------------------------------------------------------------
#
# Whether a construct PARSES says nothing about what the endpoint does with it: the same text
# is clamped, has a LIMIT appended, has a layer predicate injected, or is refused for a reason
# that has no grammatical shape at all.  Those answers are facts about the pinned commit, so
# each is stated here WITH the evidence that proves it, and every one is re-verified on freeze.
#
# Four kinds of evidence, each checked differently:
#   constant   -- a module constant, compared to the value recorded here
#   error_code -- a named function raises this code
#   calls      -- a named function calls this symbol
#   literal    -- a named function contains this exact source literal
#   mapping_key -- a named function returns this explicit mapping key
#   lookup_key -- a named function reads this key with mapping.get(...)
CONTRACT_BEHAVIOUR_GROUPS: tuple[str, ...] = (
    "schema_domain",
    "context_shape",
    "defaults_and_limits",
    "security_normalization",
    "error_taxonomy",
    "layer_enforcement",
    "result_envelope",
)

CONTRACT_BEHAVIOURS: tuple[tuple[str, str, str, str, str, Any], ...] = (
    # (group, name, kind, symbol, statement, expected)
    (
        "schema_domain",
        "node label domain",
        "domain",
        "node_types",
        "A public query may name only these node labels.",
        11,
    ),
    (
        "schema_domain",
        "relationship domain",
        "domain",
        "relationship_types",
        "A public query may name only these relationship types.",
        16,
    ),
    (
        "schema_domain",
        "endpoint pairs",
        "domain",
        "relationship_endpoints",
        (
            "A relationship type is only meaningful on its declared endpoint pairs; the "
            "matrix records every pair rather than the type alone."
        ),
        69,
    ),
    (
        "schema_domain",
        "root operations",
        "constant",
        "CYPHER_SUPPORTED_ROOT_OPERATIONS",
        "The first token of a raw query must be one of these.",
        ["MATCH", "OPTIONAL", "UNWIND", "WITH", "RETURN"],
    ),
    (
        "schema_domain",
        "clause vocabulary is published but not enforced",
        "constant",
        "CYPHER_SUPPORTED_CLAUSES",
        (
            "CYPHER_SUPPORTED_CLAUSES is published in the contract document and bound to "
            "CYPHER_WHITELIST, but validate_cypher_read_only consults only the blacklist and "
            "the root operation. A clause outside this list is admitted unless it is the "
            "root token or a blacklisted word."
        ),
        None,
    ),
    (
        "context_shape",
        "related-context directions",
        "constant",
        "RELATED_CONTEXT_DIRECTIONS",
        "Traversal direction is a closed public domain.",
        ["both", "incoming", "outgoing"],
    ),
    (
        "context_shape",
        "related-context depths",
        "constant",
        "RELATED_CONTEXT_DEPTHS",
        "Public traversal depth is 1 or 2, not an open range.",
        [1, 2],
    ),
    (
        "context_shape",
        "graph layers",
        "constant",
        "GRAPH_LAYER_VALUES",
        "The layer selector is a closed domain.",
        ["canonical", "working", "all"],
    ),
    (
        "context_shape",
        "similarity range",
        "constant",
        "SIMILARITY_MIN",
        "Similarity is a unit interval; the minimum is part of the contract.",
        0.0,
    ),
    (
        "context_shape",
        "timestamp normalization at the typed boundary",
        "attribute",
        "_as_iso_timestamp:isoformat",
        (
            "TIMESTAMP columns remain typed inside the graph and are converted to ISO "
            "text only when KGService constructs its public result."
        ),
        None,
    ),
    (
        "context_shape",
        "vector search stays on the structured graph-store port",
        "calls",
        "find_similar_decisions:vector_search",
        (
            "Pulse similarity retrieval calls graph_store.vector_search; it does not "
            "depend on a backend-specific raw Cypher vector function."
        ),
        None,
    ),
    (
        "defaults_and_limits",
        "default row cap",
        "constant",
        "DEFAULT_MAX_ROWS",
        "A caller that passes no max_rows gets this many rows.",
        1000,
    ),
    (
        "defaults_and_limits",
        "maximum row cap",
        "constant",
        "MAX_MAX_ROWS",
        "A caller cannot ask for more rows than this.",
        10000,
    ),
    (
        "defaults_and_limits",
        "default timeout",
        "constant",
        "DEFAULT_TIMEOUT_MS",
        "A caller that passes no timeout gets this budget in milliseconds.",
        5000,
    ),
    (
        "defaults_and_limits",
        "maximum timeout",
        "constant",
        "MAX_TIMEOUT_MS",
        "A caller cannot ask for a longer budget than this.",
        30000,
    ),
    (
        "defaults_and_limits",
        "traversal depth cap",
        "constant",
        "MAX_TRAVERSAL_DEPTH",
        (
            "The depth an UNBOUNDED variable-length path is bounded to. An EXPLICIT upper "
            "bound is left alone: _bound_relationship_segment returns the segment unchanged "
            "once it finds digits after '..', so '*1..21' is not clamped to 20."
        ),
        20,
    ),
    (
        "defaults_and_limits",
        "LIMIT injection",
        "calls",
        "execute_cypher_read_only:_auto_inject_limit",
        (
            "A query with no LIMIT outside comments and string literals has "
            "'LIMIT <max_rows>' appended after a trailing semicolon is stripped."
        ),
        None,
    ),
    (
        "defaults_and_limits",
        "path bounding runs before the layer rewrite",
        "calls",
        "execute_cypher_read_only:_auto_bound_var_length_path",
        (
            "Bounding happens first, so the layer rewrite sees the bounded text and still "
            "refuses it: its guard matches any '*' inside relationship brackets."
        ),
        None,
    ),
    (
        "security_normalization",
        "comments stripped before tokenizing",
        "calls",
        "validate_cypher_read_only:_strip_comments",
        "A keyword hidden in a // or /* */ comment is not a token.",
        None,
    ),
    (
        "security_normalization",
        "NFKC normalization",
        "calls",
        "validate_cypher_read_only:_normalize_unicode",
        "Homoglyphs are folded before the blacklist is applied, so a fullwidth DELETE is one.",
        None,
    ),
    (
        "security_normalization",
        "string literals blanked",
        "calls",
        "validate_cypher_read_only:_strip_string_literals",
        "A blacklisted word inside a quoted literal is data, not a token.",
        None,
    ),
    (
        "error_taxonomy",
        "write attempt",
        "error_code",
        "validate_cypher_read_only:unsafe_cypher",
        "A blacklisted token anywhere in the query is a security refusal.",
        None,
    ),
    (
        "error_taxonomy",
        "unsupported root",
        "error_code",
        "validate_cypher_read_only:unsupported_operation",
        (
            "A non-mutating root operation outside the subset is a DIFFERENT refusal, so a "
            "client can correct the query without it being treated as an attempted write."
        ),
        None,
    ),
    (
        "error_taxonomy",
        "unenforceable canonical filter",
        "error_code",
        "_rewrite_cypher_canonical_only:canonical_filter_unenforceable",
        (
            "Anonymous nodes and variable-length traversal cannot be filtered safely, so the "
            "contract refuses rather than risk leaking working-layer rows."
        ),
        None,
    ),
    (
        "error_taxonomy",
        "missing executor",
        "error_code",
        "execute_cypher_read_only:graph_backend_unconfigured",
        "A missing cypher_executor is a composition error, not an empty result.",
        None,
    ),
    (
        "layer_enforcement",
        "canonical predicate injected per named variable",
        "literal",
        "_canonical_filter_for_vars:.graph_layer = 'canonical'",
        "Each named node variable in a MATCH pattern gains its own layer predicate.",
        None,
    ),
    (
        "layer_enforcement",
        "existing WHERE is parenthesised",
        "literal",
        "_rewrite_cypher_canonical_only:WHERE {canonical_filter} AND ({original_where}) ",
        (
            "The caller's own WHERE is wrapped in parentheses, so an OR in it cannot widen "
            "past the injected layer predicate."
        ),
        None,
    ),
    (
        "layer_enforcement",
        "include_working skips the rewrite",
        "calls",
        "execute_cypher_read_only:_rewrite_cypher_canonical_only",
        "The rewrite is applied only when include_working is false.",
        None,
    ),
    (
        "result_envelope",
        "paired execution",
        "attribute",
        "execute_cypher_read_only:execute_read_only_pair",
        (
            "The paired call is used only when the layer rewrite actually fired and the "
            "executor exposes it; otherwise a single execute_read_only runs."
        ),
        None,
    ),
    (
        "result_envelope",
        "projection applied to every result",
        "calls",
        "execute_cypher_read_only:_apply_canonical_projection",
        (
            "Both the paired and the single path return through the same projection, which "
            "carries the filter mode and the comparison result into the envelope."
        ),
        None,
    ),
    (
        "result_envelope",
        "columns",
        "lookup_key",
        "_apply_canonical_projection:columns",
        "The projection reads the backend column vector used to interpret positional rows.",
        None,
    ),
    (
        "result_envelope",
        "row count",
        "mapping_key",
        "_apply_canonical_projection:row_count",
        "The returned envelope recomputes row_count after canonical projection.",
        None,
    ),
    (
        "result_envelope",
        "truncation",
        "lookup_key",
        "_apply_canonical_projection:truncated",
        (
            "The backend truncation flag is preserved and contributes to whether an "
            "omitted-row count is exact."
        ),
        None,
    ),
    (
        "result_envelope",
        "omitted row count",
        "mapping_key",
        "_apply_canonical_projection:working_omitted_count",
        (
            "The envelope reports omitted working rows together with exactness, source "
            "and returned-window scope fields."
        ),
        None,
    ),
    (
        "result_envelope",
        "omitted row count exactness",
        "mapping_key",
        "_apply_canonical_projection:working_omitted_count_exact",
        "Consumers can distinguish an exact count from a non-observable one.",
        None,
    ),
    (
        "result_envelope",
        "omitted layer counts",
        "mapping_key",
        "_apply_canonical_projection:omitted_layer_counts",
        "Paired execution can expose the omitted rows grouped by graph layer.",
        None,
    ),
)


def _behaviour_evidence(
    kind: str,
    symbol: str,
    expected: Any,
    trees: dict[str, ast.AST],
    evaluators: dict[str, Any],
    sources: dict[str, str],
) -> Any:
    """Prove one behaviour against the pinned source, or raise."""

    if kind == "domain":
        # Through the helpers that already enumerate these, so the schema domain published
        # in the behaviour matrix and the one the freezer validates against are one fact and
        # cannot drift into two.
        if symbol == "node_types":
            members = list(_node_types(sources))
        elif symbol == "relationship_types":
            members = list(_relationship_domain(sources)[0])
        elif symbol == "relationship_endpoints":
            members = [
                f"{name}({source}->{target})"
                for name, source, target in _relationship_domain(sources)[1]
            ]
        else:
            message = f"unknown schema domain {symbol!r}"
            raise FreezeError(message)
        if expected is not None and len(members) != expected:
            message = (
                f"the {symbol} domain now has {len(members)} members, not the {expected} "
                "this matrix records. The schema moved; re-read the corpus."
            )
            raise FreezeError(message)
        return members

    if kind == "constant":
        for module, evaluator in evaluators.items():
            node = evaluator._constants.get(symbol)
            if node is None:
                continue
            value = evaluator._value(node, {})
            if value is None:
                break
            if expected is not None:
                actual = list(value) if isinstance(value, (list, tuple)) else value
                if actual != expected:
                    message = (
                        f"{symbol} in {module} is {actual!r}, not the {expected!r} this "
                        "matrix records. The contract moved; the corpus must be re-read, "
                        "not patched."
                    )
                    raise FreezeError(message)
            return value
        message = (
            f"the constant {symbol} could not be read at the pinned commit, so the "
            "behaviour it is evidence for is unproven."
        )
        raise FreezeError(message)

    function_name, _, detail = symbol.partition(":")
    function = None
    for tree in trees.values():
        function = _function_named(tree, function_name)
        if function is not None:
            break
    if function is None:
        message = (
            f"{function_name} no longer exists at the pinned commit, so the behaviour "
            "recorded against it cannot be proven."
        )
        raise FreezeError(message)

    if kind == "error_code":
        if detail not in _raised_codes(function):
            message = (
                f"{function_name} no longer raises {detail!r}; the error taxonomy this "
                "matrix publishes is not the one the contract implements."
            )
            raise FreezeError(message)
        return detail
    if kind == "attribute":
        # Resolved with getattr and called through a local name, so the symbol never appears
        # as a call target.  Looking for the wrong SHAPE is how a real behaviour gets reported
        # as missing; the evidence is the string the source asks the executor for.
        found = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == detail
            for node in ast.walk(function)
        )
        if not found:
            message = (
                f"{function_name} no longer asks the executor for {detail!r}; the optional "
                "capability recorded against it is unproven."
            )
            raise FreezeError(message)
        return detail
    if kind == "calls":
        if detail not in _called_names(function):
            message = (
                f"{function_name} no longer calls {detail}; the behaviour recorded against "
                "that step is unproven."
            )
            raise FreezeError(message)
        return detail
    if kind == "literal":
        if detail not in ast.unparse(function):
            message = (
                f"{function_name} no longer contains the literal {detail!r}; the text this "
                "matrix says it injects is not the text it injects."
            )
            raise FreezeError(message)
        return detail
    if kind == "mapping_key":
        found = any(
            isinstance(node, ast.Dict)
            and any(
                isinstance(key, ast.Constant) and key.value == detail
                for key in node.keys
            )
            for node in ast.walk(function)
        )
        if not found:
            message = (
                f"{function_name} no longer returns the mapping key {detail!r}; the "
                "result-envelope behaviour recorded against it is unproven."
            )
            raise FreezeError(message)
        return detail
    if kind == "lookup_key":
        found = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == detail
            for node in ast.walk(function)
        )
        if not found:
            message = (
                f"{function_name} no longer reads the mapping key {detail!r}; the "
                "result-envelope behaviour recorded against it is unproven."
            )
            raise FreezeError(message)
        return detail

    message = f"unknown behaviour evidence kind {kind!r}"
    raise FreezeError(message)


def _contract_behaviours(sources: dict[str, str]) -> dict[str, Any]:
    """Every behaviour the raw contract applies beyond deciding whether text is grammatical.

    A grammar matrix alone would say a query is admitted and stop there, while the endpoint
    goes on to clamp it, append a LIMIT, bound its traversal, inject a layer predicate and
    wrap the rows in an envelope.  Each of those is recorded with the evidence that proves
    it at the pin, and an unprovable one fails the freeze rather than being published.
    """

    modules = (
        CONTRACT_MODULE,
        CONTRACT_VOCABULARY_MODULE,
        "okto_pulse/core/kg/schema_contract.py",
        "okto_pulse/core/kg/kg_service.py",
    )
    trees = {module: ast.parse(sources[module]) for module in modules}
    evaluators = {module: _PureEvaluator(sources, module) for module in modules}

    recorded: list[dict[str, Any]] = []
    for group, name, kind, symbol, statement, expected in CONTRACT_BEHAVIOURS:
        evidence = _behaviour_evidence(
            kind, symbol, expected, trees, evaluators, sources
        )
        recorded.append(
            {
                "group": group,
                "name": name,
                "statement": statement,
                "evidence_kind": kind,
                "evidence_symbol": symbol,
                "value": list(evidence) if isinstance(evidence, tuple) else evidence,
            }
        )

    covered = {item["group"] for item in recorded}
    missing = [group for group in CONTRACT_BEHAVIOUR_GROUPS if group not in covered]
    if missing:
        message = (
            f"the raw contract matrix covers no behaviour for {missing}. Every published "
            "group must carry at least one proven fact, or the matrix reads as complete "
            "while saying nothing about that group."
        )
        raise FreezeError(message)
    unknown = sorted(covered - set(CONTRACT_BEHAVIOUR_GROUPS))
    if unknown:
        message = (
            f"these behaviour groups are recorded but not declared: {unknown}. Add them to "
            "CONTRACT_BEHAVIOUR_GROUPS so the completeness check can see them."
        )
        raise FreezeError(message)

    return {
        "groups": list(CONTRACT_BEHAVIOUR_GROUPS),
        "behaviour_count": len(recorded),
        "per_group": {
            group: sum(1 for item in recorded if item["group"] == group)
            for group in CONTRACT_BEHAVIOUR_GROUPS
        },
        "behaviours": recorded,
    }


def _raw_contract(sources: dict[str, str]) -> dict[str, Any]:
    """The versioned grammar the public raw endpoint admits, with this engine's verdict.

    Recorded as a matrix rather than as fingerprints of queries: the raw surface is open by
    construction, so freezing a finite set of texts would describe a different endpoint from
    the one the contract publishes.  What CAN be frozen is which constructs the contract
    names, what it does with each, and what this engine currently does with the same text.

    The contract's answer is COMPUTED from the pinned commit, not declared here.  Each probe
    still carries the disposition its author expected, and a disagreement fails the freeze:
    that cross-check is what caught a construct recorded as refused by the contract that the
    contract in fact admits and merely rewrites.
    """

    pipeline = _check_contract_pipeline(sources)
    blacklist, roots = _contract_vocabulary(sources)
    evaluator = _PureEvaluator(sources, CONTRACT_VOCABULARY_MODULE)
    declared = {
        name: evaluator._value(evaluator._constants[name], {})
        for name in ("CYPHER_SUPPORTED_ROOT_OPERATIONS", "CYPHER_SUPPORTED_CLAUSES")
        if name in evaluator._constants
    }

    probes: list[dict[str, Any]] = []
    disagreements: list[str] = []
    for category, construct, probe, disposition in RAW_CONTRACT_PROBES:
        verdict = _contract_verdict(probe, sources)
        expected = "allowed" if verdict["admitted"] else "refused"
        if expected != disposition:
            disagreements.append(
                f"{category}/{construct}: declared {disposition}, contract says {expected}"
                f" ({verdict['reason']})"
            )
        phase, error = _try_accept(probe, sources)
        probes.append(
            {
                "category": category,
                "construct": construct,
                "probe": probe,
                "contract_disposition": expected,
                "contract_error_code": verdict["error_code"],
                "contract_reason": verdict["reason"],
                "acceptance_phase": phase,
                "engine_verdict": "accepted" if error is None else "refused",
                "error": error,
            }
        )
    if disagreements:
        listed = "; ".join(sorted(disagreements))
        message = (
            "these probes disagree with the contract read at the pinned commit: "
            f"{listed}. Correct the declared disposition -- the pinned source is the "
            "authority, and a matrix that argues with it describes nothing."
        )
        raise FreezeError(message)

    codes: dict[str, int] = {}
    for item in probes:
        code = item["contract_error_code"]
        if code is not None:
            codes[code] = codes.get(code, 0) + 1

    return {
        "declared_tokens": {
            name: list(value) if value is not None else None
            for name, value in declared.items()
        },
        "enforcement": pipeline,
        "blacklist": sorted(blacklist),
        "root_operations": list(roots),
        "probe_count": len(probes),
        "engine_accepted": sum(
            1 for item in probes if item["engine_verdict"] == "accepted"
        ),
        "engine_refused": sum(
            1 for item in probes if item["engine_verdict"] == "refused"
        ),
        "contract_refused": sum(
            1 for item in probes if item["contract_disposition"] == "refused"
        ),
        "contract_error_codes": dict(sorted(codes.items())),
        "note": (
            "contract_disposition is what the public raw endpoint admits, computed from the "
            "pinned commit; engine_verdict is what this engine does with the same text. A "
            "write is refused by the contract and accepted by the engine, because the "
            "internal port needs it."
        ),
        "behaviour": _contract_behaviours(sources),
        "probes": probes,
    }


def build_corpus() -> dict[str, Any]:
    _reset_caches()
    allowlist: dict[str, Any] = (
        json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))
        if ALLOWLIST_PATH.exists()
        else {}
    )
    baselines: dict[str, Any] = {}
    findings: list[Finding] = []
    # Every baseline is read BEFORE any of them is scanned: the plan phase needs the Core
    # schema to build a catalog, and scanning Community first would have asked for a module
    # that baseline does not contain -- reporting every query as unplannable for a reason
    # that lives in this file.
    loaded: dict[str, dict[str, str]] = {}
    for name in BASELINES:
        root = _baseline_root(name)
        pinned = BASELINES[name]["sha"]
        sha = _resolved_sha(root, pinned)
        baselines[name] = {"path_env": BASELINES[name]["env"], "sha": sha}
        loaded[name] = _baseline_sources(root, pinned)
    core_sources = loaded["core"]
    _check_catalog_sentinels(core_sources)
    discovered: dict[str, list[str]] = {
        name: _check_inventory_completeness(name, sources)
        for name, sources in loaded.items()
    }
    global _CLAUSE_EVALUATOR
    _CLAUSE_EVALUATOR = _PureEvaluator(
        core_sources, "okto_pulse/core/kg/cypher_templates.py"
    )
    for name, sources in loaded.items():
        findings.extend(_scan_baseline(name, sources, allowlist, core_sources))
    findings.extend(_public_templates(core_sources))
    _check_duplicates(findings)
    findings.sort(key=lambda item: item.entry_id)

    internal = [item for item in findings if item.surface == "internal_execute"]
    public = [item for item in findings if item.surface != "internal_execute"]

    grouped: dict[str, list[Finding]] = {}
    for item in internal:
        grouped.setdefault(" ".join(item.template.split()), []).append(item)
    ordered_families = sorted(
        grouped.values(),
        key=lambda group: (group[0].path, group[0].line, group[0].template),
    )

    per_module: dict[str, list[int]] = {}
    for group in ordered_families:
        counts_pair = per_module.setdefault(group[0].path, [0, 0])
        counts_pair[0 if group[0].statement_class == "read" else 1] += 1
    mismatched = {
        path: (tuple(pair), AUDITED_FAMILY_COUNTS.get(path))
        for path, pair in sorted(per_module.items())
        if AUDITED_FAMILY_COUNTS.get(path) != tuple(pair)
    }
    if mismatched or len(ordered_families) != sum(
        read + write for read, write in AUDITED_FAMILY_COUNTS.values()
    ):
        message = (
            "the internal family inventory no longer matches the audited table: "
            f"{mismatched or 'total ' + str(len(ordered_families))}"
        )
        raise FreezeError(message)

    entries: list[dict[str, Any]] = []
    for position, group in enumerate(ordered_families, start=1):
        first = group[0]
        entries.append(
            {
                "id": f"I{position:02d}",
                "surface": "internal_family",
                "class": "read" if first.statement_class == "read" else "write",
                "template": " ".join(first.template.split()),
                "template_kind": first.template_kind,
                "placeholders": sorted(
                    {hole for item in group for hole in item.placeholders}
                ),
                "params": sorted({name for item in group for name in item.params}),
                "origins": [
                    {
                        "baseline": item.baseline,
                        "path": item.path,
                        "line": item.line,
                        "symbol": item.symbol,
                        "receiver": item.receiver,
                    }
                    for item in sorted(
                        group, key=lambda entry: (entry.path, entry.line)
                    )
                ],
                "classification": first.classification,
                "acceptance_phase": first.acceptance_phase,
                "parse_error": first.parse_error,
                "reachability": (
                    "preventive"
                    if (first.path, first.line) in PREVENTIVE_ORIGINS
                    else "runtime_current"
                ),
                "expected": _expected_shape(
                    first.template,
                    first.statement_class,
                    first.acceptance_phase,
                    first.parse_error,
                ),
            }
        )
        expected_id = PREVENTIVE_ORIGINS.get((first.path, first.line))
        if expected_id is not None and entries[-1]["id"] != expected_id:
            message = (
                f"{first.path}:{first.line} is the audited preventive family "
                f"{expected_id}, but this inventory numbers it {entries[-1]['id']}; the "
                "ordering and the audit disagree"
            )
            raise FreezeError(message)
    entries.extend(
        {
            "id": f.entry_id,
            "origin": {
                "baseline": f.baseline,
                "path": f.path,
                "line": f.line,
                "symbol": f.symbol,
            },
            "surface": f.surface,
            "receiver": f.receiver,
            "class": f.statement_class,
            "template_kind": f.template_kind,
            "template": f.template,
            "placeholders": f.placeholders,
            "params": f.params,
            "classification": f.classification,
            "acceptance_phase": f.acceptance_phase,
            "parse_error": f.parse_error,
            "expected": _expected_shape(
                f.template,
                f.statement_class,
                f.acceptance_phase,
                f.parse_error,
            ),
        }
        for f in sorted(public, key=lambda item: item.entry_id)
    )

    counts = {}
    for entry in entries:
        for key in (
            f"surface:{entry['surface']}",
            f"class:{entry['class']}",
            f"classification:{entry['classification']}",
            *(
                (f"reachability:{entry['reachability']}",)
                if "reachability" in entry
                else ()
            ),
        ):
            counts[key] = counts.get(key, 0) + 1

    payload = {
        "descriptor": CORPUS_ID,
        "discovered_graph_modules": discovered,
        "excluded_modules": EXCLUDED_MODULES,
        "structural_authorities": _check_authorities(entries, core_sources),
        "public_raw_contract": _raw_contract(core_sources),
        "kg_query_contract_version": QUERY_CONTRACT_VERSION,
        "baselines": baselines,
        "counts": dict(sorted(counts.items())),
        "entry_count": len(entries),
        "entries": entries,
    }
    # Everything the corpus asserts, not just the entries.  The digest covered only entries,
    # so the grammar matrix and the authorities could drift and `--check` would still report
    # the corpus current -- a verification that cannot see half of what it verifies.
    payload["digest"] = hashlib.sha256(
        json.dumps(
            {key: value for key, value in payload.items() if key not in {"digest"}},
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return payload


def main() -> int:
    argument_parser = argparse.ArgumentParser(description=__doc__)
    group = argument_parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true", help="regenerate the corpus")
    group.add_argument("--check", action="store_true", help="verify it is current")
    options = argument_parser.parse_args()

    corpus = build_corpus()
    rendered = json.dumps(corpus, indent=2, sort_keys=False) + "\n"
    if options.write:
        CORPUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        CORPUS_PATH.write_text(rendered, encoding="utf-8")
        print(
            f"wrote {CORPUS_PATH.relative_to(REPO_ROOT)}: {corpus['entry_count']} entries"
        )
        for key, value in corpus["counts"].items():
            print(f"  {key}: {value}")
        return 0
    if not CORPUS_PATH.exists():
        print(f"missing {CORPUS_PATH}", file=sys.stderr)
        return 1
    if CORPUS_PATH.read_text(encoding="utf-8") != rendered:
        print("the frozen corpus is stale; rerun with --write", file=sys.stderr)
        return 1
    print(
        f"corpus current: {corpus['entry_count']} entries, digest {corpus['digest'][:12]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
