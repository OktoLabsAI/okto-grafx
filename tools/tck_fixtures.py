"""Admit typed CREATE fixtures without modifying their original Cypher text.

Schema is inferred from fixture inputs, never from expected answers. No anonymous
label or primary-key values are synthesized. Cases outside this admission contract
remain visible blockers until a separately reviewed fixture/model decision.
"""

from okto_grafx.domain.errors import GrafxError
from okto_grafx.domain.model.schema import is_identifier
from okto_grafx.domain.query.ast import CreateClause, Direction, Literal, Query, UnaryOperation
from okto_grafx.domain.query.parser import parse


def _literal_type(expression):
    if isinstance(expression, UnaryOperation) and expression.operator in {"+", "-"}:
        return _literal_type(expression.operand)
    if not isinstance(expression, Literal):
        raise ValueError("Fixture expression type inference requires literal scalar inputs")
    value = expression.value
    if value is None:
        return None
    types = {bool: "BOOL", int: "INT64", float: "DOUBLE", str: "STRING"}
    if type(value) not in types:
        raise ValueError("Fixture stored type awaits explicit typed admission")
    return types[type(value)]


def infer_fixture_schema(case: dict) -> tuple[str, ...]:
    """Infer single-label, fixed-endpoint scalar fixtures, rejecting ambiguous models."""
    nodes, relationships, endpoints = {}, {}, {}

    def properties(target, pattern):
        for entry in (() if pattern.properties is None else pattern.properties.entries):
            if not is_identifier(entry.key):
                raise ValueError("Fixture property is outside typed identifier policy")
            kind = _literal_type(entry.value)
            previous = target.get(entry.key)
            if previous is not None and kind is not None and previous != kind:
                raise ValueError("Fixture property changes stored type; explicit profile decision required")
            target[entry.key] = previous or kind

    for step in case["steps"]:
        if step["text"] != "having executed:":
            continue
        try:
            statement = parse(step["argument"]["docString"]["content"])
        except GrafxError as exc:
            raise ValueError(f"Fixture grammar not admitted: {exc.code}") from exc
        if not isinstance(statement, Query) or statement.return_clause is not None:
            raise ValueError("Fixture admission requires CREATE-only setup queries")
        aliases = {}
        for clause in statement.ordered_clauses():
            if not isinstance(clause, CreateClause):
                raise ValueError("Fixture admission requires CREATE-only setup queries")
            for path in clause.patterns:
                labels = []
                for node in path.nodes:
                    if len(node.labels) == 1:
                        label = node.labels[0]
                        if not is_identifier(label):
                            raise ValueError("Fixture label is outside typed identifier policy")
                        if node.variable and node.variable in aliases and aliases[node.variable] != label:
                            raise ValueError("Fixture alias changes label")
                    elif not node.labels and node.variable in aliases:
                        label = aliases[node.variable]
                    else:
                        raise ValueError("Unlabeled/multilabel fixture needs explicit architectural review")
                    if node.variable:
                        aliases[node.variable] = label
                    labels.append(label)
                    properties(nodes.setdefault(label, {}), node)
                for index, relationship in enumerate(path.relationships):
                    if len(relationship.types) != 1 or relationship.hop_range_written:
                        raise ValueError("Fixture requires one fixed relationship type")
                    name = relationship.types[0]
                    if not is_identifier(name):
                        raise ValueError("Fixture relationship type is outside identifier policy")
                    pair = (labels[index], labels[index + 1])
                    if relationship.direction == Direction.INCOMING:
                        pair = pair[::-1]
                    elif relationship.direction != Direction.OUTGOING:
                        raise ValueError("Undirected fixture writes are not admitted")
                    if name in endpoints and endpoints[name] != pair:
                        raise ValueError("Fixture relationship spans incompatible endpoint tables")
                    endpoints[name] = pair
                    properties(relationships.setdefault(name, {}), relationship)
    if nodes.keys() & relationships.keys():
        raise ValueError("Fixture node/relationship names share a catalog identifier")

    def columns(definitions):
        if any(kind is None for kind in definitions.values()):
            raise ValueError("All-null fixture column has no inferred stored type")
        return ", ".join(f"{key} {kind}" for key, kind in sorted(definitions.items()))

    ddl = []
    for name, definitions in sorted(nodes.items()):
        # A nullable, unset column only represents the typed table's required layout.
        # It contributes no graph property; the adaptation is visible in the receipt.
        declaration = columns(definitions) if definitions else "_tck_unset BOOL"
        ddl.append(f"CREATE NODE TABLE {name}({declaration})")
    for name, definitions in sorted(relationships.items()):
        source, target = endpoints[name]
        suffix = ", " + columns(definitions) if definitions else ""
        ddl.append(f"CREATE REL TABLE {name}(FROM {source} TO {target}{suffix})")
    return tuple(ddl)
