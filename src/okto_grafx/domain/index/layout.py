"""Physical secondary-index layouts named by durable catalog definitions."""

from __future__ import annotations

from enum import Enum

from okto_grafx.domain.errors import GrafxIndexError

__all__ = ["IndexLayout"]


class IndexLayout(str, Enum):
    """The physical organization used to answer an index definition.

    ``HASH`` is the established equality layout. ``ORDERED`` is the v0.0.4 exact,
    copy-on-write layout; its deliberately narrow key contract is validated by the definition
    that selects it.
    """

    HASH = "hash"
    ORDERED = "ordered"

    @classmethod
    def parse(cls, value: object) -> IndexLayout:
        """Return one exact layout spelling, refusing guesses and case folding."""

        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            for candidate in cls:
                if candidate.value == value:
                    return candidate
        allowed = ", ".join(repr(candidate.value) for candidate in cls)
        raise GrafxIndexError(
            f"An index layout must be one of {allowed}; got {value!r}.",
            field="layout",
            value=repr(value),
        )
