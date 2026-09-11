"""Isolated native transactions and scan-based observations for reference scenarios."""

from pathlib import Path
from tempfile import TemporaryDirectory
import hashlib

import okto_grafx
from okto_grafx.errors import GrafxError, GrafxParseError, GrafxPlanError
from okto_grafx.domain.query.analysis import analyze
from okto_grafx.domain.query.ast import ProcedureCall, Query, SubqueryClause, UnionQuery
from okto_grafx.domain.query.parser import parse

if __package__:
    from tools.check_opencypher import canonical_value
    from tools.tck_stateful import GraphState, QueryObservation
    from tools.tck_fixtures import infer_fixture_schema
    from tools.tck_errors import compile_error, native_error
    from tools.tck_procedures import procedure_registry
    from tools.tck_values import ReferenceNode, ReferenceRelationship, ReferencePath
else:
    from check_opencypher import canonical_value
    from tck_stateful import GraphState, QueryObservation
    from tck_fixtures import infer_fixture_schema
    from tck_errors import compile_error, native_error
    from tck_procedures import procedure_registry
    from tck_values import ReferenceNode, ReferenceRelationship, ReferencePath


def _reference_result_value(value):
    """Map native result DTOs into independent TCK notation, not engine equality.

    Typed absent properties are stored as NULL and do not count as TCK properties,
    consistently with the separate scan-based effects observer below.
    """
    if type(value) is okto_grafx.NodeValue:
        return ReferenceNode(frozenset(value.labels), tuple(sorted(
            (key, _reference_result_value(item)) for key, item in value.properties.items() if item is not None)))
    if type(value) is okto_grafx.RelationshipValue:
        return ReferenceRelationship(value.label, tuple(sorted(
            (key, _reference_result_value(item)) for key, item in value.properties.items() if item is not None)))
    if type(value) is okto_grafx.PathValue:
        return ReferencePath(tuple(_reference_result_value(node) for node in value.nodes),
                             tuple(_reference_result_value(edge) for edge in value.relationships),
                             tuple("outgoing" if node.identity == edge.source else "incoming"
                                   for node, edge in zip(value.nodes, value.relationships)))
    if isinstance(value, (list, tuple)):
        return tuple(_reference_result_value(item) for item in value)
    if isinstance(value, dict):
        return {key: _reference_result_value(item) for key, item in value.items()}
    return value


def _attempts_write(statement) -> bool:
    """Conservative AST evidence, including rejected nested/union write shapes.

    CALL is conservatively checked durably; a host registration can acquire write
    capability as the procedure profile grows. This does not grant permission.
    Non-query statements are schema operations, not scalar read fast paths.
    """
    pending = [statement]
    while pending:
        node = pending.pop()
        if isinstance(node, UnionQuery):
            pending.extend((node.left, node.right))
        elif isinstance(node, Query):
            if node.writes:
                return True
            for clause in node.ordered_clauses():
                if isinstance(clause, ProcedureCall):
                    return True
                if isinstance(clause, SubqueryClause):
                    pending.append(clause.query)
        else:
            return True
    return False


class NativeScenarioBackend:
    """Own a temporary database; no constructor accepts a production data path.

    Explicit schema DDL is a recorded adapter action, not a rewritten query or
    upstream-conformance pass. General fixture inference remains separate work.
    """

    def __init__(self, *, schema: tuple[str, ...] = (), infer_schema: bool = False,
                 graph_fixtures: dict | None = None):
        self.schema = schema
        self.infer_schema = infer_schema
        self.graph_fixtures = graph_fixtures or {}
        self.named_queries = {}
        self.adaptations = tuple(f"Explicit typed schema: {ddl}" for ddl in schema)
        self.temporary = None
        self.database = None

    def admit(self, case: dict) -> None:
        """Create an isolated schema only after the runner admits all reference steps."""
        if self.database is not None:
            raise ValueError("A native scenario backend cannot be reused")
        self.extensions = procedure_registry(case)
        inference_steps = []
        for step in case.get("steps", ()):
            if step["text"] in {"the binary-tree-1 graph", "the binary-tree-2 graph"}:
                name = step["text"][4:-6]
                fixture = self.graph_fixtures.get(name)
                if fixture is None:
                    raise ValueError(f"Pinned named graph script missing: {name}")
                query = fixture["query"]
                if hashlib.sha256(query.encode("utf-8")).hexdigest() != fixture["sha256_lf"]:
                    raise ValueError(f"Named graph checksum mismatch: {name}")
                self.named_queries[step["text"]] = query
                inference_steps.append({"text": "having executed:", "argument": {"docString": {"content": query}}})
            else:
                inference_steps.append(step)
        if self.infer_schema:
            if self.schema:
                raise ValueError("Choose explicit or inferred fixture schema, not both")
            self.schema = infer_fixture_schema({"steps": inference_steps})
            self.adaptations = tuple(f"Inferred fixture schema: {ddl}" for ddl in self.schema)
        self.temporary = TemporaryDirectory(prefix="grafx-tck-stateful-")
        self.path = Path(self.temporary.name) / "graph"
        self.database = okto_grafx.connect(self.path, extensions=self.extensions)
        if self.schema:
            with self.database.begin("write") as transaction:
                for ddl in self.schema:
                    transaction.execute(ddl)

    def setup(self, query: str, parameters: dict) -> QueryObservation:
        """Run original fixture text, never synthesize missing primary keys."""
        return self.execute(query, parameters, control=False)

    def load_named_fixture(self, step: str) -> QueryObservation:
        """Execute exactly the admitted, checksummed upstream fixture script."""
        return self.setup(self.named_queries[step], {})

    def execute(self, query: str, parameters: dict, *, control: bool) -> QueryObservation:
        """Separate proven parser errors from errors with unproven evaluator phase."""
        try:
            statement = parse(query)
        except GrafxParseError as exc:
            return QueryObservation(error=compile_error(exc))
        attempted_write = _attempts_write(statement)
        try:
            analyze(statement)
        except (GrafxParseError, GrafxPlanError) as exc:
            return QueryObservation(error=compile_error(exc), attempted_write=attempted_write)
        try:
            with self.database.begin("read" if control else "write") as transaction:
                result = transaction.execute(query, parameters)
                observed = QueryObservation(tuple(result.columns), _reference_result_value(result.rows),
                                            attempted_write=attempted_write)
            return observed
        except GrafxError as exc:
            return QueryObservation(error=native_error(exc), attempted_write=attempted_write)

    def snapshot(self) -> GraphState:
        """Read stored rows, not count queries whose evaluator is under test."""
        nodes, relationships, labels, properties = [], [], set(), []
        # This backend exclusively owns the temporary DB; there are no concurrent DDL writers.
        tables = self.database.catalog.catalog.tables()
        with self.database.begin("read") as transaction:
            for table in tables:
                cursor = None
                while True:
                    page = transaction.scan_rows_v1(table.name, limit=256, cursor=cursor)
                    for row in page.rows:
                        identity = (table.kind, table.table_id, row.record_id)
                        if table.kind == "node":
                            nodes.append(identity)
                            labels.add(table.name)
                        else:
                            relationships.append((identity, table.from_table, row.values[0],
                                                  table.to_table, row.values[1]))
                        for column, value in zip(table.columns, row.values, strict=True):
                            if value is not None and not (table.kind == "rel" and column.name in {"_from", "_to"}):
                                properties.append((identity, column.name, canonical_value(value)))
                    cursor = page.next_cursor
                    if cursor is None:
                        break
        return GraphState(tuple(nodes), tuple(relationships), tuple(sorted(labels)), tuple(properties))

    def reopen(self) -> None:
        """Force a new engine open after closing all native handles."""
        self.database.close()
        self.database = None
        self.database = okto_grafx.connect(self.path, extensions=self.extensions)

    def close(self) -> None:
        """Release native handles before deleting this backend's own temporary files."""
        try:
            if self.database is not None:
                self.database.close()
                self.database = None
        finally:
            if self.temporary is not None:
                self.temporary.cleanup()
                self.temporary = None
