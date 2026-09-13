"""Stateful TCK orchestration with independently observed effects and error phases.

The backend owns schema admission, public transactions and detached graph reads.
It must report adaptations; this module never rewrites reference queries or infers
an expected error from the fact that an arbitrary exception happened.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Protocol

if __package__:
    from tools.tck_ledger import ERROR_STEP, EFFECT_KEYS, step_family
    from tools.tck_values import reference_key, reference_value
else:
    from tck_ledger import ERROR_STEP, EFFECT_KEYS, step_family
    from tck_values import reference_key, reference_value


@dataclass(frozen=True)
class GraphState:
    """Detached identities and populated properties, observed in a fresh transaction.

    Properties include owner identity + key + canonical value, so replacements
    count as one removal and one addition. Labels are distinct names in the graph,
    not schema-table declarations. Relationships include identity and endpoints.
    """

    nodes: tuple = ()
    relationships: tuple = ()
    labels: tuple = ()
    properties: tuple = ()

    def counts_since(self, before: GraphState) -> dict[str, int]:
        """Compare actual multiset differences, not totals supplied by a query."""
        counts = {}
        for kind in ("nodes", "relationships", "labels", "properties"):
            old, new = Counter(getattr(before, kind)), Counter(getattr(self, kind))
            counts["+" + kind] = sum((new - old).values())
            counts["-" + kind] = sum((old - new).values())
        return counts

    def equivalent(self, other: GraphState) -> bool:
        """Enumeration order is not graph state; multiplicity and values are."""
        return not any(self.counts_since(other).values())


@dataclass(frozen=True)
class ObservedError:
    """Adapter-proven taxonomy/phase; unknown information must remain unknown."""

    type: str
    phase: str
    detail: str


@dataclass(frozen=True)
class QueryObservation:
    """Materialized outcome after statement success or proven rollback."""

    columns: tuple[str, ...] = ()
    rows: tuple[tuple, ...] = ()
    error: ObservedError | None = None
    # A zero-effect or failed write still needs durable re-open verification.
    attempted_write: bool = False


class ScenarioBackend(Protocol):
    """One isolated database per scenario; no live user graph is admitted."""

    adaptations: tuple[str, ...]

    def admit(self, case: dict) -> None:
        """Preflight all steps/values/fixtures; raise ValueError before any effects."""
        ...

    def setup(self, query: str, parameters: dict) -> QueryObservation:
        """Commit the unchanged fixture query in its own transaction."""
        ...

    def load_named_fixture(self, step: str) -> QueryObservation:
        """Load an unchanged named graph script bound to the pinned inventory."""
        ...

    def execute(self, query: str, parameters: dict, *, control: bool) -> QueryObservation:
        """Execute unchanged query, with atomic commit or proven failure rollback."""
        ...

    def snapshot(self) -> GraphState:
        """Read all observable graph state through a new transaction."""
        ...

    def reopen(self) -> None:
        """Close all handles and durably reopen the same database."""
        ...


def _cells(step: dict) -> list[list[str]]:
    return [[cell["value"] for cell in row["cells"]]
            for row in step["argument"]["dataTable"]["rows"]]


def _value_key(value: object, unordered_lists: bool) -> object:
    return reference_key(value, unordered_lists=unordered_lists)


def run_stateful_case(case: dict, backend: ScenarioBackend) -> dict:
    """Execute admitted steps in order, checking failures and durability independently.

    Only backend admission can return not_run. An execution/observation exception
    is a failed run, never relabeled as an unsupported scenario after side effects.
    Entity/temporal literals will be admitted by their independent oracle codec;
    this initial scalar codec explicitly refuses them before executing any step.
    """
    prepared = []
    try:
        for step in case["steps"]:
            family = step_family(step["text"])
            value = None
            if family == "parameters":
                value = {key: reference_value(cell) for key, cell in _cells(step)}
            elif "result" in family and family != "empty_result":
                table = _cells(step)
                value = (tuple(table[0]), [tuple(reference_value(cell) for cell in row) for row in table[1:]])
            elif family in {"effects", "zero_effects"}:
                value = dict.fromkeys(EFFECT_KEYS, 0)
                if family == "effects":
                    seen = set()
                    for key, count in _cells(step):
                        if key not in value or key in seen or not count.isdigit():
                            raise ValueError("Invalid reference effect counts")
                        seen.add(key)
                        value[key] = int(count)
            prepared.append((family, step, value))
        if not any(family == "query" for family, _, _ in prepared):
            raise ValueError("No operation under test")
        backend.admit(case)
    except (ValueError, SyntaxError) as exc:
        return {"conformance": "not_run", "reason": str(exc)}
    except Exception as exc:
        # Operational/schema initialization failures are failures, not missing
        # runner support, and must not discard the remaining inventory.
        return {"conformance": "failed", "reason": f"Admission failed: {type(exc).__name__}: {exc}",
                "adaptations": list(backend.adaptations)}

    parameters = {}
    observation = None
    before = after = None
    result_checked = True
    mutations = False
    try:
        initial = backend.snapshot()
        for family, step, value in prepared:
            if family == "graph":
                if observation is not None:
                    raise AssertionError("Graph initialization after operation")
            elif family == "procedure_fixture":
                # Registration was validated and installed by backend.admit before queries.
                continue
            elif family == "parameters":
                parameters = value
            elif family == "setup_query":
                setup = backend.setup(step["argument"]["docString"]["content"], parameters)
                if setup.error:
                    raise AssertionError(f"Fixture execution failed: {setup.error}")
                mutations = True
            elif family == "named_fixture":
                setup = backend.load_named_fixture(step["text"])
                if setup.error:
                    raise AssertionError(f"Named fixture execution failed: {setup.error}")
                mutations = True
            elif family in {"query", "control_query"}:
                if not result_checked:
                    raise AssertionError("Previous query has no checked outcome")
                before = backend.snapshot()
                observation = backend.execute(step["argument"]["docString"]["content"], parameters,
                                              control=family == "control_query")
                after = backend.snapshot()
                changed = not after.equivalent(before)
                mutations |= changed or observation.attempted_write
                if observation.error and changed:
                    raise AssertionError("Failed statement leaked graph effects")
                if family == "control_query" and changed:
                    raise AssertionError("Control query changed the graph")
                result_checked = False
            elif family == "expected_error":
                error = ERROR_STEP.fullmatch(step["text"])
                actual = observation.error if observation else None
                if (actual is None or actual.type != error[1]
                        or actual.phase not in {"compile time", "runtime"}
                        or (error[2] != "any time" and actual.phase != error[2])
                        or (error[3] != "*" and actual.detail != error[3])):
                    raise AssertionError(f"Wrong error/phase: {actual!r}; expected {step['text']}")
                result_checked = True
            elif "result" in family:
                if observation is None or observation.error:
                    raise AssertionError(f"Expected rows, got {observation}")
                if family == "empty_result":
                    if observation.rows:
                        raise AssertionError("Expected empty result")
                else:
                    columns, expected = value
                    if observation.columns != columns:
                        raise AssertionError(f"Columns differ: {observation.columns!r} != {columns!r}")
                    unordered_lists = "list_bag" in family
                    actual = [tuple(_value_key(v, unordered_lists) for v in row) for row in observation.rows]
                    wanted = [tuple(_value_key(v, unordered_lists) for v in row) for row in expected]
                    ordered = family.startswith("ordered")
                    if (actual != wanted) if ordered else (Counter(actual) != Counter(wanted)):
                        raise AssertionError("Rows differ from independent reference values")
                result_checked = True
            elif family in {"effects", "zero_effects"}:
                if before is None or after is None or after.counts_since(before) != value:
                    raise AssertionError("Observed graph effects differ from reference counts")
        if not result_checked:
            raise AssertionError("Final query has no checked outcome")
        final = backend.snapshot()
        if mutations or not final.equivalent(initial):
            backend.reopen()
            if not backend.snapshot().equivalent(final):
                raise AssertionError("Durable reopen changed graph state")
    except Exception as exc:
        return {"conformance": "failed", "reason": f"{type(exc).__name__}: {exc}",
                "adaptations": list(backend.adaptations)}
    # An adapted pass is never an upstream pass.
    return {"conformance": "adapted_passed" if backend.adaptations else "passed",
            "reason": None, "adaptations": list(backend.adaptations)}
