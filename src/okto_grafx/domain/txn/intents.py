"""Pure reduction of the logical row intents one transaction has accumulated.

The query owner and the commit path must eventually consume this same function. If each side
reduces staged work independently, read-your-own-writes can show a state different from the one
that becomes durable. Keeping the reducer in the domain gives both paths one outcome vocabulary
without allocating a physical row identity early.
"""

from __future__ import annotations

from collections.abc import Sequence

from okto_grafx.domain.errors import GrafxTransactionStateError
from okto_grafx.domain.txn.context import RowIntent, RowOperation

__all__ = ["reduce_row_intents"]


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
