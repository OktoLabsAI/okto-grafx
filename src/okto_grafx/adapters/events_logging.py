"""The logging event adapter: structured, en-US, PII-free, and incapable of raising.

``EventSink`` is the narrative channel of Okto Grafx. A metric answers "how much"; an event
answers "what just happened" -- a recovery ran, a segment was quarantined, a lease was taken
over. This adapter puts that narrative into the standard library logger of the host application,
which is the only logging framework an embedded database is allowed to assume.

Three properties make it safe to call from anywhere in the engine.

*Structured.* Every event is logged with the event name and the sanitised payload attached to the
record as ``okto_event`` and ``okto_payload``, so a JSON formatter can emit fields rather than
parse a sentence. The human-readable message carries the same content as ``key=value`` pairs,
sorted by key, so a plain text log is still readable.

*Free of personal data.* A payload value is only ever rendered when it is a number, a boolean or
a short string. Any other object is replaced by its type name, and the keys listed in
``REDACTED_PAYLOAD_KEYS`` -- the ones whose value is user content or an environment identity, such
as a query, a path or a free-text message -- are replaced by a fixed marker. Operational identity
that carries no personal data, such as an LSN, an epoch or a reason code, passes through: the
rule is about content, not about cardinality.

*Incapable of raising.* An event is a side effect of an operation that has already succeeded or
already failed. A broken handler, a payload whose values explode when rendered, or a logger that
throws must never change the outcome of a transaction, so ``emit()`` swallows everything.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

__all__ = [
    "DEFAULT_LOGGER_NAME",
    "MAX_KEY_LENGTH",
    "MAX_VALUE_LENGTH",
    "REDACTED_MARKER",
    "REDACTED_PAYLOAD_KEYS",
    "LoggingEventSink",
]

DEFAULT_LOGGER_NAME: str = "okto_grafx.events"
"""The logger an application configures to see the narrative of the engine."""

REDACTED_MARKER: str = "<redacted>"
"""What replaces the value of a key whose content may not reach a log file."""

REDACTED_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {
        "credential",
        "email",
        "embedding",
        "key",
        "message",
        "password",
        "path",
        "payload",
        "query",
        "secret",
        "text",
        "token",
        "user",
        "username",
        "value",
        "values",
        "vector",
    }
)
"""Payload keys whose value is user content or an environment identity, never engine state."""

MAX_VALUE_LENGTH: int = 256
"""Longest string value written for one payload key; the rest is dropped with an ellipsis."""

MAX_KEY_LENGTH: int = 64
"""Longest payload key written; a longer one is truncated rather than dropped."""

_PLACEHOLDER_LENGTH: int = 3
"""Length of the ellipsis appended to a truncated string."""


def _clean_text(text: str, limit: int) -> str:
    """Return a single-line, bounded rendering of a string, safe to put in a log record."""
    flattened = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    if len(flattened) > limit:
        return flattened[: max(limit - _PLACEHOLDER_LENGTH, 1)] + "..."
    return flattened


def _render_value(value: object) -> object:
    """Return a value that is safe to log: numbers and booleans as they are, strings bounded."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _clean_text(value, MAX_VALUE_LENGTH)
    return f"<{type(value).__name__}>"


class LoggingEventSink:
    """An EventSink that writes structured, sanitised events to a standard library logger."""

    __slots__ = ("_logger", "_level", "_redacted")

    def __init__(
        self,
        logger: logging.Logger | None = None,
        *,
        level: int = logging.INFO,
        redacted_keys: frozenset[str] = REDACTED_PAYLOAD_KEYS,
    ) -> None:
        self._logger = logging.getLogger(DEFAULT_LOGGER_NAME) if logger is None else logger
        self._level = level
        self._redacted = redacted_keys

    @property
    def logger(self) -> logging.Logger:
        """Return the logger this sink writes to."""
        return self._logger

    @property
    def level(self) -> int:
        """Return the level every event is logged at."""
        return self._level

    @property
    def redacted_keys(self) -> frozenset[str]:
        """Return the payload keys whose value is replaced by the redaction marker."""
        return self._redacted

    def emit(self, event: str, payload: Mapping[str, object]) -> None:
        """Log one event. Any failure of the logging stack is absorbed, never propagated."""
        try:
            logger = self._logger
            if not logger.isEnabledFor(self._level):
                return
            name = _clean_text(str(event), MAX_KEY_LENGTH)
            fields = self.sanitize(payload)
            rendered = " ".join(f"{key}={fields[key]!r}" for key in sorted(fields))
            message = f"{name} {rendered}" if rendered else name
            logger.log(
                self._level,
                "%s",
                message,
                extra={"okto_event": name, "okto_payload": fields},
            )
        except Exception:
            return

    def sanitize(self, payload: Mapping[str, object]) -> dict[str, object]:
        """Return a bounded, PII-free copy of a payload, ready to attach to a log record."""
        fields: dict[str, object] = {}
        try:
            items = payload.items()
        except Exception:
            return {"detail": "<unreadable payload>"}
        for raw_key, raw_value in items:
            try:
                key = _clean_text(str(raw_key), MAX_KEY_LENGTH)
                if key in self._redacted:
                    fields[key] = REDACTED_MARKER
                    continue
                fields[key] = _render_value(raw_value)
            except Exception:
                continue
        return fields
