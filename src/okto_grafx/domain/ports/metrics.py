"""The metrics port and the metric naming contract (CONTRACT.md sections 4.4 and G7).

A metric here is a contract, not a log line. The descriptor validates itself the moment it is
built, so a badly named metric or an unbounded label is rejected at registration time, in the
component that declares it, and never in production (SPEC-M1 TR-7, FR-14).
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import Enum
from math import isfinite
from typing import Protocol, runtime_checkable

from okto_grafx.domain.errors import GrafxConfigurationError

__all__ = [
    "METRIC_NAME_PREFIX",
    "METRIC_UNIT_SUFFIXES",
    "FORBIDDEN_LABEL_NAMES",
    "UNBOUNDED_LABEL_CARDINALITY_LIMIT",
    "NON_EN_US_MARKERS",
    "NON_EN_US_WORDS",
    "MetricKind",
    "LabelSpec",
    "MetricDescriptor",
    "MetricsSink",
]

METRIC_NAME_PREFIX: str = "oktografx_"
"""Every metric name starts with this prefix so a dashboard can select the whole engine."""

METRIC_UNIT_SUFFIXES: frozenset[str] = frozenset(
    {
        "_seconds",
        "_bytes",
        "_total",
        "_ratio",
        "_count",
        "_multiple",
        "_k",
        "_entries",
        "_segments",
        "_transactions",
        "_backlog",
        "_depth",
    }
)
"""A metric name ends with one of these, so the unit is readable without opening the code."""

FORBIDDEN_LABEL_NAMES: frozenset[str] = frozenset(
    {
        "node_id",
        "record_id",
        "id",
        "key",
        "path",
        "file",
        "query",
        "text",
        "message",
        "vector",
        "embedding",
        "uuid",
        "lsn",
        "offset",
    }
)
"""Label names whose domain is unbounded by nature. Using one of them explodes the series count."""

UNBOUNDED_LABEL_CARDINALITY_LIMIT: int = 64
"""A label that does not enumerate its values may not declare a bound larger than this."""

NON_EN_US_MARKERS: tuple[str, ...] = (
    " nao ",
    " sao ",
    " esta ",
    " estao ",
    " uma ",
    " para o ",
    " para a ",
    " que o ",
    " que a ",
    " porque ",
    " quando ",
    " entao ",
    " deve ",
    " pelo ",
    " pela ",
    " nunca e ",
    "cao ",
    "coes ",
)
"""Accent-free pt-BR fragments used to keep the product surface en-US (guideline G1).

Correctly written Portuguese leaves ASCII, which is the first and strongest rule; these
fragments catch the accent-stripped form that survives it. Each one was chosen because no
English word contains it as a whole token, so the heuristic does not fire on en-US prose. The
same tuple is the single source of truth for ``tests/test_language_surface.py``.
"""



NON_EN_US_WORDS: frozenset[str] = frozenset(
    {
    "aberto", "ainda", "antes", "apenas", "aqui", "armazenamento", "arquivo", "atual", 
    "atualizar", "aviso", "banco", "buscar", "cabecalho", "cada", "campo", "chamada", "chave", 
    "cheio", "coluna", "concluido", "consulta", "contagem", "criar", "dados", "depois", "deve", 
    "devem", "encontrado", "entao", "entrada", "entre", "erro", "escrita", "escrito", "espaco", 
    "esperado", "esta", "estao", "excluir", "executado", "falha", "falso", "fechado", "gerado", 
    "gravacao", "gravado", "gravar", "indice", "iniciado", "inicio", "leitura", "linha", 
    "lista", "memoria", "mesmo", "momento", "motivo", "muito", "nao", "nome", "novo", "numero", 
    "nunca", "obtido", "onde", "pagina", "pela", "pelo", "porque", "pouco", "primeiro", 
    "processo", "proximo", "quando", "quantidade", "recuperacao", "registro", "resultado", 
    "retorno", "saida", "salvar", "sao", "sempre", "sobre", "sucesso", "tabela", "tamanho", 
    "tentativa", "tipo", "todas", "todos", "transacao", "ultimo", "uma", "umas", "usuario", 
    "validacao", "valor", "vazio", "verdadeiro"
    }
)
"""Curated pt-BR words, the companion of :data:`NON_EN_US_MARKERS` (amendment A57).

The markers catch function words in running prose; these catch the nouns and verbs an error
message or a metric description is actually built from. Using only the markers let an ordinary
three-word pt-BR metric description pass the source gate AND this registration check, which is
the surface guideline G1 names first. Every word was checked against the complete identifier and
literal vocabulary of ``src/``, so a hit means Portuguese rather than a coincidence.
"""


def _words_of(text: str) -> set[str]:
    """Return the lowercase word tokens of a sentence, with a naive plural stripped."""
    tokens: set[str] = set()
    for raw in text.lower().split():
        cleaned = "".join(character for character in raw if character.isalpha())
        if not cleaned:
            continue
        tokens.add(cleaned)
        if cleaned.endswith("s") and len(cleaned) > 3:
            tokens.add(cleaned[:-1])
    return tokens


def _first_non_en_us_marker(text: str) -> str | None:
    """Return the first pt-BR marker found in the text, or None when it reads as en-US."""
    haystack = f" {text.lower()} "
    for marker in NON_EN_US_MARKERS:
        if marker in haystack:
            return marker
    shared = sorted(_words_of(text) & NON_EN_US_WORDS)
    return shared[0] if shared else None


def _is_snake_case(value: str) -> bool:
    """Return True when the value is lowercase ASCII words joined by single underscores."""
    if not value or value.startswith("_") or value.endswith("_"):
        return False
    if not (value[0].isascii() and value[0].islower()):
        return False
    previous_was_underscore = False
    for character in value:
        if character == "_":
            if previous_was_underscore:
                return False
            previous_was_underscore = True
            continue
        if not character.isascii() or not (character.islower() or character.isdigit()):
            return False
        previous_was_underscore = False
    return True


class MetricKind(str, Enum):
    """The three shapes a metric can have."""

    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"


@dataclass(frozen=True, slots=True)
class LabelSpec:
    """One label of a metric together with the bound of its value domain.

    Either the values are enumerated in allowed_values, or max_cardinality states how many
    distinct values the label may ever take. An unenumerated label may not claim a bound larger
    than UNBOUNDED_LABEL_CARDINALITY_LIMIT, and the names in FORBIDDEN_LABEL_NAMES are refused
    outright because their domain is unbounded by nature.
    """

    name: str
    allowed_values: frozenset[str] | None = None
    max_cardinality: int = 8

    def __post_init__(self) -> None:
        """Reject an unusable label specification with GrafxConfigurationError."""
        name = self.name
        if not isinstance(name, str) or not _is_snake_case(name):
            raise GrafxConfigurationError(
                f"Metric label name must be snake_case; got {name!r}.",
                field="name",
                value=name,
            )
        if name in FORBIDDEN_LABEL_NAMES:
            raise GrafxConfigurationError(
                f"Metric label name {name!r} is forbidden because its domain is unbounded.",
                field="name",
                value=name,
            )
        if not isinstance(self.max_cardinality, int) or isinstance(self.max_cardinality, bool):
            raise GrafxConfigurationError(
                f"Metric label {name!r} needs an integer max_cardinality; got {self.max_cardinality!r}.",
                field="max_cardinality",
                value=self.max_cardinality,
            )
        if self.max_cardinality < 1:
            raise GrafxConfigurationError(
                f"Metric label {name!r} needs max_cardinality of at least 1; got {self.max_cardinality}.",
                field="max_cardinality",
                value=self.max_cardinality,
            )
        allowed = self.allowed_values
        if allowed is None:
            if self.max_cardinality > UNBOUNDED_LABEL_CARDINALITY_LIMIT:
                raise GrafxConfigurationError(
                    f"Metric label {name!r} does not enumerate its values, so max_cardinality may "
                    f"not exceed {UNBOUNDED_LABEL_CARDINALITY_LIMIT}; got {self.max_cardinality}.",
                    field="max_cardinality",
                    value=self.max_cardinality,
                )
            return
        if not isinstance(allowed, frozenset):
            raise GrafxConfigurationError(
                f"Metric label {name!r} needs allowed_values as a frozenset; got {type(allowed).__name__}.",
                field="allowed_values",
                value=allowed,
            )
        if not allowed:
            raise GrafxConfigurationError(
                f"Metric label {name!r} enumerates an empty value domain.",
                field="allowed_values",
                value=allowed,
            )
        if any(not isinstance(value, str) or not value for value in allowed):
            raise GrafxConfigurationError(
                f"Metric label {name!r} must enumerate non-empty strings.",
                field="allowed_values",
                value=sorted(str(value) for value in allowed),
            )
        if len(allowed) > self.max_cardinality:
            raise GrafxConfigurationError(
                f"Metric label {name!r} enumerates {len(allowed)} values but declares a bound of "
                f"{self.max_cardinality}; raise max_cardinality to match.",
                field="max_cardinality",
                value=self.max_cardinality,
            )


@dataclass(frozen=True, slots=True)
class MetricDescriptor:
    """The registration contract of one metric: name, kind, en-US description, unit and labels.

    Validation happens on construction, so a component cannot ship a metric that a dashboard or
    a continuous integration gate would be unable to consume.
    """

    name: str
    kind: MetricKind
    description: str
    unit: str = ""
    labels: tuple[LabelSpec, ...] = ()
    buckets: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        """Reject an unusable metric descriptor with GrafxConfigurationError."""
        self._validate_name()
        if not isinstance(self.kind, MetricKind):
            raise GrafxConfigurationError(
                f"Metric {self.name!r} needs a MetricKind; got {self.kind!r}.",
                field="kind",
                value=self.kind,
            )
        self._validate_description()
        self._validate_unit()
        self._validate_labels()
        self._validate_buckets()

    def _validate_name(self) -> None:
        """Enforce prefix, snake_case shape and unit suffix on the metric name."""
        name = self.name
        if not isinstance(name, str) or not _is_snake_case(name):
            raise GrafxConfigurationError(
                f"Metric name must be snake_case; got {name!r}.",
                field="name",
                value=name,
            )
        if not name.startswith(METRIC_NAME_PREFIX):
            raise GrafxConfigurationError(
                f"Metric name must start with {METRIC_NAME_PREFIX!r}; got {name!r}.",
                field="name",
                value=name,
            )
        if not any(name.endswith(suffix) for suffix in METRIC_UNIT_SUFFIXES):
            raise GrafxConfigurationError(
                f"Metric name {name!r} must end with one of the declared unit suffixes: "
                f"{', '.join(sorted(METRIC_UNIT_SUFFIXES))}.",
                field="name",
                value=name,
            )

    def _validate_description(self) -> None:
        """Require a non-empty en-US description that reads as a sentence.

        The language is checked at registration because a dashboard, an alert and an operator
        all read this string, and by then the component that wrote it is long gone (G1, G7).
        """
        description = self.description
        if not isinstance(description, str) or not description.strip():
            raise GrafxConfigurationError(
                f"Metric {self.name!r} needs a non-empty en-US description.",
                field="description",
                value=description,
            )
        if not description.rstrip().endswith("."):
            raise GrafxConfigurationError(
                f"Metric {self.name!r} needs a description ending with a period; got {description!r}.",
                field="description",
                value=description,
            )
        if len(description.strip().strip(".").split()) < 2:
            raise GrafxConfigurationError(
                f"Metric {self.name!r} needs a description that says something, not {description!r}.",
                field="description",
                value=description,
            )
        if not description.isascii():
            raise GrafxConfigurationError(
                f"Metric {self.name!r} needs an en-US description in ASCII; got {description!r}.",
                field="description",
                value=description,
            )
        marker = _first_non_en_us_marker(description)
        if marker is not None:
            raise GrafxConfigurationError(
                f"Metric {self.name!r} needs an en-US description; {description!r} matches the "
                f"non en-US marker {marker!r}.",
                field="description",
                value=description,
            )

    def _validate_unit(self) -> None:
        """Allow an empty unit, and otherwise require a lowercase snake_case token."""
        unit = self.unit
        if not isinstance(unit, str):
            raise GrafxConfigurationError(
                f"Metric {self.name!r} needs a string unit; got {unit!r}.",
                field="unit",
                value=unit,
            )
        if unit and not _is_snake_case(unit):
            raise GrafxConfigurationError(
                f"Metric {self.name!r} needs a snake_case unit; got {unit!r}.",
                field="unit",
                value=unit,
            )

    def _validate_labels(self) -> None:
        """Require a tuple of LabelSpec with distinct names."""
        labels = self.labels
        if not isinstance(labels, tuple):
            raise GrafxConfigurationError(
                f"Metric {self.name!r} needs labels as a tuple; got {type(labels).__name__}.",
                field="labels",
                value=labels,
            )
        seen: set[str] = set()
        for label in labels:
            if not isinstance(label, LabelSpec):
                raise GrafxConfigurationError(
                    f"Metric {self.name!r} needs every label to be a LabelSpec; got {label!r}.",
                    field="labels",
                    value=label,
                )
            if label.name in seen:
                raise GrafxConfigurationError(
                    f"Metric {self.name!r} declares the label {label.name!r} more than once.",
                    field="labels",
                    value=label.name,
                )
            seen.add(label.name)

    def _validate_buckets(self) -> None:
        """Require buckets on a histogram, forbid them anywhere else, and keep them ordered."""
        buckets = self.buckets
        if not isinstance(buckets, tuple):
            raise GrafxConfigurationError(
                f"Metric {self.name!r} needs buckets as a tuple; got {type(buckets).__name__}.",
                field="buckets",
                value=buckets,
            )
        if self.kind is not MetricKind.HISTOGRAM:
            if buckets:
                raise GrafxConfigurationError(
                    f"Metric {self.name!r} is a {self.kind.value} and may not declare buckets.",
                    field="buckets",
                    value=buckets,
                )
            return
        if not buckets:
            raise GrafxConfigurationError(
                f"Histogram {self.name!r} must declare at least one bucket boundary.",
                field="buckets",
                value=buckets,
            )
        previous: float | None = None
        for boundary in buckets:
            if isinstance(boundary, bool) or not isinstance(boundary, (int, float)):
                raise GrafxConfigurationError(
                    f"Histogram {self.name!r} needs numeric bucket boundaries; got {boundary!r}.",
                    field="buckets",
                    value=boundary,
                )
            if not isfinite(boundary):
                raise GrafxConfigurationError(
                    f"Histogram {self.name!r} needs finite bucket boundaries; got {boundary!r}.",
                    field="buckets",
                    value=boundary,
                )
            if previous is not None and boundary <= previous:
                raise GrafxConfigurationError(
                    f"Histogram {self.name!r} needs strictly increasing bucket boundaries; "
                    f"{boundary!r} does not follow {previous!r}.",
                    field="buckets",
                    value=buckets,
                )
            previous = boundary


@runtime_checkable
class MetricsSink(Protocol):
    """Where every operational number of the engine goes, including the no-op destination."""

    @property
    def enabled(self) -> bool:
        """False for the no-op sink; hot paths MUST guard with this to avoid allocation."""
        ...

    def register(self, descriptor: MetricDescriptor) -> None:
        """Declare a metric before it is used. An unknown name at emission time is an error."""
        ...

    def increment(self, name: str, value: float = 1.0, labels: Mapping[str, str] | None = None) -> None:
        """Add to a counter."""
        ...

    def set_gauge(self, name: str, value: float, labels: Mapping[str, str] | None = None) -> None:
        """Set the current value of a gauge."""
        ...

    def observe(self, name: str, value: float, labels: Mapping[str, str] | None = None) -> None:
        """Record one observation of a histogram."""
        ...

    def time(self, name: str, labels: Mapping[str, str] | None = None) -> AbstractContextManager[None]:
        """Return a context manager that observes the duration of the block it wraps."""
        ...

    def snapshot(self) -> Mapping[str, object]:
        """Machine-readable current values; the CI gate reads THIS, never a log."""
        ...
