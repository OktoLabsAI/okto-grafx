"""Pure reduction of the logical row intents one transaction has accumulated.

The query owner and the commit path must eventually consume this same function. If each side
reduces staged work independently, read-your-own-writes can show a state different from the one
that becomes durable. Keeping the reducer in the domain gives both paths one outcome vocabulary
without allocating a physical row identity early.

The same file answers the second question a pending identity raises: a relationship staged
against a node this transaction is still creating carries that node's PRIVATE identity in an
endpoint slot, and something has to prove -- before anything is written -- that the promise it
makes will be kept. :func:`plan_relationship_endpoints` is that proof, and it is pure for the
same reason the reducer is: both the caller that validates and the caller that writes must reach
the same verdict, and two implementations of one rule eventually disagree.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from okto_grafx.domain.errors import GrafxTransactionStateError
from okto_grafx.domain.model.schema import ENDPOINT_COLUMN_COUNT
from okto_grafx.domain.txn.context import PendingRowRef, RowIntent, RowOperation

__all__ = ["PendingEndpoint", "plan_relationship_endpoints", "reduce_row_intents"]


def reduce_row_intents(intents: Sequence[RowIntent]) -> tuple[RowIntent, ...]:
    """Return the final logical outcome per referenced row, preserving first-seen order.

    A pending insert carries a transaction-local reference. Updates fold into that insert and a
    delete cancels it entirely. A committed row keeps the established behaviour: updates collapse
    to the last values and delete wins permanently. Legacy inserts without a reference remain
    independent passthrough rows.
    """
    outcomes: dict[object, RowIntent] = {}
    first_seen: dict[object, int] = {}
    cancelled: set[object] = set()
    passthrough: list[tuple[int, RowIntent]] = []

    for position, intent in enumerate(intents):
        reference = intent.reference
        if reference is None:
            passthrough.append((position, intent))
            continue
        if reference not in first_seen:
            first_seen[reference] = position
        if reference in cancelled:
            continue

        previous = outcomes.get(reference)
        if previous is None:
            outcomes[reference] = intent
            continue
        if previous.operation is RowOperation.DELETE:
            continue
        if previous.operation is RowOperation.INSERT:
            if intent.operation is RowOperation.UPDATE:
                outcomes[reference] = RowIntent(
                    table=previous.table,
                    values=intent.values,
                    record_id=previous.record_id,
                    operation=RowOperation.INSERT,
                    reference=reference,
                )
                continue
            if intent.operation is RowOperation.DELETE:
                del outcomes[reference]
                cancelled.add(reference)
                continue
            raise GrafxTransactionStateError(
                "One pending row reference cannot name two inserts in the same transaction.",
                field="reference",
                value=repr(reference),
            )
        if intent.operation is RowOperation.INSERT:
            raise GrafxTransactionStateError(
                "A stored row reference cannot become a new insert inside the same transaction.",
                field="reference",
                value=repr(reference),
            )
        outcomes[reference] = intent

    placed = list(passthrough)
    placed.extend((first_seen[reference], intent) for reference, intent in outcomes.items())
    placed.sort(key=lambda item: item[0])
    return tuple(intent for _position, intent in placed)


@dataclass(frozen=True, slots=True)
class PendingEndpoint:
    """One relationship endpoint slot that names a node this transaction is still creating.

    Positions index the REDUCED sequence, not the raw staging list. An insert the transaction
    cancelled is not in the reduced sequence at all, which is what makes "the node it names did
    not survive" the same question as "there is no position for it".
    """

    relationship_position: int
    slot: int
    endpoint_position: int


def plan_relationship_endpoints(
    intents: Sequence[RowIntent],
    *,
    txn_id: int,
    owns: Callable[[PendingRowRef], bool],
) -> tuple[PendingEndpoint, ...]:
    """Prove every pending endpoint of every relationship insert, and say what it names.

    Takes the REDUCED sequence, so cancellation has already happened: an endpoint whose node
    insert was deleted in the same transaction finds nothing here and is refused as an orphan,
    rather than resolved against a row that will never exist.

    Everything is proven from the intents alone -- no identity is allocated and nothing is
    written -- so a caller can run this before the commit section and again before it writes and
    get the same answer both times. For each endpoint carrying a pending reference it proves:

    * AUTHENTICITY -- the reference is the exact live object this transaction emitted, checked
      through ``owns`` by identity. Value equality is not enough: another handle's first insert
      has the same fields, and a copy of this handle's own reference is a different promise about
      a row than the one the insert made.
    * ORDER -- the insert it names comes first. This is not a separate rule but a property of
      the walk: only inserts ALREADY passed are in the map, so an endpoint naming a later one is
      indistinguishable from one naming nothing, and both are refused as unstaged.
    * KIND -- the insert it names is a node insert. An edge whose endpoint is another edge is
      not a graph, and the relationship table's own declaration is not enough to rule that out:
      nothing stops a catalog from declaring a relationship as one of its sides.
    * ROLE -- slot 0 is the source and slot 1 the target (W5c), so the table it names must be the
      one the relationship declares for THAT side. A reference correct for the other end is not
      correct here.

    Everywhere else a pending identity is refused outright: only the two endpoint columns of a
    relationship INSERT may name a row this transaction has not written yet.
    """
    positions: dict[int, int] = {}
    plans: list[PendingEndpoint] = []
    for position, intent in enumerate(intents):
        reference = intent.reference
        if intent.operation is RowOperation.INSERT and isinstance(reference, PendingRowRef):
            positions[id(reference)] = position
        if intent.operation is RowOperation.DELETE:
            continue
        if (
            getattr(intent.table, "kind", None) != "rel"
            or intent.operation is not RowOperation.INSERT
        ):
            # An update rewrites a row that already exists, so both of its endpoints already have
            # durable identities, and a node row has no endpoint column at all.
            _refuse_pending_values(intent, position, txn_id)
            continue
        for slot in range(min(ENDPOINT_COLUMN_COUNT, len(intent.values))):
            endpoint = intent.values[slot]
            if not isinstance(endpoint, PendingRowRef):
                continue
            plans.append(
                _plan_one_endpoint(
                    intents=intents,
                    positions=positions,
                    intent=intent,
                    position=position,
                    slot=slot,
                    endpoint=endpoint,
                    txn_id=txn_id,
                    owns=owns,
                )
            )
        _refuse_pending_values(intent, position, txn_id, after=ENDPOINT_COLUMN_COUNT)
    return tuple(plans)


def _plan_one_endpoint(
    *,
    intents: Sequence[RowIntent],
    positions: dict[int, int],
    intent: RowIntent,
    position: int,
    slot: int,
    endpoint: PendingRowRef,
    txn_id: int,
    owns: Callable[[PendingRowRef], bool],
) -> PendingEndpoint:
    """Return where one pending endpoint points, refusing every way it could point nowhere."""
    if endpoint.txn_id != txn_id or not owns(endpoint):
        raise GrafxTransactionStateError(
            "A relationship endpoint may name only a pending row identity this transaction "
            "emitted and still holds.",
            field="pending_row_reference",
            position=position,
            slot=slot,
            value=repr(endpoint),
            txn_id=txn_id,
        )
    endpoint_position = positions.get(id(endpoint))
    if endpoint_position is None:
        raise GrafxTransactionStateError(
            "A relationship endpoint must name an insert this transaction has already staged "
            "and has not cancelled.",
            field="pending_row_reference",
            position=position,
            slot=slot,
            value=repr(endpoint),
            txn_id=txn_id,
        )
    endpoint_table = intents[endpoint_position].table
    if getattr(endpoint_table, "kind", None) != "node":
        raise GrafxTransactionStateError(
            "A relationship endpoint must name a node insert, not another relationship.",
            field="pending_row_reference",
            position=position,
            slot=slot,
            value=repr(endpoint),
            table=getattr(endpoint_table, "name", None),
            txn_id=txn_id,
        )
    declared = intent.table.from_table if slot == 0 else intent.table.to_table
    if (
        endpoint.table_id != getattr(endpoint_table, "table_id", None)
        or getattr(endpoint_table, "name", None) != declared
    ):
        raise GrafxTransactionStateError(
            "A relationship endpoint must name a row of the table its own side declares.",
            field="pending_row_reference",
            position=position,
            slot=slot,
            value=repr(endpoint),
            declared=declared,
            observed=getattr(endpoint_table, "name", None),
            txn_id=txn_id,
        )
    return PendingEndpoint(
        relationship_position=position,
        slot=slot,
        endpoint_position=endpoint_position,
    )


def _refuse_pending_values(
    intent: RowIntent, position: int, txn_id: int, *, after: int = 0
) -> None:
    """Refuse a pending identity anywhere a stored value is expected.

    Only the two endpoint columns of a relationship insert may name a row this transaction has
    not written yet. A pending token in a property column, or anywhere in a node row, would reach
    the encoder as a value -- and the encoder's job is to turn values into bytes, not to notice
    that one of them was a promise.
    """
    for slot in range(after, len(intent.values)):
        if isinstance(intent.values[slot], PendingRowRef):
            raise GrafxTransactionStateError(
                "Only the two endpoint columns of a relationship insert may carry a pending row "
                "identity; every other column must carry a stored value.",
                field="values",
                position=position,
                slot=slot,
                value=repr(intent.values[slot]),
                table=getattr(intent.table, "name", None),
                txn_id=txn_id,
            )
