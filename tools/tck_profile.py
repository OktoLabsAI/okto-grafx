"""Source-bound architectural review, independent of execution failures.

These candidates implement only boundaries explicitly excluded by the approved
functional-parity plan. They are reviewed before freezing, never inferred from a
test failure or from an unsupported native function.
"""

from dataclasses import fields, is_dataclass
import math

from okto_grafx.domain.errors import GrafxError
from okto_grafx.domain.query.ast import CreateClause, MergeClause, Query, UnionQuery
from okto_grafx.domain.query.parser import parse

if __package__:
    from tools.tck_values import reference_value
    from tools.tck_ledger import ERROR_STEP, case_checksum
else:
    from tck_values import reference_value
    from tck_ledger import ERROR_STEP, case_checksum


def _nonfinite(value):
    if type(value) is float:
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_nonfinite(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_nonfinite(item) for item in value)
    return False


def _walk(value):
    if is_dataclass(value):
        yield value
        for field in fields(value):
            yield from _walk(getattr(value, field.name))
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk(item)


def _creation_divergences(statement):
    if isinstance(statement, UnionQuery):
        for branch in statement.branches():
            yield from _creation_divergences(branch)
        return
    if not isinstance(statement, Query):
        return
    bound = set()
    for clause in statement.ordered_clauses():
        if isinstance(clause, (CreateClause, MergeClause)):
            paths = clause.patterns if isinstance(clause, CreateClause) else (clause.pattern,)
            for path in paths:
                for node in path.nodes:
                    if len(node.labels) > 1:
                        yield "MODEL_MULTILABEL", node.describe()
                    elif not node.labels and (node.variable is None or node.variable not in bound):
                        yield "MODEL_UNLABELED_CREATE", node.describe()
                    if node.variable:
                        bound.add(node.variable)
        else:
            # Existing bindings are references, not fresh schema-free CREATEs.
            for item in _walk(clause):
                variable = getattr(item, "variable", None)
                if isinstance(variable, str):
                    bound.add(variable)
                alias = getattr(item, "alias", None)
                if isinstance(alias, str):
                    bound.add(alias)


def architectural_candidates(case):
    """Return exact counterexamples, or no candidate; uncertainty is never exclusion."""
    negative = any(ERROR_STEP.fullmatch(step["text"]) for step in case["steps"])
    evidence = []
    for number, step in enumerate(case["steps"]):
        if step["text"] == "having executed:" or (not negative and step["text"] == "executing query:"):
            query = step["argument"]["docString"]["content"]
            try:
                statement = parse(query)
            except GrafxError:
                # A parser failure cannot prove a model divergence.
                continue
            for reason, counterexample in _creation_divergences(statement):
                item = {"rule": reason, "step": number, "counterexample": counterexample}
                if item not in evidence:
                    evidence.append(item)
        if "result should be" in step["text"] and "dataTable" in step.get("argument", {}):
            for row in step["argument"]["dataTable"]["rows"][1:]:
                for cell in row["cells"]:
                    try:
                        value = reference_value(cell["value"])
                    except (ValueError, SyntaxError):
                        continue
                    if _nonfinite(value):
                        evidence.append({"rule": "FINITE_ARITHMETIC", "step": number,
                                         "counterexample": cell["value"]})
    return {"id": case["id"], "case_sha256": case_checksum(case), "evidence": evidence}


def review_candidates(report):
    """Prepare a review document; this command does not change ledger inclusion."""
    return {"schema_version": 1, "status": "candidates_only",
            "upstream_revision": report["upstream_revision"],
            "cases": [candidate for case in report["cases"]
                      if (candidate := architectural_candidates(case))["evidence"]]}
