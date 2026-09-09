"""FTS-v1 analyzer identity and multi-entry derivation over STRING node fields.

Unicode normalization/category rules are frozen at 3.2 and full case folding at
15.1; Python's evolving Unicode database or host locale cannot reinterpret an index.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from collections.abc import Sequence
from unicodedata import ucd_3_2_0
from okto_grafx.domain.index.text_casefold import CASEFOLD_15_1

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxIndexError,
    GrafxQueryBudgetExceeded,
)

__all__ = ["TextIndexOptions", "TextSearchLimits", "TextHit", "TextSearchResult"]

FULLTEXT_CAPABILITY = "fulltext_indexes_v1"
FULLTEXT_STATISTICS_CAPABILITY = "fulltext_statistics_v1"
PREFIX = "fulltext_v1_"
STATISTICS_PREFIX = "fulltext_v2_"
ANALYZERS = ("standard", "keyword", "code_identifier", "whitespace")
_HEADER = struct.Struct("<BBBBHII")
_NORMALIZATIONS = ("none", "NFC", "NFKC")
_FOLDS = ("none", "ascii", "unicode")
_SPACES = frozenset(
    " \t\n\r\v\f\u0085\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
)
_ASCII_PUNCTUATION = frozenset("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")


def _bad(field: str) -> GrafxConfigurationError:
    """Return a configuration error before any index publication."""
    return GrafxConfigurationError("Invalid full-text option.", field=field)


@dataclass(frozen=True, slots=True)
class TextIndexOptions:
    """Persisted analyzer v1, pinned Unicode rules, locale und and no stop/stem profiles."""

    analyzer: str = "standard"
    analyzer_version: int = 1
    max_token_bytes: int = 64
    max_document_characters: int = 65_536
    max_document_tokens: int = 4096
    field_weights: tuple[float, ...] = (1.0,)
    normalization: str = "NFC"
    case_folding: str = "unicode"
    locale: str = "und"
    stopwords: str = "none"
    stemming: str = "none"
    statistics_mode: str = "wal"

    def __post_init__(self) -> None:
        """Capture bounded immutable field weights and exact supported analyzer semantics."""
        if type(self.statistics_mode) is not str or self.statistics_mode not in ("wal", "durable"):
            raise _bad("statistics_mode")
        if type(self.analyzer) is not str or self.analyzer not in ANALYZERS:
            raise _bad("analyzer")
        if type(self.analyzer_version) is not int or self.analyzer_version != 1:
            raise _bad("analyzer_version")
        if (
            type(self.normalization) is not str
            or type(self.case_folding) is not str
            or self.normalization not in _NORMALIZATIONS
            or self.case_folding not in _FOLDS
        ):
            raise _bad("normalization_or_case_folding")
        if (
            any(
                type(v) is not str for v in (self.locale, self.stopwords, self.stemming)
            )
            or self.locale != "und"
            or self.stopwords != "none"
            or self.stemming != "none"
        ):
            raise _bad("locale_or_profile")
        for field, maximum in (
            ("max_token_bytes", 128),
            ("max_document_characters", 16_777_216),
            ("max_document_tokens", 65_536),
        ):
            if (
                type(getattr(self, field)) is not int
                or not 1 <= getattr(self, field) <= maximum
            ):
                raise _bad(field)
        if (
            type(self.field_weights) is not tuple
            or not 1 <= len(self.field_weights) <= 4
        ):
            raise _bad("field_weights")
        weights = []
        for weight in self.field_weights:
            if type(weight) not in (int, float) or not 0 < weight <= 1000:
                raise _bad("field_weights")
            weights.append(float(weight))
        object.__setattr__(self, "field_weights", tuple(weights))

    def derivation(self) -> str:
        """Encode the complete durable identity as canonical ASCII hex in catalog v2."""
        raw = _HEADER.pack(
            ANALYZERS.index(self.analyzer),
            len(self.field_weights),
            _NORMALIZATIONS.index(self.normalization),
            _FOLDS.index(self.case_folding),
            self.max_token_bytes,
            self.max_document_characters,
            self.max_document_tokens,
        )
        return (
            (STATISTICS_PREFIX if self.statistics_mode == "durable" else PREFIX)
            + (
                raw
                + struct.pack("<" + "d" * len(self.field_weights), *self.field_weights)
            ).hex()
        )


def decode_options(derivation: str) -> TextIndexOptions:
    """Decode/validate all analyzer identity bytes; reserved/unknown forms refuse."""
    try:
        raw = bytes.fromhex(derivation[len(PREFIX) :])
        analyzer, fields, normalization, folding, length, characters, tokens = (
            _HEADER.unpack_from(raw)
        )
        weights = struct.unpack("<" + "d" * fields, raw[_HEADER.size :])
        options = TextIndexOptions(
            ANALYZERS[analyzer],
            1,
            length,
            characters,
            tokens,
            weights,
            _NORMALIZATIONS[normalization],
            _FOLDS[folding],
            statistics_mode="durable" if derivation.startswith(STATISTICS_PREFIX) else "wal",
        )
        if derivation != options.derivation():
            raise ValueError("noncanonical")
        return options
    except (ValueError, IndexError, struct.error, GrafxConfigurationError) as failure:
        raise GrafxIndexError(
            "Unsupported/corrupt full-text analyzer identity.", field="key_derivation"
        ) from failure


def is_fulltext(derivation: str) -> bool:
    """Recognize the reserved family; decoding still validates the complete identity."""
    return derivation.startswith((PREFIX, STATISTICS_PREFIX))


def has_durable_statistics(derivation: str) -> bool:
    """Recognize the opt-in derivation requiring native persisted corpus totals."""
    return derivation.startswith(STATISTICS_PREFIX)


def _fold(text: str, policy: str) -> str:
    """Apply a selected fixed folding policy, never the runtime str.casefold table."""
    if policy == "none":
        return text
    if policy == "unicode":
        return "".join(CASEFOLD_15_1.get(ord(c), c) for c in text)
    return "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in text)


def analyze(text: str, options: TextIndexOptions) -> tuple[str, ...]:
    """Tokenize deterministically; overlong documents/tokens refuse, never truncate."""
    if type(text) is not str:
        raise _bad("text")
    if len(text) > options.max_document_characters:
        raise GrafxQueryBudgetExceeded(
            "Full-text document character budget exceeded.", resource="text_characters"
        )
    if options.normalization != "none":
        text = ucd_3_2_0.normalize(options.normalization, text)
    if len(text) > options.max_document_characters:
        raise GrafxQueryBudgetExceeded(
            "Normalized text exceeds character budget.", resource="text_characters"
        )
    tokens: list[str] = []

    def add(token: str) -> None:
        """Admit one complete term before retaining it."""
        if not token:
            return
        normalized = _fold(token, options.case_folding)
        if options.normalization != "none":
            normalized = ucd_3_2_0.normalize(options.normalization, normalized)
        if len(normalized.encode("utf-8")) > options.max_token_bytes:
            raise GrafxQueryBudgetExceeded(
                "Full-text token byte budget exceeded.", resource="text_token_bytes"
            )
        if len(tokens) >= options.max_document_tokens:
            raise GrafxQueryBudgetExceeded(
                "Full-text document token budget exceeded.", resource="text_tokens"
            )
        tokens.append(normalized)

    if options.analyzer == "keyword":
        add(text.strip("".join(_SPACES)))
        return tuple(tokens)
    delimiters = _SPACES | (
        _ASCII_PUNCTUATION if options.analyzer == "standard" else frozenset()
    )
    start = 0
    words = []
    for i, character in enumerate(text):
        if character in delimiters or (
            options.analyzer == "standard" and ucd_3_2_0.category(character)[0] in "PZC"
        ):
            if i > start:
                words.append(text[start:i])
            start = i + 1
    if start < len(text):
        words.append(text[start:])
    for word in words:
        add(word)
        if options.analyzer != "code_identifier":
            continue
        # Whole spelling + snake/camel/acronym/path/scope/digit components.
        parts = []
        start = 0
        for i, char in enumerate(word):
            separator = char in _ASCII_PUNCTUATION
            previous = word[i - 1] if i else ""
            boundary = i > start and (
                ("A" <= char <= "Z" and "a" <= previous <= "z")
                or (("0" <= char <= "9") != ("0" <= previous <= "9"))
                or (
                    "A" <= char <= "Z"
                    and "A" <= previous <= "Z"
                    and i + 1 < len(word)
                    and "a" <= word[i + 1] <= "z"
                )
            )
            if separator or boundary:
                if i > start:
                    parts.append(word[start:i])
                start = i + 1 if separator else i
        if start < len(word):
            parts.append(word[start:])
        for part in parts:
            if _fold(part, options.case_folding) != _fold(word, options.case_folding):
                add(part)
    return tuple(tokens)


def field_tokens(
    values: Sequence[object], positions: tuple[int, ...], options: TextIndexOptions
) -> tuple[tuple[str, ...], ...]:
    """Apply all field/document bounds consistently in writes, reads and rebuilds."""
    if len(positions) != len(options.field_weights):
        raise _bad("field_weights")
    result = []
    total = characters = 0
    for position in positions:
        text = values[position]
        if text is not None and type(text) is not str:
            raise GrafxIndexError(
                "A full-text index requires STRING fields.", field="columns"
            )
        characters += len(text) if text is not None else 0
        if characters > options.max_document_characters:
            raise GrafxQueryBudgetExceeded(
                "Full-text document budget exceeded.", resource="text_characters"
            )
        tokens = () if text is None else analyze(text, options)
        total += len(tokens)
        if total > options.max_document_tokens:
            raise GrafxQueryBudgetExceeded(
                "Full-text document budget exceeded.", resource="text_tokens"
            )
        result.append(tokens)
    return tuple(result)


def entry_keys(
    values: Sequence[object], positions: tuple[int, ...], derivation: str
) -> tuple[bytes, ...]:
    """One length-statistics entry plus one posting per distinct analyzed term."""
    options = decode_options(derivation)
    fields = field_tokens(values, positions, options)
    return keys_from_fields(fields)


def keys_from_fields(fields: tuple[tuple[str, ...], ...]) -> tuple[bytes, ...]:
    """Encode already-analyzed fields without invoking the analyzer again."""
    stats = b"\x00" + struct.pack("<" + "I" * len(fields), *(len(f) for f in fields))
    return (
        stats,
        *(
            b"\x01" + token.encode("utf-8")
            for token in sorted({t for f in fields for t in f})
        ),
    )


def query_term_frequencies(
    fields: tuple[tuple[str, ...], ...], terms: tuple[str, ...]
) -> tuple[dict[str, int], ...]:
    """Count only requested terms in one pass per field, without retaining others.

    Callers reserve O(fields * query terms) temporary space before invoking this
    pure derivation. Document frequency and snapshot visibility are not cached here.
    """
    result = []
    for tokens in fields:
        counts = dict.fromkeys(terms, 0)
        for token in tokens:
            if token in counts:
                counts[token] += 1
        result.append(counts)
    return tuple(result)


class TextAnalysisMemo:
    """Bounded operation-owned pure derivations; never heap or visibility authority."""

    __slots__ = ("_items", "max_bytes", "retained_bytes")

    def __init__(self, max_bytes: int = 1_048_576) -> None:
        self._items: dict[tuple, tuple[tuple[str, ...], ...]] = {}
        self.max_bytes = max_bytes
        self.retained_bytes = 0

    def fields(
        self,
        values: Sequence[object],
        positions: tuple[int, ...],
        options: TextIndexOptions,
    ) -> tuple[tuple[str, ...], ...]:
        """Capture STRING leaves and complete persisted analyzer identity before reuse."""
        texts = tuple(values[p] for p in positions)
        if any(v is not None and type(v) is not str for v in texts):
            return field_tokens(values, positions, options)
        key = (options.derivation(), texts)
        found = self._items.get(key)
        if found is not None:
            return found
        found = field_tokens(values, positions, options)
        charge = 256 + sum(64 + 4 * len(t) for t in texts if t is not None)
        charge += sum(64 + 4 * len(t) for field in found for t in field)
        if self.retained_bytes + charge <= self.max_bytes:
            self._items[key] = found
            self.retained_bytes += charge
        return found

    def keys(
        self, values: Sequence[object], positions: tuple[int, ...], derivation: str
    ) -> tuple[bytes, ...]:
        """Reuse analyzed fields for counting, staging or verification."""
        return keys_from_fields(
            self.fields(values, positions, decode_options(derivation))
        )


@dataclass(frozen=True, slots=True)
class TextSearchLimits:
    """Per-search work/memory bounds; page IO remains cooperatively interruptible."""

    max_query_tokens: int = 64
    max_postings: int = 100_000
    max_candidates: int = 10_000
    max_explanation_bytes: int = 65_536
    max_memory_bytes: int = 32 * 1024 * 1024
    max_statistics_wal_records: int = 4096
    max_statistics_wal_bytes: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:
        """Reject disabled, forged or unbounded counters."""
        for name in (
            "max_query_tokens",
            "max_postings",
            "max_candidates",
            "max_explanation_bytes",
            "max_memory_bytes",
            "max_statistics_wal_records",
            "max_statistics_wal_bytes",
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= 2**31:
                raise _bad(name)


@dataclass(frozen=True, slots=True)
class TextHit:
    """One snapshot-visible lexical match; no mutable row payload leaks out."""

    record_id: int
    score: float
    matched_fields: tuple[str, ...]
    matched_terms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TextSearchResult:
    """Complete bounded BM25 result or a typed refusal, never a partial success."""

    hits: tuple[TextHit, ...]
    regime: str
    index_built_through_commit: int
    snapshot_commit: int
    postings_visited: int
    candidates: int
    corpus_documents: int
    statistics_regime: str = "full_census"
    statistics_wal_records: int = 0
