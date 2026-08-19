"""The one typed condition the page layer raises for a caller to handle, not for a user to read.

CONTRACT.md section 2 fixes the taxonomy of errors that leave the engine. The condition declared
here is not one of them: it is the internal answer to "this payload does not fit on this page",
which the heap and the catalog handle by allocating another page. It derives from GrafxError all
the same, so even a caller that forgets to handle it never sees an untyped exception escape
(CONTRACT.md section 11 item 5).
"""

from __future__ import annotations

from okto_grafx.domain.errors import GrafxError

__all__ = ["PageFullError"]


class PageFullError(GrafxError):
    """The page has no room for the payload, so the caller must place it somewhere else.

    This is an expected outcome of filling a page, not a failure of the database. The details
    carry the requested size and the space that was actually available, which is what a caller
    needs in order to decide between compacting, allocating a new page or overflowing.
    """

    code: str = "page_full"
    retryable: bool = False
