"""Admit typed fixtures without modifying their original Cypher text.

Schema is inferred from fixture inputs or an explicitly admitted initial typed
CREATE action, never from expected answers. No anonymous
label or primary-key values are synthesized. Cases outside this admission contract
remain visible blockers until a separately reviewed fixture/model decision.
"""

from okto_grafx.domain.errors import GrafxError
from okto_grafx.domain.model.schema import is_identifier
from okto_grafx.domain.query.ast import (
    BinaryOperation, CreateClause, DeleteClause, Direction, Literal, MatchClause,
    Property, Query, UnaryOperation, UnwindClause, Variable, WithClause,
)
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


def _match_bindings(clause, nodes, endpoints, aliases, pairs, edge_aliases):
    """Conservatively prove table domains from prior fixture schema, not data/results.

    Direct edge constraints retain endpoint-pair correlation for later reversal;
    they never become a Cartesian product of independently inferred endpoints.
    No property predicate is evaluated to guess a stored type or invent a row.
    """
    if clause.optional:
        raise ValueError("Optional fixture bindings require separate admission")
    for path in clause.patterns:
        domains = []
        for node in path.nodes:
            if len(node.labels) > 1:
                raise ValueError("Multilabel fixture needs explicit architectural review")
            domain = set(node.labels) if node.labels else set(nodes)
            if not domain.issubset(nodes):
                raise ValueError("Fixture MATCH needs previously declared node tables")
            if node.variable in edge_aliases:
                raise ValueError("Fixture alias changes entity kind")
            if node.variable in aliases:
                domain &= aliases[node.variable]
            domains.append(domain)
        constraints = []
        for index, edge in enumerate(path.relationships):
            if edge.hop_range_written or edge.variable_length:
                raise ValueError("Fixture MATCH type proof requires fixed one-hop edges")
            candidates = set()
            for name in edge.types or tuple(endpoints):
                for source, target in endpoints.get(name, ()):
                    if edge.direction in (Direction.OUTGOING, Direction.UNDIRECTED):
                        candidates.add((source, target))
                    if edge.direction in (Direction.INCOMING, Direction.UNDIRECTED):
                        candidates.add((target, source))
            left, right = path.nodes[index].variable, path.nodes[index + 1].variable
            if left is not None and left == right:
                candidates = {(source, target) for source, target in candidates if source == target}
            constraints.append((index, candidates))
            if edge.variable is not None:
                if edge.variable in aliases:
                    raise ValueError("Fixture alias changes entity kind")
                edge_aliases.add(edge.variable)
        changed = True
        while changed:
            before = tuple(frozenset(domain) for domain in domains)
            for index, candidates in constraints:
                compatible = {(source, target) for source, target in candidates
                              if source in domains[index] and target in domains[index + 1]}
                domains[index] &= {source for source, _target in compatible}
                domains[index + 1] &= {target for _source, target in compatible}
            # Multiple occurrences of a variable share one table domain.
            for index, node in enumerate(path.nodes):
                if node.variable is not None:
                    for other, other_node in enumerate(path.nodes):
                        if other_node.variable == node.variable:
                            domains[index] &= domains[other]
            changed = before != tuple(frozenset(domain) for domain in domains)
        for node, domain in zip(path.nodes, domains):
            if node.variable is not None:
                aliases[node.variable] = frozenset(domain)
        for index, candidates in constraints:
            left, right = path.nodes[index].variable, path.nodes[index + 1].variable
            if left is None or right is None:
                continue
            compatible = {(source, target) for source, target in candidates
                          if source in domains[index] and target in domains[index + 1]}
            if (left, right) in pairs:
                compatible &= pairs[left, right]
            pairs[left, right] = compatible
            pairs[right, left] = {(target, source) for source, target in compatible}


def infer_fixture_schema(case: dict, *, allow_initial_create: bool = False) -> tuple[str, ...]:
    """Infer fixtures, optionally a sole CREATE-only action on an otherwise empty graph.

    Action schema comes only from typed input patterns, never RETURN or expected
    answers. Existing fixtures/multiple actions keep their established admission.
    Invalid action grammar is left to execution, not converted to a fixture error.
    """
    nodes, relationships, endpoints = {}, {}, {}
    setup = [step for step in case["steps"] if step["text"] == "having executed:"]
    if setup:
        # A wholly unlabeled fixture now has native storage. No property type,
        # expected result, synthetic label or endpoint DDL is inferred for it.
        try:
            native_queries = [parse(step["argument"]["docString"]["content"]) for step in setup]
        except GrafxError:
            native_queries = []
        if native_queries and all(isinstance(query, Query) for query in native_queries):
            clauses = [clause for query in native_queries for clause in query.ordered_clauses()]
            patterns = [pattern for clause in clauses
                        if isinstance(clause, (CreateClause, MatchClause)) for pattern in clause.patterns]
            if (patterns and all(isinstance(clause, (CreateClause, MatchClause, WithClause, UnwindClause)) for clause in clauses)
                    and all(not node.labels for pattern in patterns for node in pattern.nodes)
                    and any(isinstance(clause, CreateClause) for clause in clauses)):
                return ()
    initial_action = (allow_initial_create
                      and not any(step["text"] == "having executed:" for step in case["steps"])
                      and sum(step["text"] == "executing query:" for step in case["steps"]) == 1)

    def expression_type(expression):
        if isinstance(expression, Property) and isinstance(expression.subject, Variable):
            domain = aliases.get(expression.subject.name)
            if not domain:
                raise ValueError("Fixture property needs a proved node-table binding")
            kinds = {nodes[name].get(expression.key) for name in domain} - {None}
            if len(kinds) != 1:
                raise ValueError("Fixture property has no unambiguous stored type")
            return kinds.pop()
        if isinstance(expression, BinaryOperation) and expression.operator == "+":
            left, right = expression_type(expression.left), expression_type(expression.right)
            if left == right == "STRING":
                return "STRING"
            if left in {"INT64", "DOUBLE"} and right in {"INT64", "DOUBLE"}:
                return "DOUBLE" if "DOUBLE" in (left, right) else "INT64"
            raise ValueError("Fixture addition has no unambiguous stored type")
        return _literal_type(expression)

    def properties(target, pattern):
        for entry in (() if pattern.properties is None else pattern.properties.entries):
            if not is_identifier(entry.key):
                raise ValueError("Fixture property is outside typed identifier policy")
            kind = expression_type(entry.value)
            previous = target.get(entry.key)
            if previous is not None and kind is not None and previous != kind:
                raise ValueError("Fixture property changes stored type; explicit profile decision required")
            target[entry.key] = previous or kind

    for step in case["steps"]:
        action = initial_action and step["text"] == "executing query:"
        if step["text"] != "having executed:" and not action:
            continue
        try:
            statement = parse(step["argument"]["docString"]["content"])
        except GrafxError as exc:
            if action:
                continue
            raise ValueError(f"Fixture grammar not admitted: {exc.code}") from exc
        if action and (not isinstance(statement, Query) or not statement.updating_clauses
                       or any(not isinstance(clause, CreateClause) for clause in statement.ordered_clauses())):
            continue
        if not isinstance(statement, Query) or (statement.return_clause is not None and not action):
            raise ValueError("Fixture admission requires typed setup query clauses without RETURN")
        aliases, pairs, edge_aliases = {}, {}, set()
        for clause in statement.ordered_clauses():
            if isinstance(clause, MatchClause):
                _match_bindings(clause, nodes, endpoints, aliases, pairs, edge_aliases)
                continue
            if isinstance(clause, DeleteClause):
                if any(target.name not in aliases and target.name not in edge_aliases for target in clause.targets):
                    raise ValueError("Fixture DELETE needs an existing entity binding")
                continue
            if not isinstance(clause, CreateClause):
                raise ValueError("Fixture admission requires CREATE/MATCH/DELETE setup clauses")
            for path in clause.patterns:
                labels = []
                for node in path.nodes:
                    if len(node.labels) == 1:
                        label = node.labels[0]
                        if not is_identifier(label):
                            raise ValueError("Fixture label is outside typed identifier policy")
                        if node.variable and node.variable in aliases and aliases[node.variable] != frozenset((label,)):
                            raise ValueError("Fixture alias changes label")
                        domain = frozenset((label,))
                    elif not node.labels and node.variable in aliases:
                        domain = aliases[node.variable]
                    else:
                        raise ValueError("Unlabeled/multilabel fixture needs explicit architectural review")
                    if node.variable:
                        aliases[node.variable] = domain
                    labels.append(domain)
                    for label in domain:
                        properties(nodes.setdefault(label, {}), node)
                for index, relationship in enumerate(path.relationships):
                    if len(relationship.types) != 1 or relationship.hop_range_written:
                        raise ValueError("Fixture requires one fixed relationship type")
                    name = relationship.types[0]
                    if not is_identifier(name):
                        raise ValueError("Fixture relationship type is outside identifier policy")
                    left, right = path.nodes[index].variable, path.nodes[index + 1].variable
                    admitted = pairs.get((left, right)) if left is not None and right is not None else None
                    if admitted is None:
                        admitted = {(source, target) for source in labels[index] for target in labels[index + 1]}
                    else:
                        admitted = {(source, target) for source, target in admitted
                                    if source in labels[index] and target in labels[index + 1]}
                    if not admitted:
                        raise ValueError("Fixture write has no proved endpoint-table pair")
                    if relationship.direction == Direction.INCOMING:
                        admitted = {(target, source) for source, target in admitted}
                    elif relationship.direction != Direction.OUTGOING:
                        raise ValueError("Undirected fixture writes are not admitted")
                    endpoints.setdefault(name, set()).update(admitted)
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
        pairs = sorted(endpoints[name])
        group = "GROUP " if len(pairs) > 1 else ""
        endpoint_text = ", ".join(f"FROM {source} TO {target}" for source, target in pairs)
        suffix = ", " + columns(definitions) if definitions else ""
        ddl.append(f"CREATE REL TABLE {group}{name}({endpoint_text}{suffix})")
    return tuple(ddl)
