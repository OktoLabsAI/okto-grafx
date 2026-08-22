"""Reading a retry decision out of an error, the way amendment A47 requires.

A47 exists because A28 folded transient access failures into
``GrafxDurabilityBarrierFailed``: a caller that decides by EXCEPTION CLASS therefore reports an
antivirus touch on a barrier as permanent, while riding out the identical condition on
``append_log``. The classification travels in ``details["retryable"]``, so that is what a caller
reads -- here, in one function, so no site of this component can decide it differently.

The fallback is deliberate and narrow. When ``details`` says nothing, the class attribute is the
declared default for that error type (``GrafxError.retryable``), which is the same value the
constructor would have put in ``details`` had the raiser passed one. Anything that is not a
``GrafxError`` at all is not retryable: this component only ever lets ``Grafx*`` out of a public
door, so a foreign exception reaching a retry decision is a bug being reported, not a condition
to ride out.
"""

from __future__ import annotations

from okto_grafx.domain.errors import GrafxError

__all__ = ["RETRYABLE_KEY", "is_retryable"]

RETRYABLE_KEY: str = "retryable"
"""The ``details`` key A47 makes authoritative for every retry decision."""


def is_retryable(failure: BaseException) -> bool:
    """Return whether this failure is worth attempting again, reading details and never the class.

    Proven by ``test_a_retry_decision_reads_the_details_and_not_the_class``, which passes an error
    whose class default disagrees with what its details declare.
    """
    if not isinstance(failure, GrafxError):
        return False
    declared = failure.details.get(RETRYABLE_KEY)
    if isinstance(declared, bool):
        return declared
    return bool(failure.retryable)
