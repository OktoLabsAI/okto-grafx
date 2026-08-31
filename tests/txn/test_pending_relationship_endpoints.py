"""Relationship endpoints that name a node the same transaction is still creating.

An edge stores the RECORD IDENTITIES of its two endpoints (W5c), and a node's identity does not
exist until the commit writes the row. So a transaction that creates a node and an edge to it in
one go has nothing to put in the endpoint slot at staging time -- nothing durable, anyway. It
puts the node's PRIVATE identity there instead, and the commit path turns that promise into a
number before anything is written.

What these tests hold, in order: the private identity is handed back to whoever staged the insert;
a promise that cannot be kept is refused BEFORE the commit window opens, which is proven by making
the window itself fail the test if it is ever reached; a promise that can be kept is resolved to
the identity the row actually receives, and survives a reopen; and no private token is left
anywhere a durable value is expected -- not in a stored row, not in a row reference, not in the
budget's arithmetic.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from okto_grafx.domain.errors import (
    GrafxTransactionBudgetExceeded,
    GrafxTransactionStateError,
)
from okto_grafx.domain.model.schema import ColumnDef, TableDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.txn.context import (
    PendingRowRef,
    RowIntent,
    RowOperation,
    TransactionContext,
    TransactionMode,
    TransactionState,
)
from okto_grafx.domain.txn.intents import (
    PendingEndpoint,
    plan_relationship_endpoints,
)
from okto_grafx.domain.txn.snapshot import Snapshot
from okto_grafx.domain.page.layout import MAX_U64
from okto_grafx.engine.heap_store import FIRST_RECORD_ID
from okto_grafx.engine.txn_manager import TransactionManager
from okto_grafx.engine.verifier import Verifier
from txn_support import Stack, build_stack

PERSON_ID: int = 1
COMPANY_ID: int = 2
WORKS_AT_ID: int = 3
CHAIN_ID: int = 4


def _person(table_id: int = PERSON_ID, name: str = "Person") -> TableDef:
    """Return a node table with one column, so a row is one value."""
    return TableDef(
        table_id=table_id,
        name=name,
        kind="node",
        columns=(ColumnDef(name="id", type=ValueType.INT64, nullable=False),),
        primary_key="id",
        from_table=None,
        to_table=None,
    )


def _works_at(
    table_id: int = WORKS_AT_ID,
    *,
    source: str = "Person",
    target: str = "Company",
) -> TableDef:
    """Return a relationship table; the two endpoint columns are prepended for us."""
    return TableDef(
        table_id=table_id,
        name="WorksAt",
        kind="rel",
        columns=(ColumnDef(name="since", type=ValueType.INT64, nullable=False),),
        primary_key=None,
        from_table=source,
        to_table=target,
    )


def _registered(stack: Stack, *tables: TableDef) -> tuple[TableDef, ...]:
    """Put the tables in the catalog of a built stack and flush it."""
    for table in tables:
        stack.catalog.catalog.add_table(table)
    stack.catalog.save()
    stack.pool.flush(stack.catalog.file)
    stack.pool.flush(stack.heap.file)
    return tables


def _context(
    *, txn_id: int = 7, max_transaction_bytes: int | None = None
) -> TransactionContext:
    """Return a bare context, for the claims that need no stack at all."""
    return TransactionContext(
        txn_id=txn_id,
        mode=TransactionMode.WRITE,
        snapshot=Snapshot(0),
        epoch=0,
        owner=object(),
        page_staging_capability=object(),
        max_transaction_bytes=max_transaction_bytes,
    )


def _declare(stack: Stack, transaction: TransactionContext, *tables: TableDef) -> None:
    """Declare write interest, which optimistic validation requires of any writer."""
    for table in tables:
        transaction.note_write(stack.manager.partition_of(table.table_id, b"1"))


def _stored(stack: Stack, table: TableDef) -> list[tuple[object, ...]]:
    """Return every stored row of the table, whatever its visibility."""
    return [version.values for _ref, version in stack.heap.scan_all(table)]


def test_staging_an_insert_hands_back_the_identity_it_emitted() -> None:
    """The caller receives the exact live reference, and receives it either way.

    An explicit ``record_id`` names the DURABLE identity the row will carry. The pending
    reference names the staged INTENT, which is a different thing and is still the only handle a
    later statement of this transaction has on it -- so it comes back in both cases.
    """
    transaction = _context()
    table = _person()

    issued = transaction.stage_row_insert(table, (1,))
    chosen = transaction.stage_row_insert(table, (2,), record_id=99)

    assert isinstance(issued, PendingRowRef) and isinstance(chosen, PendingRowRef)
    # The exact object, not merely an equal one: ownership is proven by identity.
    assert transaction.row_intents[0].reference is issued
    assert transaction.row_intents[1].reference is chosen
    assert transaction.owns_pending_row_ref(issued)
    assert transaction.owns_pending_row_ref(chosen)
    assert issued != chosen


def test_a_pending_identity_is_refused_outside_a_relationship_endpoint() -> None:
    """Only the two endpoint columns of a relationship insert may carry a promise."""
    transaction = _context()
    person = _person()
    works_at = _works_at()
    issued = transaction.stage_row_insert(person, (1,))

    # In a node row, where there is no endpoint column at all.
    with pytest.raises(GrafxTransactionStateError) as in_a_node:
        transaction.stage_row_insert(person, (issued,))
    assert in_a_node.value.details["field"] == "values"

    # In a property column of a relationship, past the two endpoints.
    with pytest.raises(GrafxTransactionStateError) as in_a_property:
        transaction.stage_row_insert(works_at, (1, 2, issued))
    assert in_a_property.value.details["field"] == "values"

    # And in an UPDATE, whose row already exists and whose endpoints are already durable.
    stored = transaction.stage_row_insert(works_at, (1, 2, 2020))
    with pytest.raises(GrafxTransactionStateError) as in_an_update:
        transaction.stage_row_update(works_at, stored, (issued, 2, 2020))
    assert in_an_update.value.details["field"] == "values"


def test_staging_refuses_an_endpoint_this_transaction_never_issued() -> None:
    """A reference from elsewhere is refused where it is offered, not later."""
    transaction = _context()
    works_at = _works_at()

    foreign = PendingRowRef(txn_id=transaction.txn_id + 1, table_id=PERSON_ID, token=-1)
    with pytest.raises(GrafxTransactionStateError) as raised:
        transaction.stage_row_insert(works_at, (foreign, 2, 2020))
    assert raised.value.details["field"] == "pending_row_reference"


@pytest.mark.parametrize(
    "case",
    ("clone", "orphan", "cancelled", "endpoint_is_an_edge", "wrong_side", "names_a_later_insert"),
)
def test_commit_refuses_an_endpoint_it_cannot_keep_before_the_commit_window(
    stack: Stack,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """Every way an endpoint can point nowhere is refused while nothing has been written.

    The commit window is made to FAIL the test if it is ever reached, so this asserts the
    ordering itself rather than only the refusal: a proof that runs after the lease is held is a
    proof that runs after the transaction has begun to spend the device.
    """
    person, company, works_at = _registered(
        stack, _person(), _person(COMPANY_ID, "Company"), _works_at()
    )
    transaction = stack.manager.begin("write")
    source = transaction.stage_row_insert(person, (1,))
    target = transaction.stage_row_insert(company, (1,))

    if case == "clone":
        # Equal in every field and a different object: a copy of a promise is not the promise.
        endpoint = PendingRowRef(
            txn_id=source.txn_id, table_id=source.table_id, token=source.token
        )
        transaction.row_intents.append(
            RowIntent(table=works_at, values=(endpoint, target, 2020), reference=None)
        )
    elif case == "orphan":
        endpoint = PendingRowRef(txn_id=transaction.txn_id, table_id=PERSON_ID, token=-999)
        transaction.row_intents.append(
            RowIntent(table=works_at, values=(endpoint, target, 2020), reference=None)
        )
    elif case == "cancelled":
        # Staged legitimately, then the node it names is deleted in the same transaction.
        transaction.stage_row_insert(works_at, (source, target, 2020))
        transaction.stage_row_delete(person, source)
    elif case == "endpoint_is_an_edge":
        edge = transaction.stage_row_insert(works_at, (source, target, 2020))
        transaction.row_intents.append(
            RowIntent(table=works_at, values=(edge, target, 2021), reference=None)
        )
    elif case == "wrong_side":
        # The company reference offered as the SOURCE, whose side declares Person.
        transaction.row_intents.append(
            RowIntent(table=works_at, values=(target, target, 2020), reference=None)
        )
    else:
        # The edge placed BEFORE the node it names. Order needs no rule of its own: the walk has
        # not reached that insert yet, so the endpoint is indistinguishable from one naming
        # nothing at all, and both are refused as unstaged.
        later = transaction.stage_row_insert(person, (2,))
        transaction.row_intents.insert(
            0, RowIntent(table=works_at, values=(later, target, 2020), reference=None)
        )

    _declare(stack, transaction, person, company, works_at)

    def commit_window_was_reached(_manager: TransactionManager) -> object:
        pytest.fail("endpoint validation ran after the commit window opened")

    monkeypatch.setattr(TransactionManager, "_hold_lease", commit_window_was_reached)
    with pytest.raises(GrafxTransactionStateError) as raised:
        stack.manager.commit(transaction)

    assert raised.value.details["field"] == "pending_row_reference"
    assert transaction.state is TransactionState.ACTIVE
    assert transaction.row_refs == []
    assert _stored(stack, works_at) == []
    assert _stored(stack, person) == []
    stack.manager.rollback(transaction)


def test_two_pending_endpoints_resolve_to_the_identities_their_nodes_receive(
    stack: Stack,
    database_root: Path,
) -> None:
    """The edge lands carrying the two ids the commit actually gave its endpoints."""
    person, company, works_at = _registered(
        stack, _person(), _person(COMPANY_ID, "Company"), _works_at()
    )
    transaction = stack.manager.begin("write")
    source = transaction.stage_row_insert(person, (10,))
    target = transaction.stage_row_insert(company, (20,))
    transaction.stage_row_insert(works_at, (source, target, 2020))
    _declare(stack, transaction, person, company, works_at)

    stack.manager.commit(transaction)

    # The ids are read back from the ROWS, not from what the planner believed it chose.
    people = {version.record_id for _ref, version in stack.heap.scan_all(person)}
    companies = {version.record_id for _ref, version in stack.heap.scan_all(company)}
    assert len(people) == 1 and len(companies) == 1
    edges = _stored(stack, works_at)
    assert edges == [(people.pop(), companies.pop(), 2020)]

    reopened = build_stack(database_root, owner_id="pending-endpoints-reopen")
    reader = reopened.manager.begin("read")
    assert [
        version.values for _ref, version in reopened.heap.scan(works_at, reader.snapshot)
    ] == edges
    assert Verifier(
        reopened.pool,
        reopened.metrics,
        heap=reopened.heap,
        catalog=reopened.catalog,
    ).verify("all").findings == ()
    reopened.manager.rollback(reader)


def test_one_committed_endpoint_and_one_pending_endpoint_both_land(
    stack: Stack,
) -> None:
    """A mixed edge is the ordinary case: one end exists, the other is being created now."""
    person, company, works_at = _registered(
        stack, _person(), _person(COMPANY_ID, "Company"), _works_at()
    )
    first = stack.manager.begin("write")
    first.stage_row_insert(person, (10,))
    _declare(stack, first, person)
    stack.manager.commit(first)
    established = next(version.record_id for _ref, version in stack.heap.scan_all(person))

    second = stack.manager.begin("write")
    target = second.stage_row_insert(company, (20,))
    second.stage_row_insert(works_at, (established, target, 2021))
    _declare(stack, second, company, works_at)
    stack.manager.commit(second)

    company_id = next(version.record_id for _ref, version in stack.heap.scan_all(company))
    assert _stored(stack, works_at) == [(established, company_id, 2021)]


def test_a_planned_identity_never_collides_with_one_the_same_batch_chose(
    stack: Stack,
) -> None:
    """An insert that names its own id pushes the counter past it, so the next plan clears it."""
    (person,) = _registered(stack, _person())
    transaction = stack.manager.begin("write")
    transaction.stage_row_insert(person, (1,), record_id=5)
    transaction.stage_row_insert(person, (2,))
    _declare(stack, transaction, person)

    stack.manager.commit(transaction)

    identities = sorted(version.record_id for _ref, version in stack.heap.scan_all(person))
    assert identities == [5, 6]
    assert len(set(identities)) == len(identities)


def test_an_abandoned_attempt_spends_no_identity(stack: Stack) -> None:
    """Planning reads the counter and does not move it, so a rollback leaves it where it was."""
    person, company, works_at = _registered(
        stack, _person(), _person(COMPANY_ID, "Company"), _works_at()
    )
    before = stack.heap.next_record_id(person)

    transaction = stack.manager.begin("write")
    source = transaction.stage_row_insert(person, (1,))
    target = transaction.stage_row_insert(company, (1,))
    transaction.stage_row_insert(works_at, (source, target, 2020))
    _declare(stack, transaction, person, company, works_at)
    stack.manager.rollback(transaction)

    assert stack.heap.next_record_id(person) == before
    assert _stored(stack, person) == []
    assert _stored(stack, works_at) == []


def test_the_budget_charges_a_pending_edge_what_the_stored_edge_costs() -> None:
    """A promise is measured as the id it becomes, so the accounting is exact, not approximate.

    A budget must be configured for any of this to be measured at all -- without one the whole
    calculation short-circuits to zero, and comparing zero to zero is a check that cannot fail.
    The comparison is against the SAME row with concrete endpoints rather than a constant: the
    claim is that the two agree, and a hard-coded size would let the encoding change without the
    test noticing they had come apart.
    """
    works_at = _works_at()
    budget = 1 << 20

    concrete = _context(max_transaction_bytes=budget)
    concrete_bytes = concrete._row_payload_bytes(works_at, (1, 2, 2020))
    assert concrete_bytes > 0

    promised = _context(max_transaction_bytes=budget)
    source = promised.stage_row_insert(_person(), (1,))
    target = promised.stage_row_insert(_person(COMPANY_ID, "Company"), (1,))
    before = promised._staged_payload_bytes
    promised.stage_row_insert(works_at, (source, target, 2020))

    assert promised._row_payload_bytes(works_at, (source, target, 2020)) == concrete_bytes
    # And staging really charged it, so the budget sees the edge rather than skipping it.
    assert promised._staged_payload_bytes - before == concrete_bytes


def test_each_table_plans_its_identities_from_its_own_counter(stack: Stack) -> None:
    """Identities are per table, so two tables inserted together both start at the first one.

    One shared cursor would give the second table's row the number 2 and still look correct --
    the edge would name whatever the rows received. The numbers themselves are the assertion.
    """
    person, company, works_at = _registered(
        stack, _person(), _person(COMPANY_ID, "Company"), _works_at()
    )
    transaction = stack.manager.begin("write")
    source = transaction.stage_row_insert(person, (10,))
    target = transaction.stage_row_insert(company, (20,))
    transaction.stage_row_insert(works_at, (source, target, 2020))
    _declare(stack, transaction, person, company, works_at)

    stack.manager.commit(transaction)

    assert [version.record_id for _ref, version in stack.heap.scan_all(person)] == [
        FIRST_RECORD_ID
    ]
    assert [version.record_id for _ref, version in stack.heap.scan_all(company)] == [
        FIRST_RECORD_ID
    ]
    assert _stored(stack, works_at) == [(FIRST_RECORD_ID, FIRST_RECORD_ID, 2020)]


def test_an_implicit_identity_clears_an_explicit_one_chosen_later(stack: Stack) -> None:
    """The batch's own choices are reserved before any implicit identity is planned.

    Planned in one pass, the first row takes the counter's next number and the second row then
    asks for that very number by name -- and both are correct locally while the table ends up
    with two rows under one identity. The explicit choice is therefore collected first.
    """
    (person,) = _registered(stack, _person())
    transaction = stack.manager.begin("write")
    transaction.stage_row_insert(person, (10,))
    transaction.stage_row_insert(person, (20,), record_id=FIRST_RECORD_ID)
    _declare(stack, transaction, person)

    stack.manager.commit(transaction)

    identities = sorted(version.record_id for _ref, version in stack.heap.scan_all(person))
    assert len(identities) == len(set(identities)) == 2
    assert FIRST_RECORD_ID in identities


def test_two_inserts_of_one_table_cannot_name_the_same_identity(
    stack: Stack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repeated explicit identity is refused before the commit window, with nothing written."""
    (person,) = _registered(stack, _person())
    transaction = stack.manager.begin("write")
    transaction.stage_row_insert(person, (10,), record_id=5)
    transaction.stage_row_insert(person, (20,), record_id=5)
    _declare(stack, transaction, person)

    def commit_window_was_reached(_manager: TransactionManager) -> object:
        pytest.fail("identity validation ran after the commit window opened")

    monkeypatch.setattr(TransactionManager, "_hold_lease", commit_window_was_reached)
    with pytest.raises(GrafxTransactionStateError) as raised:
        stack.manager.commit(transaction)

    assert raised.value.details["field"] == "record_id"
    assert _stored(stack, person) == []
    stack.manager.rollback(transaction)


def _chain() -> TableDef:
    """Return a relationship table that declares ITSELF as its source side.

    Nothing in the schema forbids it, and that is the point: for an ordinary table the side
    declaration already rejects an edge as an endpoint, so the rule that an endpoint must be a
    NODE looks redundant. Here the declaration agrees with the edge, and only that rule is left.
    """
    return TableDef(
        table_id=CHAIN_ID,
        name="Chain",
        kind="rel",
        columns=(ColumnDef(name="since", type=ValueType.INT64, nullable=False),),
        primary_key=None,
        from_table="Chain",
        to_table="Company",
    )


def test_the_planner_refuses_an_endpoint_that_names_another_edge() -> None:
    """An edge as an endpoint is refused even where the side declaration permits it."""
    chain = _chain()
    reference = PendingRowRef(txn_id=7, table_id=CHAIN_ID, token=-1)
    intents = (
        RowIntent(table=chain, values=(1, 2, 2020), reference=reference),
        RowIntent(table=chain, values=(reference, 2, 2021), reference=None),
    )

    # The premise, asserted: the side agrees, so nothing but the kind rule can refuse this.
    assert chain.from_table == chain.name

    with pytest.raises(GrafxTransactionStateError) as raised:
        plan_relationship_endpoints(intents, txn_id=7, owns=lambda _reference: True)

    assert raised.value.details["field"] == "pending_row_reference"


def test_the_planner_refuses_an_endpoint_its_transaction_no_longer_holds() -> None:
    """Ownership is a rule of the planner, injected rather than inferred from the sequence.

    The two calls differ in NOTHING but the answer ``owns`` gives, so the rule is the only thing
    that can account for the difference. Held, the plan is made; not held, it is refused -- and
    an endpoint that is present in the sequence is exactly the case that being absent cannot
    stand in for.
    """
    person, works_at = _person(), _works_at()
    reference = PendingRowRef(txn_id=7, table_id=PERSON_ID, token=-1)
    intents = (
        RowIntent(table=person, values=(1,), reference=reference),
        RowIntent(table=works_at, values=(reference, 2, 2020), reference=None),
    )

    assert plan_relationship_endpoints(
        intents, txn_id=7, owns=lambda _reference: True
    ) == (PendingEndpoint(relationship_position=1, slot=0, endpoint_position=0),)

    with pytest.raises(GrafxTransactionStateError) as raised:
        plan_relationship_endpoints(intents, txn_id=7, owns=lambda _reference: False)
    assert raised.value.details["field"] == "pending_row_reference"

    # The other half of the same rule: a reference minted by another transaction.
    foreign = (
        intents[0],
        RowIntent(table=works_at, values=(reference, 2, 2020), reference=None),
    )
    with pytest.raises(GrafxTransactionStateError):
        plan_relationship_endpoints(foreign, txn_id=8, owns=lambda _reference: True)


def test_the_heap_boundary_refuses_a_row_that_still_carries_a_promise(stack: Stack) -> None:
    """The guard in front of the heap, the indexes and the log is stated, not inherited.

    Nothing should ever reach it -- the values came from intents this commit already resolved --
    so it is exercised directly rather than through a commit. A boundary no test can reach is a
    boundary no test can hold, and this one exists precisely for the change that has not been
    written yet.
    """
    (person,) = _registered(stack, _person())
    works_at = _works_at()
    transaction = stack.manager.begin("write")
    reference = transaction.stage_row_insert(person, (1,))

    leaked = (RowIntent(table=works_at, values=(reference, 1, 2020), reference=None),)
    with pytest.raises(GrafxTransactionStateError) as raised:
        stack.manager._refuse_unresolved_intents(transaction, leaked)
    assert raised.value.details["field"] == "values"

    unplanned = (RowIntent(table=person, values=(1,), reference=None),)
    with pytest.raises(GrafxTransactionStateError) as missing:
        stack.manager._refuse_unresolved_intents(transaction, unplanned)
    assert missing.value.details["field"] == "record_id"

    negative = (
        RowIntent(table=works_at, values=(0, 1, 2020), record_id=1, reference=None),
    )
    with pytest.raises(GrafxTransactionStateError) as unstored:
        stack.manager._refuse_unresolved_intents(transaction, negative)
    assert unstored.value.details["field"] == "endpoint"

    stack.manager.rollback(transaction)


def test_an_endpoint_whose_insert_was_discarded_is_refused(
    stack: Stack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A discarded insert put back by hand cannot lend its identity to an edge.

    A discarded statement takes its inserts AND its identities away. Reinstating the intent is
    all a caller holding the public staging list has to do, and this fixes what the whole commit
    does about it: refuse, before the window, with nothing written. The refusal itself comes from
    the intent-level ownership check that already guards every staged reference -- the endpoint
    rule of the same name is held separately, over the pure planner, where it can be isolated.
    """
    person, company, works_at = _registered(
        stack, _person(), _person(COMPANY_ID, "Company"), _works_at()
    )
    transaction = stack.manager.begin("write")
    mark = transaction.staging_mark()
    source = transaction.stage_row_insert(person, (1,))
    transaction.discard_since(mark)
    assert not transaction.owns_pending_row_ref(source)

    target = transaction.stage_row_insert(company, (1,))
    transaction.row_intents.insert(
        0, RowIntent(table=person, values=(1,), reference=source)
    )
    transaction.row_intents.append(
        RowIntent(table=works_at, values=(source, target, 2020), reference=None)
    )
    _declare(stack, transaction, person, company, works_at)

    def commit_window_was_reached(_manager: TransactionManager) -> object:
        pytest.fail("endpoint validation ran after the commit window opened")

    monkeypatch.setattr(TransactionManager, "_hold_lease", commit_window_was_reached)
    with pytest.raises(GrafxTransactionStateError) as raised:
        stack.manager.commit(transaction)

    assert raised.value.details["field"] == "pending_row_reference"
    assert _stored(stack, works_at) == []
    stack.manager.rollback(transaction)


@pytest.mark.parametrize(
    "tampered",
    (True, 1.0, "1", 0, -1, MAX_U64, MAX_U64 + 1),
    ids=("bool", "float", "string", "zero", "negative", "marker", "past_marker"),
)
def test_a_tampered_identity_is_refused_before_the_commit_window(
    stack: Stack,
    monkeypatch: pytest.MonkeyPatch,
    tampered: object,
) -> None:
    """The staging door validates what it is handed; the list it appends to is public.

    So an identity can be replaced AFTER it was staged, and the commit is the only thing left
    between that and the heap. A negative one matters most: that is exactly the shape of a
    private token, and the marker itself is the one number a counter cannot advance past.
    """
    (person,) = _registered(stack, _person())
    transaction = stack.manager.begin("write")
    transaction.stage_row_insert(person, (1,), record_id=FIRST_RECORD_ID)
    transaction.row_intents[0] = replace(transaction.row_intents[0], record_id=tampered)
    _declare(stack, transaction, person)

    def commit_window_was_reached(_manager: TransactionManager) -> object:
        pytest.fail("identity validation ran after the commit window opened")

    monkeypatch.setattr(TransactionManager, "_hold_lease", commit_window_was_reached)
    with pytest.raises(GrafxTransactionStateError) as raised:
        stack.manager.commit(transaction)

    assert raised.value.details["field"] == "record_id"
    assert _stored(stack, person) == []
    stack.manager.rollback(transaction)


def test_an_identity_below_the_durable_floor_is_refused_even_when_it_is_a_gap(
    stack: Stack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing row does not prove that an id below the floor is unowned.

    Durable leasing advances the shared floor before a process uses its range.  Another process
    can therefore own a below-floor id that no row carries yet, so both a known reuse and an
    apparently empty gap fail closed.
    """
    (person,) = _registered(stack, _person())
    established = stack.manager.begin("write")
    established.stage_row_insert(person, (1,), record_id=5)
    _declare(stack, established, person)
    stack.manager.commit(established)
    assert stack.heap.next_record_id(person) == 6

    reusing = stack.manager.begin("write")
    reusing.stage_row_insert(person, (2,), record_id=5)
    _declare(stack, reusing, person)

    def commit_window_was_reached(_manager: TransactionManager) -> object:
        pytest.fail("reuse validation ran after the commit window opened")

    monkeypatch.setattr(TransactionManager, "_hold_lease", commit_window_was_reached)
    with pytest.raises(GrafxTransactionStateError) as raised:
        stack.manager.commit(reusing)
    assert raised.value.details["field"] == "record_id"
    stack.manager.rollback(reusing)
    monkeypatch.undo()

    # Identity 3 has no row, but it is below the durable floor and may be reserved elsewhere.
    gap = stack.manager.begin("write")
    gap.stage_row_insert(person, (3,), record_id=3)
    _declare(stack, gap, person)
    with pytest.raises(GrafxTransactionStateError) as gap_refusal:
        stack.manager.commit(gap)
    assert gap_refusal.value.details["field"] == "record_id"
    assert gap_refusal.value.details["durable_floor"] == 6
    stack.manager.rollback(gap)

    identities = [version.record_id for _ref, version in stack.heap.scan_all(person)]
    assert identities == [5]


def test_the_budget_admits_the_exact_size_and_refuses_one_byte_less() -> None:
    """The edge is charged what it will store, so the limit lands on the row and not near it.

    A budget measured from a placeholder would put the boundary somewhere else, and the two
    directions are asserted together for that reason: an exact limit that admits proves the
    charge is not too high, and one byte less refusing proves it is not too low.
    """
    person, company, works_at = _person(), _person(COMPANY_ID, "Company"), _works_at()
    measuring = _context(max_transaction_bytes=1 << 20)
    source = measuring.stage_row_insert(person, (1,))
    target = measuring.stage_row_insert(company, (1,))
    measuring.stage_row_insert(works_at, (source, target, 2020))
    exact = measuring._staged_payload_bytes
    assert exact > 0

    admitted = _context(max_transaction_bytes=exact)
    first = admitted.stage_row_insert(person, (1,))
    second = admitted.stage_row_insert(company, (1,))
    admitted.stage_row_insert(works_at, (first, second, 2020))
    assert admitted._staged_payload_bytes == exact
    assert len(admitted.row_intents) == 3

    refusing = _context(max_transaction_bytes=exact - 1)
    third = refusing.stage_row_insert(person, (1,))
    fourth = refusing.stage_row_insert(company, (1,))
    held = len(refusing.row_intents)
    with pytest.raises(GrafxTransactionBudgetExceeded):
        refusing.stage_row_insert(works_at, (third, fourth, 2020))
    # The refused edge is not half-staged: the list is exactly what it was before the attempt.
    assert len(refusing.row_intents) == held


def test_a_committed_edge_leaves_no_private_token_anywhere(stack: Stack) -> None:
    """Nothing durable, and nothing the caller can reach afterwards, holds a pending identity."""
    person, company, works_at = _registered(
        stack, _person(), _person(COMPANY_ID, "Company"), _works_at()
    )
    transaction = stack.manager.begin("write")
    source = transaction.stage_row_insert(person, (10,))
    target = transaction.stage_row_insert(company, (20,))
    transaction.stage_row_insert(works_at, (source, target, 2020))
    _declare(stack, transaction, person, company, works_at)

    stack.manager.commit(transaction)

    for table in (person, company, works_at):
        for values in _stored(stack, table):
            assert not any(isinstance(value, PendingRowRef) for value in values)
    assert transaction.row_refs
    assert not any(
        isinstance(reference, PendingRowRef) for reference in transaction.row_refs
    )
    for intent in transaction.row_intents:
        assert intent.operation in RowOperation
