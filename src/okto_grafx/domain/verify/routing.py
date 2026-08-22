"""Routing a page refusal onto what it means (CONTRACT.md section 8.6, SPEC-M1 FR-11).

C1 distinguishes four states behind one exception class, and it does so because this component
asked: a refusal that says only ``corruption_detected`` cannot be acted on, and three genuinely
different states were arriving under it. Each now carries a ``field`` in ``details``, and this
module is where those turn into a verdict.

* ``unwritten_page`` -- the page was ALLOCATED and never written, which is exactly what a crash
  between the ``PAGE_ALLOC`` record and the ``WRITE_PAGE`` record that was to follow it leaves.
  The redo already coming for it is the answer, so this is **not damage** and it must not be
  routed to truncation and quarantine.
* ``page_type`` -- the page WAS written and says the wrong thing. Nothing is coming to fix it,
  so it needs a verifier and, if an operator agrees, quarantine.
* ``missing_page_descriptor`` -- the page was written as a heap page and carries no descriptor
  slot, so it cannot say which table it belongs to. Written, structurally incomplete, and not
  something a redo repairs: damage.
* ``freed_slot`` -- an ordinary state of a live page, and C1 now raises
  ``GrafxUnsupportedOperation`` for it rather than routing it here at all. It is listed so the
  route is a CLOSED set rather than an open one with a permissive default.

The set is closed on purpose (A54.1, A89): a field this module has never heard of is routed to
``unclassified`` and reported as such, never quietly folded into the nearest neighbour. A verifier
that guesses is a verifier that certifies.
"""

from __future__ import annotations

from okto_grafx.domain.errors import GrafxError, GrafxUnsupportedOperation
from okto_grafx.domain.verify.findings import FindingKind

__all__ = [
    "PAGE_REFUSAL_ROUTES",
    "UNCLASSIFIED",
    "REDO_ANSWERS_IT",
    "route_page_refusal",
]

UNCLASSIFIED: str = "unclassified"
"""What a refusal this build has never been taught to route is called."""

REDO_ANSWERS_IT: frozenset[str] = frozenset({"unwritten_page"})
"""The refusals a replay of the log repairs on its own, so recovery must not treat them as loss."""

PAGE_REFUSAL_ROUTES: dict[str, str] = {
    "unwritten_page": FindingKind.PAGE_UNWRITTEN,
    "page_type": FindingKind.PAGE_TYPE,
    "missing_page_descriptor": FindingKind.PAGE_DESCRIPTOR_MISSING,
    "freed_slot": FindingKind.RECORD_HEADER,
}
"""Every routing key C1 publishes, mapped to what this component reports for it.

Pinned by ``test_the_page_refusal_routes_are_the_four_keys_c1_publishes`` as an EXACT set, so
widening it -- the one edit that would let an unknown state be reported as a known one -- fails
the suite rather than passing quietly (A56, A68).
"""


def route_page_refusal(failure: GrafxError) -> tuple[str, bool]:
    """Return what a page refusal should be reported as, and whether it is damage.

    The second half of the answer is the one that matters to recovery: an allocated page nobody
    wrote is repaired by the redo that was already coming for it, so reporting it as damage would
    send an ordinary crash window to truncation and quarantine. Everything else this can meet was
    written and is wrong, which no replay repairs.
    """
    if not isinstance(failure, GrafxError):
        raise GrafxUnsupportedOperation(
            f"A page refusal is routed from a Grafx error; got {type(failure).__name__}.",
            field="failure",
            value=type(failure).__name__,
        )
    field = failure.details.get("field")
    if not isinstance(field, str):
        return UNCLASSIFIED, True
    kind = PAGE_REFUSAL_ROUTES.get(field)
    if kind is None:
        return UNCLASSIFIED, True
    return kind, field not in REDO_ANSWERS_IT
