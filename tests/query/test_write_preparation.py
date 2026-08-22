"""Nothing is staged until everything that can still refuse has succeeded.

This is an ORDERING rule, not a validation rule, and it is the one C1 was rejected twice for and
C4 once. A write that discovered a bad value halfway through would already have put a row intent
on the transaction, and the refusal would then be a lie about what that transaction contains.

The rule is directly observable here rather than inferred: after a refusal, ``row_intents`` is
empty. That assertion is only possible because the transaction stages rather than writes -- the
heap is untouched at this point by construction -- so these tests check the thing that decides
correctness rather than a proxy for it.

Each test asserts the SPECIFIC refusal, never merely that something refused, so none of them can
pass because a neighbouring guard answered first (amendment A62).
"""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import (
    GrafxPlanError,
    GrafxSpaceRetired,
    GrafxUnsupportedOperation,
    GrafxVectorValidationError,
)
from okto_grafx.domain.model.errors import SchemaMismatchError
from okto_grafx.domain.model.value import ValueType, VectorValue
from okto_grafx.domain.txn.context import RowOperation
from tests.query.stack import QueryStack, TransactionDouble, build_query_stack


@pytest.fixture
def stack() -> QueryStack:
    """Return a database with two people, so a write can be driven off matched rows."""
    built = build_query_stack()
    built.insert("Person", 1, (1, "Ada", 36, "London"), csn=1)
    built.insert("Person", 2, (2, "Grace", 45, "New York"), csn=1)
    return built


def write(
    stack: QueryStack, text: str, parameters: dict[str, object] | None = None
) -> TransactionDouble:
    """Run one statement and return the transaction it staged onto."""
    transaction = stack.transaction(read_lsn=1000)
    stack.engine.execute(text, transaction, parameters)
    return transaction


def refused(
    stack: QueryStack, text: str, parameters: dict[str, object] | None = None
) -> tuple[Exception, TransactionDouble]:
    """Run one statement expecting a refusal, and return it with the transaction."""
    transaction = stack.transaction(read_lsn=1000)
    with pytest.raises(Exception) as failure:  # noqa: PT011 - the class is under test
        stack.engine.execute(text, transaction, parameters)
    return failure.value, transaction


# --- the ordering the module exists for -------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "CREATE (:Person {id: 9, nosuch: 1})",
        "CREATE (:Person {name: 'no key'})",
        "CREATE (:Person {id: 'text'})",
        "MERGE (p:Person {id: 'text'})",
    ],
)
def test_a_refused_write_stages_nothing_at_all(stack: QueryStack, text: str) -> None:
    _failure, transaction = refused(stack, text)
    assert transaction.row_intents == []
    assert transaction.write_partitions == set()


def test_a_refusal_in_the_second_pattern_leaves_the_first_unstaged(
    stack: QueryStack,
) -> None:
    # The strongest form of the rule: a statement whose FIRST pattern is perfectly good and whose
    # second is not must stage neither. Materialising and staging in one pass per pattern would
    # pass every other test in this module and fail this one.
    _failure, transaction = refused(
        stack, "CREATE (:Person {id: 9, name: 'Fine'}), (:Person {id: 'text'})"
    )
    assert transaction.row_intents == []


def test_a_write_that_can_succeed_stages_exactly_what_it_named(stack: QueryStack) -> None:
    transaction = write(stack, "CREATE (:Person {id: 9, name: 'New'})")
    assert len(transaction.row_intents) == 1
    intent = transaction.row_intents[0]
    assert intent.table.name == "Person"
    assert intent.values == (9, "New", None, None)


def test_a_staged_row_asks_for_no_identity_of_its_own(stack: QueryStack) -> None:
    # The commit allocates it. An id taken here and then abandoned would be spent for nothing,
    # and one taken here and reused would put two rows under one identity.
    transaction = write(stack, "CREATE (:Person {id: 9, name: 'New'})")
    assert transaction.row_intents[0].record_id is None


def test_a_write_notes_the_partition_it_touched(stack: QueryStack) -> None:
    # Without this, optimistic validation cannot see the write and two transactions inserting
    # the same row would both commit.
    transaction = write(stack, "CREATE (:Person {id: 9, name: 'New'})")
    assert len(transaction.write_partitions) == 1


def test_two_rows_of_one_table_take_different_partitions(stack: QueryStack) -> None:
    first = write(stack, "CREATE (:Person {id: 100, name: 'A'})")
    second = write(stack, "CREATE (:Person {id: 200, name: 'B'})")
    assert first.write_partitions != second.write_partitions


def test_every_row_of_a_driving_pattern_is_staged(stack: QueryStack) -> None:
    # Each created row takes a key of its own: a primary key names exactly one row, so a pattern
    # that gave every driven row the same literal key would now be refused on its second row.
    transaction = write(stack, "MATCH (p:Person) CREATE (:Person {id: p.id + 100, name: 'Copy'})")
    assert len(transaction.row_intents) == 2
    assert sorted(intent.values[0] for intent in transaction.row_intents) == [101, 102]


def test_a_write_driven_by_no_rows_stages_nothing(stack: QueryStack) -> None:
    transaction = write(
        stack, "MATCH (p:Person) WHERE p.id = 999 CREATE (:Person {id: 9, name: 'C'})"
    )
    assert transaction.row_intents == []


def test_a_statement_reports_what_it_created(stack: QueryStack) -> None:
    transaction = stack.transaction(read_lsn=1000)
    result = stack.engine.execute("CREATE (:Person {id: 9, name: 'New'})", transaction)
    assert result.statistics["rows_created"] == 1


# --- what refuses, and with which refusal -----------------------------------------------------


def test_a_property_the_table_does_not_declare_lists_the_ones_it_does(
    stack: QueryStack,
) -> None:
    failure, _transaction = refused(stack, "CREATE (:Person {id: 9, nosuch: 1})")
    assert isinstance(failure, GrafxPlanError)
    assert failure.details["value"] == "nosuch"
    assert "name" in failure.message


def test_a_column_that_forbids_a_null_refuses_a_row_that_leaves_it_out(
    stack: QueryStack,
) -> None:
    failure, _transaction = refused(stack, "CREATE (:Person {name: 'no key'})")
    assert isinstance(failure, SchemaMismatchError)


def test_a_value_of_the_wrong_type_is_refused_against_the_column(stack: QueryStack) -> None:
    failure, _transaction = refused(stack, "CREATE (:Person {id: 'text'})")
    assert isinstance(failure, SchemaMismatchError)


def test_a_column_the_pattern_omits_takes_a_null_when_the_column_allows_one(
    stack: QueryStack,
) -> None:
    transaction = write(stack, "CREATE (:Person {id: 9, name: 'New'})")
    assert transaction.row_intents[0].values[2:] == (None, None)


def test_a_parameter_carries_its_value_into_the_row(stack: QueryStack) -> None:
    transaction = write(
        stack, "CREATE (:Person {id: $id, name: $name})", {"id": 9, "name": "Grace"}
    )
    assert transaction.row_intents[0].values[:2] == (9, "Grace")


def test_a_parameter_of_the_wrong_type_is_refused_like_a_literal(stack: QueryStack) -> None:
    failure, transaction = refused(stack, "CREATE (:Person {id: $id})", {"id": "text"})
    assert isinstance(failure, SchemaMismatchError)
    assert transaction.row_intents == []


def test_a_write_without_a_write_transaction_is_refused(stack: QueryStack) -> None:
    from okto_grafx.domain.errors import GrafxTransactionStateError

    with pytest.raises(GrafxTransactionStateError) as failure:
        stack.engine.execute("CREATE (:Person {id: 9, name: 'New'})", object())
    assert failure.value.details["field"] == "transaction"


# --- vectors ----------------------------------------------------------------------------------


def test_a_vector_written_as_a_list_of_numbers_is_stored_in_its_own_shape(
    stack: QueryStack,
) -> None:
    transaction = write(
        stack,
        "CREATE (:Chunk {id: 1, layer: 1, embedding: $v})",
        {"v": [1.0, 0.0, 0.0, 0.0]},
    )
    stored = transaction.row_intents[0].values[2]
    assert isinstance(stored, VectorValue)
    assert stored.space_ref == 1
    assert stack.table("Chunk").columns[2].type is ValueType.VECTOR_F32


@pytest.mark.parametrize(
    ("components", "why"),
    [([1.0, 0.0, 0.0], "wrong dimension"), ([float("inf"), 0.0, 0.0, 0.0], "not finite")],
)
def test_a_vector_the_space_refuses_stages_nothing(
    stack: QueryStack, components: list[float], why: str
) -> None:
    # The dimension and finiteness rules belong to the vector subsystem and are asked for rather
    # than repeated, so the refusal carries ITS class and not a lookalike from this component.
    failure, transaction = refused(
        stack, "CREATE (:Chunk {id: 1, embedding: $v})", {"v": components}
    )
    assert isinstance(failure, GrafxVectorValidationError), why
    assert transaction.row_intents == []


def test_a_vector_written_to_a_retired_space_is_refused(stack: QueryStack) -> None:
    stack.vectors.retire_space("minilm_v2")
    failure, transaction = refused(
        stack, "CREATE (:Chunk {id: 1, embedding: $v})", {"v": [1.0, 0.0, 0.0, 0.0]}
    )
    assert isinstance(failure, GrafxSpaceRetired)
    assert transaction.row_intents == []


def test_something_that_is_not_a_sequence_of_numbers_is_refused_as_an_embedding(
    stack: QueryStack,
) -> None:
    failure, _transaction = refused(
        stack, "CREATE (:Chunk {id: 1, embedding: $v})", {"v": "not a vector"}
    )
    assert isinstance(failure, GrafxPlanError)
    assert failure.details["value"] == "embedding"


def test_a_vector_already_in_its_stored_shape_is_carried_through(stack: QueryStack) -> None:
    stored = VectorValue(values=(1.0, 0.0, 0.0, 0.0), space_ref=1)
    transaction = write(stack, "CREATE (:Chunk {id: 1, embedding: $v})", {"v": stored})
    assert transaction.row_intents[0].values[2] is stored


# --- relationships ------------------------------------------------------------------------------


def test_an_edge_between_matched_rows_is_staged_with_both_endpoints(
    stack: QueryStack,
) -> None:
    transaction = write(
        stack,
        "MATCH (a:Person), (b:Person) WHERE a.id = 1 AND b.id = 2 "
        "CREATE (a)-[:Knows {since: 1994}]->(b)",
    )
    assert len(transaction.row_intents) == 1
    intent = transaction.row_intents[0]
    assert intent.table.name == "Knows"
    # Source and target lead the tuple, so their position never depends on the properties.
    assert intent.values[:2] == (1, 2)
    assert intent.values[2] == 1994


def test_an_edge_written_the_other_way_round_stores_its_endpoints_in_schema_order(
    stack: QueryStack,
) -> None:
    transaction = write(
        stack,
        "MATCH (a:Person), (b:Person) WHERE a.id = 1 AND b.id = 2 CREATE (a)<-[:Knows]-(b)",
    )
    assert transaction.row_intents[0].values[:2] == (2, 1)


def test_an_edge_to_a_node_the_same_statement_creates_is_refused(stack: QueryStack) -> None:
    # The identity of a row this statement creates is allocated by the commit, so an edge to it
    # would name a number nobody has issued. The refusal names the remedy.
    failure, transaction = refused(
        stack,
        "MATCH (a:Person) WHERE a.id = 1 CREATE (a)-[:Knows]->(b:Person {id: 9, name: 'New'})",
    )
    assert isinstance(failure, GrafxUnsupportedOperation)
    assert failure.details["value"] == "b"
    assert transaction.row_intents == []


def test_an_edge_can_only_name_rows_this_snapshot_matched() -> None:
    # The endpoint identities come from bound rows, and a bound row is one the snapshot matched,
    # so an edge can never name a row the snapshot cannot see. The heap's endpoint door is still
    # asked -- before anything is staged -- and it is defensive rather than reachable from here;
    # what IS observable is that a snapshot which cannot see an endpoint matches no row at all
    # and therefore stages no edge.
    built = build_query_stack()
    built.insert("Person", 1, (1, "Ada", 36, "London"), csn=1)
    built.insert("Person", 2, (2, "Later", 45, "Paris"), csn=900)
    pattern = (
        "MATCH (a:Person), (b:Person) WHERE a.id = 1 AND b.id = 2 CREATE (a)-[:Knows]->(b)"
    )
    late = built.transaction(read_lsn=1000)
    built.engine.execute(pattern, late)
    assert [intent.values[:2] for intent in late.row_intents] == [(1, 2)]
    early = built.transaction(read_lsn=100)
    built.engine.execute(pattern, early)
    assert early.row_intents == []


def test_a_pattern_may_not_write_the_reserved_endpoint_columns(stack: QueryStack) -> None:
    failure, transaction = refused(
        stack,
        "MATCH (a:Person), (b:Person) WHERE a.id = 1 AND b.id = 2 "
        "CREATE (a)-[:Knows {_from: 7}]->(b)",
    )
    assert isinstance(failure, GrafxPlanError)
    assert failure.details["value"] == "_from"
    assert transaction.row_intents == []


def test_an_edge_is_keyed_on_the_pair_of_endpoints_it_connects(stack: QueryStack) -> None:
    # The KEY is what this component decides; which partition a key lands in is the transaction
    # manager's hash, and asserting the partitions differ would be asserting the absence of a
    # collision in a space of eight (amendment A44: assert the property, not a coincidence).
    from okto_grafx.engine.query_engine import _partition_key

    table = stack.table("Knows")
    forward = _partition_key(table, (1, 2, None))
    backward = _partition_key(table, (2, 1, None))
    assert forward != backward
    assert _partition_key(table, (1, 2, 1994)) == forward


def test_an_edge_notes_a_partition_when_it_is_staged(stack: QueryStack) -> None:
    transaction = write(
        stack,
        "MATCH (a:Person), (b:Person) WHERE a.id = 1 AND b.id = 2 CREATE (a)-[:Knows]->(b)",
    )
    assert len(transaction.write_partitions) == 1


# --- merge --------------------------------------------------------------------------------------


def test_a_merge_that_matches_an_existing_row_stages_nothing(stack: QueryStack) -> None:
    transaction = write(stack, "MERGE (p:Person {id: 1})")
    assert transaction.row_intents == []


def test_a_merge_that_matches_nothing_stages_the_row(stack: QueryStack) -> None:
    transaction = write(stack, "MERGE (p:Person {id: 9})")
    assert len(transaction.row_intents) == 1
    assert transaction.row_intents[0].values[0] == 9


def test_a_merge_reports_which_way_it_went(stack: QueryStack) -> None:
    matched = stack.transaction(read_lsn=1000)
    report = stack.engine.execute("MERGE (p:Person {id: 1})", matched).statistics
    assert report["rows_matched"] == 1
    assert "rows_created" not in report
    created = stack.transaction(read_lsn=1000)
    assert (
        stack.engine.execute("MERGE (p:Person {id: 9})", created).statistics["rows_created"]
        == 1
    )


def test_a_merge_matches_on_the_properties_the_pattern_named_and_no_others(
    stack: QueryStack,
) -> None:
    # Ada carries an age and a city the pattern says nothing about; she is still the row this
    # pattern means, so nothing is created.
    transaction = write(stack, "MERGE (p:Person {id: 1, name: 'Ada'})")
    assert transaction.row_intents == []


def test_a_merge_whose_named_properties_disagree_creates(stack: QueryStack) -> None:
    # The named properties do not all match the stored row, so MERGE takes the CREATE path; the
    # key it would create under is free, so the row is staged.
    transaction = write(stack, "MERGE (p:Person {id: 3, name: 'Somebody else'})")
    assert len(transaction.row_intents) == 1


def test_a_merge_that_would_create_under_a_taken_primary_key_is_refused(
    stack: QueryStack,
) -> None:
    # Named properties disagree on the NAME, so there is no match -- and the CREATE that follows
    # would put a second row under key 1. Kuzu refuses that and so does this engine; the refusal
    # names the constraint and stages nothing.
    from okto_grafx.domain.errors import GrafxQueryError

    failure, transaction = refused(stack, "MERGE (p:Person {id: 1, name: 'Somebody else'})")
    assert isinstance(failure, GrafxQueryError)
    assert failure.details["constraint"] == "primary_key"
    assert transaction.row_intents == []


# --- what still refuses -------------------------------------------------------------------------


def test_a_set_stages_one_update_per_matched_row_and_no_inserts(stack: QueryStack) -> None:
    """SET reaches the transaction as an UPDATE naming the row it replaces, never as an insert."""
    transaction = write(stack, "MATCH (p:Person) SET p.age = 37")
    assert [intent.operation for intent in transaction.row_intents] == [
        RowOperation.UPDATE,
        RowOperation.UPDATE,
    ]
    assert all(intent.reference is not None for intent in transaction.row_intents)
    assert {intent.values[2] for intent in transaction.row_intents} == {37}
    # The properties the statement did not name travel unchanged; an update writes a whole row.
    assert {intent.values[1] for intent in transaction.row_intents} == {"Ada", "Grace"}


def test_a_delete_stages_an_end_carrying_no_values(stack: QueryStack) -> None:
    """A delete ends a version, so it names the row and carries no tuple to write."""
    transaction = write(stack, "MATCH (p:Person) DELETE p")
    assert [intent.operation for intent in transaction.row_intents] == [
        RowOperation.DELETE,
        RowOperation.DELETE,
    ]
    assert all(intent.reference is not None for intent in transaction.row_intents)
    assert all(intent.values == () for intent in transaction.row_intents)


def test_a_set_whose_second_assignment_refuses_stages_nothing(stack: QueryStack) -> None:
    """The hold-until-complete discipline, on the operator that can refuse half way through."""
    failure, transaction = refused(
        stack, "MATCH (p:Person) SET p.age = 37, p.nickname = 'x'"
    )
    assert isinstance(failure, GrafxPlanError)
    assert transaction.row_intents == []


@pytest.mark.parametrize(
    ("text", "detail"),
    [
        ("MATCH (p:Person) SET p.nosuch = 1", "nosuch"),
        ("MATCH (p:Person) SET p.age = 'old'", "age"),
        ("MATCH (p:Person) SET p.id = null", "id"),
    ],
)
def test_a_set_that_could_never_be_stored_refuses_before_the_missing_door(
    stack: QueryStack, text: str, detail: str
) -> None:
    # The distinguishing assertion: the refusal is about the VALUE, not about the missing door.
    # If validation happened after the door was checked, every one of these would report the
    # door and the caller would never learn what was wrong with the value.
    failure, _transaction = refused(stack, text)
    assert isinstance(failure, GrafxPlanError)
    assert failure.details["value"] == detail
