"""Shared fixtures for the Okto Grafx suite.

The fakes below exist to prove that the ports are satisfiable by an ordinary object and to give
the registry something real to bind. They are deliberately minimal: the production adapters
arrive with C1, C2, C3, C8 and C9, and they will be tested against their own behaviour, not
against these stand-ins.
"""

from __future__ import annotations

import ast
import contextlib
import fnmatch
import importlib.util
import os
import pathlib
import platform
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

import pytest

from okto_grafx.adapters.codec_v1 import PageCodecV1
from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxUnsupportedOperation
from okto_grafx.domain.ids import Epoch, Lsn, PageIndex
from okto_grafx.domain.ports import (
    DeadOwnerReport,
    DistanceMetric,
    Lease,
    MetricDescriptor,
    ReaderHandle,
)
from okto_grafx.runtime.registry import PortRegistry

PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
"""Repository root, used by the packaging and boundary gates."""

SOURCE_ROOT: Path = PROJECT_ROOT / "src" / "okto_grafx"
"""Root of the installed package tree."""


class FakeStorageDevice:
    """In-memory stand-in for StorageDevice with the append-only and paged semantics of the port."""

    def __init__(self, page_size: int = 8192) -> None:
        self._page_size = page_size
        self._pages: dict[str, list[bytes]] = {}
        self._logs: dict[str, bytearray] = {}

    @property
    def name(self) -> str:
        return "fake"

    @property
    def page_size(self) -> int:
        return self._page_size

    def exists(self, file: str) -> bool:
        return file in self._pages or file in self._logs

    def create(self, file: str, *, exclusive: bool = True) -> None:
        if exclusive and self.exists(file):
            raise GrafxUnsupportedOperation(f"File {file!r} already exists.", file=file)
        self._pages.setdefault(file, [])
        self._logs.setdefault(file, bytearray())

    def remove(self, file: str) -> None:
        self._pages.pop(file, None)
        self._logs.pop(file, None)

    def list_files(self, prefix: str = "") -> tuple[str, ...]:
        return tuple(sorted(name for name in self._pages if name.startswith(prefix)))

    def file_size(self, file: str) -> int:
        return len(self._logs.get(file, b"")) + len(self._pages.get(file, ())) * self._page_size

    def atomic_replace(self, source: str, target: str) -> None:
        self._pages[target] = self._pages.pop(source, [])
        self._logs[target] = self._logs.pop(source, bytearray())

    def recycle(self, file: str) -> bool:
        self.remove(file)
        return True

    def page_count(self, file: str) -> int:
        return len(self._pages.get(file, ()))

    def allocate(self, file: str, count: int = 1) -> PageIndex:
        pages = self._pages.setdefault(file, [])
        first = len(pages)
        pages.extend(bytes(self._page_size) for _ in range(count))
        return first

    def read_page(self, file: str, page_index: PageIndex) -> bytes:
        pages = self._pages.get(file, [])
        if not 0 <= page_index < len(pages):
            raise GrafxCorruptionDetected(
                f"Page {page_index} of {file!r} is not allocated.", file=file, page=page_index
            )
        return pages[page_index]

    def write_page(self, file: str, page_index: PageIndex, data: bytes) -> None:
        pages = self._pages.get(file, [])
        if not 0 <= page_index < len(pages) or len(data) != self._page_size:
            raise GrafxCorruptionDetected(
                f"Refused a page write to {file!r} at {page_index}.", file=file, page=page_index
            )
        pages[page_index] = bytes(data)

    def append_log(self, file: str, payload: bytes) -> int:
        log = self._logs.setdefault(file, bytearray())
        log.extend(payload)
        return len(log)

    def read_log(self, file: str, offset: int, length: int) -> bytes:
        return bytes(self._logs.get(file, bytearray())[offset : offset + length])

    def log_size(self, file: str) -> int:
        return len(self._logs.get(file, bytearray()))

    def truncate_log(self, file: str, size: int) -> None:
        log = self._logs.setdefault(file, bytearray())
        if size > len(log):
            raise GrafxUnsupportedOperation(
                f"truncate_log only shrinks; {file!r} holds {len(log)} bytes.", file=file
            )
        del log[size:]

    def durable_barrier(self, file: str | None = None) -> None:
        return None


class FakeClock:
    """Deterministic clock: monotonic advances only when the test asks for it."""

    def __init__(self, monotonic: float = 100.0, wall: float = 1_700_000_000.0) -> None:
        self._monotonic = monotonic
        self._wall = wall

    def monotonic(self) -> float:
        return self._monotonic

    def wall(self) -> float:
        return self._wall

    def advance(self, seconds: float) -> None:
        """Move the monotonic and the wall reading forward by the same amount."""
        self._monotonic += seconds
        self._wall += seconds


class FakeCoordinator:
    """Single-participant coordinator: enough shape to satisfy the port, no cross-process claims."""

    def __init__(self) -> None:
        self._epoch: Epoch = 1
        self._readers: dict[str, Lsn] = {}
        self._next_reader = 0

    def owner_id(self) -> str:
        return "fake-owner"

    def current_epoch(self) -> Epoch:
        return self._epoch

    def acquire_writer_lease(self, *, timeout: float) -> Lease:
        return Lease(
            owner_id=self.owner_id(),
            epoch=self._epoch,
            acquired_monotonic=0.0,
            heartbeat_seq=1,
            ttl_seconds=timeout,
        )

    def renew_lease(self, lease: Lease) -> Lease:
        return Lease(
            owner_id=lease.owner_id,
            epoch=lease.epoch,
            acquired_monotonic=lease.acquired_monotonic,
            heartbeat_seq=lease.heartbeat_seq + 1,
            ttl_seconds=lease.ttl_seconds,
        )

    def release_lease(self, lease: Lease) -> None:
        return None

    def validate_epoch(self, epoch: Epoch) -> None:
        return None

    def detect_dead_owner(self, *, stall_threshold: float) -> DeadOwnerReport | None:
        return None

    def takeover(self) -> Lease:
        self._epoch += 1
        return self.acquire_writer_lease(timeout=1.0)

    def register_reader(self, snapshot_lsn: Lsn) -> ReaderHandle:
        self._next_reader += 1
        reader_id = f"reader-{self._next_reader}"
        self._readers[reader_id] = snapshot_lsn
        return ReaderHandle(reader_id=reader_id, snapshot_lsn=snapshot_lsn)

    def refresh_reader(self, handle: ReaderHandle) -> None:
        return None

    def unregister_reader(self, handle: ReaderHandle) -> None:
        self._readers.pop(handle.reader_id, None)

    def reader_horizon(self) -> Lsn | None:
        return min(self._readers.values()) if self._readers else None

    def exclusive(self, name: str, *, timeout: float) -> contextlib.AbstractContextManager[None]:
        return contextlib.nullcontext()


class FakePageCodec:
    """Inspectable codec twin that preserves the real page-format semantics of the port."""

    def __init__(self, page_size: int = 8192) -> None:
        self._page_size = page_size
        self._codec = PageCodecV1(page_size)

    @property
    def format_version(self) -> int:
        return 1

    def checksum(self, payload: bytes) -> int:
        return self._codec.checksum(payload)

    def encode_page(self, page: object) -> bytes:
        return self._codec.encode_page(page)  # type: ignore[arg-type]

    def decode_page(self, raw: bytes, *, verify: bool = True) -> object:
        return self._codec.decode_page(raw, verify=verify)


class RecordingMetricsSink:
    """Metrics sink that keeps every call, so a test can assert on what was emitted."""

    def __init__(self, *, enabled: bool = True) -> None:
        self._enabled = enabled
        self.registered: dict[str, MetricDescriptor] = {}
        self.calls: list[tuple[str, str, float]] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    def register(self, descriptor: MetricDescriptor) -> None:
        self.registered[descriptor.name] = descriptor

    def increment(self, name: str, value: float = 1.0, labels: Mapping[str, str] | None = None) -> None:
        self.calls.append(("increment", name, value))

    def set_gauge(self, name: str, value: float, labels: Mapping[str, str] | None = None) -> None:
        self.calls.append(("set_gauge", name, value))

    def observe(self, name: str, value: float, labels: Mapping[str, str] | None = None) -> None:
        self.calls.append(("observe", name, value))

    def time(
        self, name: str, labels: Mapping[str, str] | None = None
    ) -> contextlib.AbstractContextManager[None]:
        return self._timed(name)

    @contextlib.contextmanager
    def _timed(self, name: str) -> Iterator[None]:
        self.calls.append(("time_enter", name, 0.0))
        yield
        self.calls.append(("time_exit", name, 0.0))

    def snapshot(self) -> Mapping[str, object]:
        return {name: descriptor.kind.value for name, descriptor in self.registered.items()}


class FakeVectorMath:
    """Minimal vector arithmetic in pure Python, sufficient to satisfy the port."""

    @property
    def name(self) -> str:
        return "fake"

    def dot(self, a: Sequence[float], b: Sequence[float]) -> float:
        return float(sum(x * y for x, y in zip(a, b)))

    def cosine(self, a: Sequence[float], b: Sequence[float]) -> float:
        denominator = self.norm(a) * self.norm(b)
        return self.dot(a, b) / denominator if denominator else 0.0

    def euclidean(self, a: Sequence[float], b: Sequence[float]) -> float:
        return float(sum((x - y) ** 2 for x, y in zip(a, b))) ** 0.5

    def norm(self, a: Sequence[float]) -> float:
        return float(sum(x * x for x in a)) ** 0.5

    def normalize(self, a: Sequence[float]) -> tuple[float, ...]:
        length = self.norm(a)
        return tuple(x / length for x in a) if length else tuple(float(x) for x in a)

    def score(self, a: Sequence[float], b: Sequence[float], metric: DistanceMetric) -> float:
        if metric is DistanceMetric.COSINE:
            return self.cosine(a, b)
        if metric is DistanceMetric.DOT:
            return self.dot(a, b)
        return -self.euclidean(a, b)

    def top_k(
        self,
        query: Sequence[float],
        candidates: Sequence[tuple[int, Sequence[float]]],
        k: int,
        metric: DistanceMetric,
    ) -> list[tuple[int, float]]:
        scored = [(identifier, self.score(query, vector, metric)) for identifier, vector in candidates]
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[:k]


class RecordingEventSink:
    """Event sink that keeps every event, so a test can assert on what was published."""

    def __init__(self) -> None:
        self.events: list[tuple[str, Mapping[str, object]]] = []

    def emit(self, event: str, payload: Mapping[str, object]) -> None:
        self.events.append((event, dict(payload)))


@pytest.fixture
def fake_ports() -> dict[str, object]:
    """Return one fresh adapter per required port slot."""
    return {
        "storage": FakeStorageDevice(),
        "clock": FakeClock(),
        "coordinator": FakeCoordinator(),
        "codec": FakePageCodec(),
        "metrics": RecordingMetricsSink(),
        "vector_math": FakeVectorMath(),
        "events": RecordingEventSink(),
    }


@pytest.fixture
def complete_registry(fake_ports: dict[str, object]) -> PortRegistry:
    """Return a registry with every required slot bound to a fake adapter."""
    registry = PortRegistry()
    for slot, instance in fake_ports.items():
        registry.bind(slot, instance)
    return registry


# --- the dynamic half of G4: skips and disappearances pay a marker (A32, A36, A54, A55) -------
#
# Predicting "will this test be skipped for platform reasons?" from source is undecidable, so
# this half observes outcomes instead. It records every skip the run actually produces and every
# module that vanishes from collection, and fails the session unless each one is attributable.
#
# A32 keyed attribution on the skip REASON. A35 moved it to a module name inside that reason.
# Both were passwords, because the author writes both: a fabricated name that is honestly absent
# everywhere, or a real single-family stdlib module a denylist had not enumerated, each bought
# silence for a module of failing assertions. A denylist of platform-only modules can never be
# complete, so no rule resting on "is the named module absent?" can hold on its own.
#
# A54 replaces it. A skip is attributable ONLY by a marker that is
#
#   * visible in the SOURCE as a pytest marker (a ``mark.NAME`` chain), so the static half sees
#     the same evidence and can charge the counterpart price -- ``item.add_marker`` does not
#     count, and neither does an ordinary decorator that merely shares the name of a marker;
#   * present on the real marker set of the item, which only pytest can populate;
#   * REGISTERED in pyproject.toml, which ``--strict-markers`` then enforces.
#
# Three markers qualify:
#   1. platform_specific -- the static half demands a counterpart for the other family.
#   2. optional_dependency("<module>") -- the test DECLARES what it needs and the gate CHECKS it:
#      the named module must really be absent, and a single-family module never qualifies. The
#      marker is a claim under verification rather than a phrase under trust.
#   3. pending / xfail -- a declared, registered debt.
#
# A36 adds disappearance: collect_ignore, pytest_ignore_collect and a conftest deselection each
# remove a module with no report at all. A module holding tests that contributes no collected
# item has vanished -- unless it declares a module-level optional_dependency whose module really
# is absent, which is the honest idiom SPEC-VEC TR-6 requires for a run without an extra.
#
# A55: a filter is not a violation. Under -k, -m, --lf, --ff, --sw, --deselect or an explicit
# node id the caller asked for a subset, and a module contributing nothing is the filter working.
#
# Scope note: an x-failed test is not a skip. It ran, and its outcome was recorded.

def _normalise_distribution(name: str) -> str:
    """Return the PEP 503 normalised form of a distribution name."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _declared_optional_distributions(config: pytest.Config) -> frozenset[str]:
    """Return the distributions pyproject.toml declares under optional-dependencies (A54.1).

    This is the closed set the rule tests against. An author can write any module name they
    like into a marker, but they cannot add a name here without editing the packaging manifest,
    which is the whole point: three rounds of attribution rules failed because each asked an
    open-world question whose answer the author chose.
    """
    manifest_path = pathlib.Path(str(config.rootpath)) / "pyproject.toml"
    try:
        manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return frozenset()
    extras = manifest.get("project", {}).get("optional-dependencies", {})
    declared: set[str] = set()
    for requirements in extras.values():
        for requirement in requirements:
            name = re.split(r"[<>=!~;\[\s]", requirement, maxsplit=1)[0]
            if name:
                declared.add(_normalise_distribution(name))
    return frozenset(declared)


PLATFORM_MARKER: str = "platform_specific"
DEPENDENCY_MARKER: str = "optional_dependency"
DEBT_MARKERS: frozenset[str] = frozenset({"pending", "xfail"})
ATTRIBUTING_MARKERS: frozenset[str] = (
    frozenset({PLATFORM_MARKER, DEPENDENCY_MARKER}) | DEBT_MARKERS
)
"""The complete set of markers that can attribute a skip (A54).

Widening this set switches G4 off in one token, so a test pins it (A56).
"""

SELECTION_OPTIONS: tuple[str, ...] = (
    "keyword",
    "markexpr",
    "lf",
    "last_failed",
    "failedfirst",
    "ff",
    "stepwise",
    "sw",
    "stepwise_skip",
    "deselect",
    "lastfailed",
    "ignore",
    "ignore_glob",
)
"""Options through which a caller asks for a subset of the suite (A55)."""

SELECTION_FLAGS: tuple[str, ...] = (
    "-k",
    "-m",
    "--lf",
    "--last-failed",
    "--ff",
    "--failed-first",
    "--sw",
    "--stepwise",
    "--stepwise-skip",
    "--deselect",
    "--ignore",
    "--ignore-glob",
    "--collect-only",
    "--co",
)
"""The same request, seen on the command line rather than through the parsed options."""

_SOURCE_MARKERS: dict[tuple[str, str], dict[str, tuple[str, ...]]] = {}
_ATTRIBUTED: dict[str, str] = {}
_UNATTRIBUTED: list[tuple[str, str]] = []
_REASONS: dict[str, str] = {}
_COLLECTED_FILES: set[str] = set()
_ITEMS_BY_ID: dict[str, pytest.Item] = {}
_VANISHED: list[str] = []
_ERRORED_FILES: set[str] = set()
_COLLECTED_IDS: dict[str, pytest.Item] = {}
_REPORTED_IDS: set[str] = set()
_DESELECTED_IDS: set[str] = set()
_DECLARED_DISTRIBUTIONS: frozenset[str] = frozenset()
_EXPECTED_MODULES: list[pathlib.Path] = []
_INVOCATION_ARGS: list[str] = []
_USER_FILTERED: bool = False


def _marker_of(expression: ast.expr) -> dict[str, tuple[str, ...]]:
    """Return the single pytest marker an expression spells, or an empty mapping.

    Only a ``mark.NAME`` chain counts. A plain decorator that happens to be named ``pending``
    registers nothing, is not on the marker set of the item, and buys nothing.
    """
    call = expression if isinstance(expression, ast.Call) else None
    target = call.func if call is not None else expression
    if not isinstance(target, ast.Attribute):
        return {}
    owner = target.value
    spelled_as_marker = (isinstance(owner, ast.Attribute) and owner.attr == "mark") or (
        isinstance(owner, ast.Name) and owner.id == "mark"
    )
    if not spelled_as_marker:
        return {}
    arguments: tuple[str, ...] = ()
    if call is not None:
        arguments = tuple(
            argument.value
            for argument in call.args
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
        )
    return {target.attr: arguments}


def _marker_calls(node: ast.AST) -> dict[str, tuple[str, ...]]:
    """Return the pytest markers a definition carries, mapped to their literal string arguments."""
    found: dict[str, tuple[str, ...]] = {}
    for decorator in getattr(node, "decorator_list", []):
        found.update(_marker_of(decorator))
    return found


def _module_markers(tree: ast.Module) -> dict[str, tuple[str, ...]]:
    """Return the markers a module applies to every test through pytestmark."""
    found: dict[str, tuple[str, ...]] = {}
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in statement.targets
        ):
            continue
        values = (
            list(statement.value.elts)
            if isinstance(statement.value, (ast.List, ast.Tuple))
            else [statement.value]
        )
        for value in values:
            found.update(_marker_of(value))
    return found


def _parse(path: str) -> ast.Module | None:
    """Return the syntax tree of a module, or None when it cannot be read."""
    try:
        return ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None


def _source_markers(path: str, test_name: str) -> dict[str, tuple[str, ...]]:
    """Return the markers written in the SOURCE for one test, with their string arguments."""
    key = (path, test_name)
    cached = _SOURCE_MARKERS.get(key)
    if cached is not None:
        return cached
    tree = _parse(path)
    if tree is None:
        _SOURCE_MARKERS[key] = {}
        return {}
    found = _module_markers(tree)

    def visit(container: ast.AST, inherited: dict[str, tuple[str, ...]]) -> None:
        for child in ast.iter_child_nodes(container):
            if isinstance(child, ast.ClassDef):
                merged = dict(inherited)
                merged.update(_marker_calls(child))
                visit(child, merged)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if child.name == test_name:
                    found.update(inherited)
                    found.update(_marker_calls(child))

    visit(tree, {})
    _SOURCE_MARKERS[key] = found
    return found


PYTEST_BUILTIN_MARKERS: frozenset[str] = frozenset(
    {"skip", "skipif", "parametrize", "usefixtures", "filterwarnings", "tryfirst", "trylast"}
)
"""Markers pytest registers for itself, which no project had to declare.

They appear in ``getini("markers")`` alongside the project ones, so without this subtraction
adding ``skipif`` to ATTRIBUTING_MARKERS would switch G4 off in a single token: every ordinary
skipif test would attribute itself. ``xfail`` is deliberately absent, because A54 names it as a
declared debt.
"""


def _registered_markers(config: pytest.Config) -> set[str]:
    """Return the markers this project registers, plus the one pytest builtin that attributes."""
    names = {
        entry.split(":", 1)[0].split("(", 1)[0].strip()
        for entry in config.getini("markers")
    }
    return (names - PYTEST_BUILTIN_MARKERS) | {"xfail"}


PLATFORM_READS: frozenset[tuple[str, str]] = frozenset(
    {
        ("sys", "platform"),
        ("os", "name"),
        ("os", "uname"),
        ("platform", "system"),
        ("platform", "machine"),
    }
)
"""Direct readings of the running operating system."""

PLATFORM_NAME_HINTS: tuple[str, ...] = (
    "WINDOWS",
    "WIN32",
    "POSIX",
    "LINUX",
    "DARWIN",
    "MACOS",
    "CYGWIN",
    "PLATFORM",
)
"""Spellings that make a name a platform flag, wherever it was defined or imported from."""


PLATFORM_DOMAINS: dict[str, frozenset[str]] = {
    "sys.platform": frozenset({"win32", "linux", "darwin"}),
    "os.name": frozenset({"nt", "posix"}),
    "platform.system": frozenset({"Windows", "Linux", "Darwin"}),
}
"""The families this project targets, per reading (D9, and B1 of the round-10 audit).

Not the set of values a reading CAN return -- that set includes ``emscripten``, ``aix`` and
``wasi``, so ``skipif(sys.platform != "emscripten")`` was a legitimate member that runs on no
target we ship to, attributed-skipped on every family forever. The closed set has to be the
support matrix, which is a fact about the project rather than about CPython.

Kept per reading, so ``os.name == "win32"`` is refused: the value is real, the pairing is not.
"""

PLATFORM_VALUES: frozenset[str] = frozenset().union(*PLATFORM_DOMAINS.values())
"""The union, for the pins that assert what the closed set does and does not hold."""

PLATFORM_READINGS: frozenset[tuple[str, str]] = frozenset(
    {("sys", "platform"), ("os", "name"), ("platform", "system"), ("platform", "machine")}
)


def _is_platform_reading(node: ast.expr) -> bool:
    """Return True when the expression reads which operating system is running."""
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
        return (target.value.id, target.attr) in PLATFORM_READINGS
    return False


def _platform_comparison(node: ast.expr) -> tuple[str, str, tuple[str, ...]] | None:
    """Return (reading, operator, values) when the WHOLE node is one platform comparison.

    A whitelist of the entire expression, not a search for a matching part. Walking the tree and
    returning True on any sub-expression accepted ``IS_WINDOWS or True`` -- true on every family,
    so the test ran on none -- and let a rejected constant be rescued by adding ``or`` in front
    of it. A89 says the closed set binds the whole value; this is that rule applied to shape.
    """
    if not isinstance(node, ast.Compare) or len(node.ops) != 1:
        return None
    operator = node.ops[0]
    if not isinstance(operator, (ast.Eq, ast.NotEq, ast.In, ast.NotIn)):
        return None
    left, right = node.left, node.comparators[0]
    for reading, literal in ((left, right), (right, left)):
        if not _is_platform_reading(reading):
            continue
        name = _reading_name(reading)
        domain = PLATFORM_DOMAINS.get(name)
        if domain is None:
            continue
        if isinstance(literal, ast.Constant) and literal.value in domain:
            return (name, type(operator).__name__, (literal.value,))
        if isinstance(literal, (ast.Tuple, ast.List, ast.Set)):
            items = [item for item in literal.elts if isinstance(item, ast.Constant)]
            if items and len(items) == len(literal.elts):
                if all(item.value in domain for item in items):
                    return (name, type(operator).__name__, tuple(i.value for i in items))
    return None


def _reading_name(node: ast.expr) -> str:
    """Return a stable label for a platform reading, so it can be evaluated later."""
    target = node.func if isinstance(node, ast.Call) else node
    assert isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
    return f"{target.value.id}.{target.attr}"


def _family_shape(
    node: ast.expr, path: str, source: str | None = None, depth: int = 0
) -> tuple[str, str, tuple[str, ...], bool] | None:
    """Return the family claim a condition makes, or None when it makes none.

    Exactly three shapes are admitted, and nothing else: a platform comparison, a negation of
    one, or a name bound at module level to one of those. Any BoolOp, any call, any mixed
    expression is refused whatever it contains.
    """
    if depth > 8:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            node = ast.parse(node.value, mode="eval").body
        except SyntaxError:
            return None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        inner = _family_shape(node.operand, path, source, depth + 1)
        if inner is None:
            return None
        reading, operator, values, negated = inner
        return (reading, operator, values, not negated)
    comparison = _platform_comparison(node)
    if comparison is not None:
        reading, operator, values = comparison
        return (reading, operator, values, False)
    if isinstance(node, ast.Name):
        return _bound_family_shape(node.id, path, source, depth + 1)
    return None


def _module_level_bindings(tree: ast.Module, name: str) -> list[tuple[str, ast.expr | None]]:
    """Return every module-level binding of a name, with its value where it has one.

    Descends the containers that do not open a scope, so a rebinding inside ``if True:`` counts.
    A binding with no readable value (a del, an augmented assignment, a tuple unpack, a walrus,
    an import) is recorded as a sentinel: it still makes the name ambiguous.
    """
    bindings: list[tuple[str, ast.expr | None]] = []

    def walk(body: list[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == name:
                        bindings.append(("value", node.value))
                    elif isinstance(target, (ast.Tuple, ast.List)):
                        for element in ast.walk(target):
                            if isinstance(element, ast.Name) and element.id == name:
                                bindings.append(("opaque", None))
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name) and node.target.id == name:
                    bindings.append(("value", node.value) if node.value else ("opaque", None))
            elif isinstance(node, ast.AugAssign):
                if isinstance(node.target, ast.Name) and node.target.id == name:
                    bindings.append(("opaque", None))
            elif isinstance(node, ast.Delete):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == name:
                        bindings.append(("opaque", None))
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    if (alias.asname or alias.name.split(".")[0]) == name:
                        bindings.append(("import", None))
            if isinstance(node, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                for attribute in ("body", "orelse", "finalbody"):
                    walk(getattr(node, attribute, []))
                for handler in getattr(node, "handlers", []):
                    walk(handler.body)
        for node in body:
            for element in ast.walk(node):
                if isinstance(element, ast.NamedExpr):
                    if isinstance(element.target, ast.Name) and element.target.id == name:
                        bindings.append(("opaque", None))

    walk(tree.body)
    return bindings


def _bound_family_shape(
    name: str, path: str, source: str | None, depth: int
) -> tuple[str, str, tuple[str, ...], bool] | None:
    """Resolve a name to the family claim it holds, here or where it was imported from."""
    if depth > 8:
        return None
    if source is not None:
        try:
            tree: ast.Module | None = ast.parse(source)
        except SyntaxError:
            tree = None
    else:
        tree = _parse(path)
    if tree is None:
        return None
    bindings = _module_level_bindings(tree, name)
    if len(bindings) > 1:
        # Two bindings is a refusal, not a puzzle. pytest evaluates the LAST one, and following
        # a rebinding is a dataflow analysis that loses: FAMILY = sys.platform != "linux"
        # followed by FAMILY = False kept the claim and changed the outcome, in eight spellings.
        # One unambiguous binding is a closed condition; anything else is not.
        return None
    if len(bindings) == 1:
        kind, value = bindings[0]
        if kind == "value" and value is not None:
            return _family_shape(value, path, source, depth + 1)
        if kind == "opaque":
            return None
        # kind == "import": the single binding comes from elsewhere, so follow it below.
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        for alias in node.names:
            if (alias.asname or alias.name) != name:
                continue
            origin = _module_file(node.module)
            if origin is not None:
                return _bound_family_shape(alias.name, str(origin), None, depth + 1)
    return None


def _reading_value(reading: str) -> str | None:
    """Return what a platform reading evaluates to on THIS interpreter."""
    if reading == "sys.platform":
        return sys.platform
    if reading == "os.name":
        return os.name
    if reading == "platform.system":
        return platform.system()
    if reading == "platform.machine":
        return platform.machine()
    return None


def _condition_holds_here(claim: tuple[str, str, tuple[str, ...], bool]) -> bool | None:
    """Return whether a family claim is true on the running platform, or None if unknown."""
    reading, operator, values, negated = claim
    actual = _reading_value(reading)
    if actual is None:
        return None
    if operator in {"Eq", "In"}:
        result = actual in values
    elif operator in {"NotEq", "NotIn"}:
        result = actual not in values
    else:
        return None
    return (not result) if negated else result


def _module_file(module_name: str) -> pathlib.Path | None:
    """Return the file backing an importable module, without importing it."""
    try:
        spec = importlib.util.find_spec(module_name)
    except (ImportError, ValueError, AttributeError):
        return None
    if spec is None or not spec.origin or not spec.origin.endswith(".py"):
        return None
    return pathlib.Path(spec.origin)


def _condition_names_a_family(
    node: ast.expr, path: str, source: str | None = None
) -> bool:
    """Return True when a skipif condition names a real operating-system family (A89).

    ``source`` lets a caller resolve names against text it already holds, which is what the
    static half needs for a synthetic module that has no file on disk.
    """
    return _family_shape(node, path, source) is not None


def _reads_the_platform(node: ast.AST) -> bool:
    """Return True when an expression asks which operating system is running.

    A string condition is source that pytest evaluates, so it is parsed rather than walked as an
    opaque constant -- the static half already blesses that spelling, and A32 forbids the two
    halves from disagreeing about what is correct.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            node = ast.parse(node.value, mode="eval").body
        except SyntaxError:
            return False
    for element in ast.walk(node):
        if isinstance(element, ast.Attribute):
            owner = element.value
            if isinstance(owner, ast.Name) and (owner.id, element.attr) in PLATFORM_READS:
                return True
        if isinstance(element, ast.Name):
            if any(hint in element.id.upper() for hint in PLATFORM_NAME_HINTS):
                return True
    return False


def _family_claims(
    path: str, test_name: str, source: str | None = None
) -> list[tuple[str, str, tuple[str, ...], bool]]:
    """Return the family claims a test declares through its skipif conditions."""
    module_source = source
    if module_source is None:
        try:
            module_source = pathlib.Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            module_source = None
    tree = _parse(path) if source is None else None
    if tree is None and module_source is not None:
        try:
            tree = ast.parse(module_source)
        except SyntaxError:
            tree = None
    if tree is None:
        return []

    def conditions_of(node: ast.AST) -> list[ast.expr]:
        found: list[ast.expr] = []
        for decorator in getattr(node, "decorator_list", []):
            if not isinstance(decorator, ast.Call):
                continue
            target = decorator.func
            if not (isinstance(target, ast.Attribute) and target.attr == "skipif"):
                continue
            found.extend(decorator.args)
            found.extend(
                keyword.value for keyword in decorator.keywords if keyword.arg == "condition"
            )
        return found

    module_level: list[ast.expr] = []
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in statement.targets
        ):
            values = (
                list(statement.value.elts)
                if isinstance(statement.value, (ast.List, ast.Tuple))
                else [statement.value]
            )
            for value in values:
                if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute):
                    if value.func.attr == "skipif":
                        module_level.extend(value.args)
                        module_level.extend(
                            keyword.value
                            for keyword in value.keywords
                            if keyword.arg == "condition"
                        )

    claims: list[tuple[str, str, tuple[str, ...], bool]] = []

    def visit(container: ast.AST, inherited: list[ast.expr]) -> None:
        for child in ast.iter_child_nodes(container):
            if isinstance(child, ast.ClassDef):
                visit(child, inherited + conditions_of(child))
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if child.name != test_name:
                    continue
                for condition in inherited + conditions_of(child):
                    claim = _family_shape(condition, path, module_source)
                    if claim is not None:
                        claims.append(claim)

    visit(tree, module_level)
    return claims


def _names_a_family(path: str, test_name: str, source: str | None = None) -> bool:
    """Return True when the test declares WHICH family it runs on, through a skipif condition.

    A54 pays a marker instead of prose because a marker carries a claim the gate can check. A
    bare ``platform_specific`` carries none: it names no family, so the static half can demand no
    counterpart and the mark costs nothing. That is the password again, in the newest envelope.
    """
    module_source = source
    if module_source is None:
        try:
            module_source = pathlib.Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            module_source = None
    tree = _parse(path) if source is None else None
    if tree is None and module_source is not None:
        try:
            tree = ast.parse(module_source)
        except SyntaxError:
            tree = None
    if tree is None:
        return False

    def conditions_of(node: ast.AST) -> list[ast.expr]:
        found: list[ast.expr] = []
        for decorator in getattr(node, "decorator_list", []):
            if not isinstance(decorator, ast.Call):
                continue
            target = decorator.func
            if not (isinstance(target, ast.Attribute) and target.attr == "skipif"):
                continue
            found.extend(decorator.args)
            found.extend(
                keyword.value for keyword in decorator.keywords if keyword.arg == "condition"
            )
        return found

    def visit(container: ast.AST, inherited: list[ast.expr]) -> bool:
        for child in ast.iter_child_nodes(container):
            if isinstance(child, ast.ClassDef):
                if visit(child, inherited + conditions_of(child)):
                    return True
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if child.name != test_name:
                    continue
                for condition in inherited + conditions_of(child):
                    if _condition_names_a_family(condition, path, module_source):
                        return True
        return False

    module_level: list[ast.expr] = []
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in statement.targets
        ):
            values = (
                list(statement.value.elts)
                if isinstance(statement.value, (ast.List, ast.Tuple))
                else [statement.value]
            )
            for value in values:
                if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute):
                    if value.func.attr == "skipif":
                        module_level.extend(value.args)
                        module_level.extend(
                            keyword.value
                            for keyword in value.keywords
                            if keyword.arg == "condition"
                        )
    return visit(tree, module_level)


def _module_is_absent(name: str) -> bool:
    """Return True when the named module genuinely cannot be imported on this interpreter."""
    # A name that is not a single identifier cannot be a distribution. A54.1 asked for a
    # closed set; A89 adds that the set must bind the WHOLE value, so a dotted path is refused
    # outright rather than checked one segment at a time.
    if not name or not name.isidentifier():
        return False
    try:
        return importlib.util.find_spec(name) is None
    except ModuleNotFoundError:
        return True
    except ValueError:
        return False


def _attribution(item: pytest.Item | None) -> str | None:
    """Return why a skip is allowed, or None when no marker attributes it (A54).

    The reason string is never consulted: it is prose the author writes, and prose was the
    password A54 exists to withdraw.
    """
    if item is None:
        return None
    path = str(getattr(item, "path", "") or getattr(item, "fspath", ""))
    name = getattr(item, "originalname", None) or item.name.split("[")[0]
    written = _source_markers(path, name)
    registered = _registered_markers(item.config)
    live = {marker.name for marker in item.iter_markers()}
    claimed = set(written) & ATTRIBUTING_MARKERS & registered & live
    if PLATFORM_MARKER in claimed:
        claims = _family_claims(path, name)
        # The marker must describe the skip that HAPPENED. A condition that is false here says
        # the test should run on this family, so a skip from it is not the family talking -- it
        # is an imperative skip wearing a marker that does not cover it. Asking only whether a
        # family was NAMED let one added line vanish any correctly paired test.
        if any(_condition_holds_here(claim) is True for claim in claims):
            return "platform_specific, and its condition holds on this platform"
    if claimed & DEBT_MARKERS:
        return "declared debt with a registered marker"
    if DEPENDENCY_MARKER in claimed:
        declared = _declared_optional_distributions(item.config)
        for module in written.get(DEPENDENCY_MARKER, ()):
            # A89: the closed set binds the WHOLE argument, and the absence check uses the same
            # identity. Binding the root only turned every declared distribution into an
            # unbounded namespace, because find_spec("numpy.anything") is None for an INSTALLED
            # numpy -- one invariant, two branches, two identities (A66.1).
            if _normalise_distribution(module) not in declared:
                continue
            if _module_is_absent(module.replace("-", "_")):
                return f"declared optional dependency {module!r} is absent"
    return None


def _declared_absent(path: pathlib.Path) -> bool:
    """Return True when a module declares an optional dependency that really is absent."""
    tree = _parse(str(path))
    if tree is None:
        return False
    modules = _module_markers(tree).get(DEPENDENCY_MARKER, ())
    declared = _DECLARED_DISTRIBUTIONS
    return any(
        _normalise_distribution(module) in declared
        and _module_is_absent(module.replace("-", "_"))
        for module in modules
    )


def _record_skip(nodeid: str, item: pytest.Item | None) -> None:
    """Record a skip unless a source-visible registered marker attributes it."""
    attribution = _attribution(item)
    if attribution is not None:
        _ATTRIBUTED[nodeid] = attribution
        return
    entry = (nodeid, _REASONS.get(nodeid, "no reason given"))
    if entry not in _UNATTRIBUTED:
        _UNATTRIBUTED.append(entry)


def _skip_reason(report: object) -> str:
    """Return the reason a report carries, for the REPORT only -- never for attribution."""
    longrepr = getattr(report, "longrepr", None)
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        text = str(longrepr[2])
    else:
        text = str(longrepr) if longrepr is not None else ""
    for prefix in ("Skipped: ", "Skipped "):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    return text.strip() or "no reason given"


REFERENCE_ENVIRONMENT: str = "OKTO_G4_REFERENCE"
"""Suppresses the reference collection, so a child cannot spawn another one.

    It disables the extra COLLECTION, never the gate. Disabling the gate here made every planted
    probe in the suite pay for a second pytest run it had no use for, and the run time went from
    two minutes to over ten.
    """


def _reference_collection(config: pytest.Config) -> set[str] | None:
    """Return the files pytest itself collects for this invocation, or None when unavailable.

    A36 asked for an expectation that cannot drift from pytest, and the way not to drift from
    pytest is to ask pytest. Counting ``def test_*`` nodes instead missed seven shapes it
    collects and runs -- a test built by a factory, a def under ``if True:``, a class made with
    ``type()``, a test inherited from a base the patterns do not match -- and each of those
    modules could then be removed with nothing paid at all.
    """
    if os.environ.get(REFERENCE_ENVIRONMENT):
        return None
    arguments = [argument for argument in _INVOCATION_ARGS if not argument.startswith("-")]
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
             *arguments],
            cwd=str(config.rootpath),
            capture_output=True,
            text=True,
            timeout=300,
            env={**os.environ, REFERENCE_ENVIRONMENT: "1"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode not in (0, 1, 2, 5):
        return None
    collected: set[str] = set()
    for line in result.stdout.splitlines():
        candidate = line.split("::", 1)[0].strip()
        if candidate.endswith(".py"):
            collected.add(str((pathlib.Path(config.rootpath) / candidate).resolve()))
    return collected or None


def _matches_pytest_option(name: str, patterns: list[str]) -> bool:
    """Match a name the way pytest does: a glob if the pattern has glob characters, else a prefix.

    Reimplementing the rule as ``startswith("test_")`` missed ``def testalpha``, which pytest
    collects and runs under the default ``python_functions = ["test"]`` prefix. A module holding
    only such tests was invisible to the expectation, so removing it cost nothing at all.
    """
    for pattern in patterns:
        if set(pattern) & {"*", "?", "["}:
            if fnmatch.fnmatch(name, pattern):
                return True
        elif name.startswith(pattern):
            return True
    return False


def _test_functions(path: pathlib.Path, config: pytest.Config | None = None) -> int:
    """Return whether a module looks like it holds tests, for the floor under the reference pass.

    Deliberately generous and deliberately not a count of anything. The reference collection is
    the authority; this only has to avoid the undercount that let a module be removed for free,
    so a name matching pytest's patterns ANYWHERE -- at any nesting, or bound by an assignment
    rather than a def -- is enough to expect the module to contribute something.
    """
    tree = _parse(str(path))
    if tree is None:
        return 0
    if config is not None:
        functions = list(config.getini("python_functions")) or ["test"]
        classes = list(config.getini("python_classes")) or ["Test"]
    else:
        functions, classes = ["test"], ["Test"]

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _matches_pytest_option(node.name, functions):
                return 1
        elif isinstance(node, ast.ClassDef):
            if _matches_pytest_option(node.name, classes):
                return 1
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            if _matches_pytest_option(node.id, functions) or _matches_pytest_option(
                node.id, classes
            ):
                return 1
    return 0


def _snapshot_expected_modules(config: pytest.Config) -> list[pathlib.Path]:
    """Return every module the invocation should collect, read BEFORE any conftest runs.

    Taken at command-line time on purpose: an expectation rebuilt at session end cannot see a
    module a conftest deleted on one family, and A36 asks for a family-independent one.
    """
    patterns = config.getini("python_files") or ["test_*.py"]
    roots = [
        pathlib.Path(argument.split("::")[0])
        for argument in _INVOCATION_ARGS
        if not argument.startswith("-")
    ]
    roots = [root for root in roots if root.exists()]
    if not roots:
        # testpaths is what pytest itself would collect with no arguments. Walking rootpath
        # instead swept bench/, tools/ and any stale build/ copy into the expectation, and a
        # green tree then failed because C13 owns bench/ (A55).
        rootpath = pathlib.Path(str(config.rootpath))
        roots = [rootpath / entry for entry in config.getini("testpaths")] or [rootpath]
        roots = [root for root in roots if root.exists()]
    expected: set[pathlib.Path] = set()
    reference = _reference_collection(config)
    if reference is not None:
        expected.update(pathlib.Path(entry) for entry in reference)
    for root in roots:
        candidates = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for candidate in candidates:
            if not any(fnmatch.fnmatch(candidate.name, pattern) for pattern in patterns):
                continue
            if _test_functions(candidate, config):
                expected.add(candidate.resolve())
    return sorted(expected)


def _detect_user_filter(config: pytest.Config) -> bool:
    """Return True when the caller asked for a subset of the suite (A55)."""
    option = config.option
    for name in SELECTION_OPTIONS:
        value = getattr(option, name, None)
        if isinstance(value, bool) and value:
            return True
        if isinstance(value, str) and value:
            return True
        if isinstance(value, (list, tuple)) and value:
            return True
    for argument in _INVOCATION_ARGS:
        if argument in SELECTION_FLAGS:
            return True
        if any(argument.startswith(flag + "=") for flag in SELECTION_FLAGS):
            return True
        if "::" in argument:
            return True
    return False


def pytest_cmdline_main(config: pytest.Config) -> None:
    """Snapshot the invocation and the expected modules before any conftest can rewrite them."""
    _INVOCATION_ARGS.extend(config.invocation_params.args)
    global _USER_FILTERED, _DECLARED_DISTRIBUTIONS
    _USER_FILTERED = _detect_user_filter(config)
    _DECLARED_DISTRIBUTIONS = _declared_optional_distributions(config)
    _EXPECTED_MODULES.extend(_snapshot_expected_modules(config))


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Remember which modules produced collected tests."""
    for item in items:
        path = str(getattr(item, "path", "") or getattr(item, "fspath", ""))
        if path:
            _COLLECTED_FILES.add(str(pathlib.Path(path).resolve()))


def pytest_itemcollected(item: pytest.Item) -> None:
    """Record every item at the moment pytest collects it (A69, D2).

    Fired per item DURING collection, before any pytest_collection_modifyitems and before any
    pytest_collection_finish, so there is no earlier hook from which an item could be removed.
    Capturing at collection_finish instead watched "what survived up to my hook": a nested
    conftest registers after the parent, so among equal-priority tryfirst implementations the
    nested one runs first, and one decorator on an otherwise identical hook body walked a whole
    module of failing assertions through at exit 0.
    """
    _COLLECTED_IDS[item.nodeid] = item


def pytest_deselected(items: list[pytest.Item]) -> None:
    """Record a deselection nobody asked for, which is how a module leaves without a report."""
    for item in items:
        _DESELECTED_IDS.add(item.nodeid)
    if _USER_FILTERED:
        return
    for item in items:
        _REASONS[item.nodeid] = "deselected without a user filter"
        _record_skip(item.nodeid, item)


def pytest_runtest_protocol(item: pytest.Item) -> None:
    """Keep the item beside its node id, so a report can be attributed to written markers."""
    _ITEMS_BY_ID[item.nodeid] = item


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Record every skipped test, and note that this node produced a report at all (A69)."""
    _REPORTED_IDS.add(report.nodeid)
    if hasattr(report, "wasxfail"):
        # An x-fail is the same invariant as a skip: the marker must be visible in the source,
        # so a collection-time add_marker(xfail) cannot suppress a failure. Enforcing it for
        # skip and not for xfail was two paths for one rule.
        item = _ITEMS_BY_ID.get(report.nodeid)
        if _attribution(item) is None:
            _REASONS.setdefault(report.nodeid, "x-failed without a source-visible marker")
            _record_skip(report.nodeid, item)
        return
    if not report.skipped:
        return
    _REASONS.setdefault(report.nodeid, _skip_reason(report))
    _record_skip(report.nodeid, _ITEMS_BY_ID.get(report.nodeid))


def pytest_collectreport(report: pytest.CollectReport) -> None:
    """Record a module skipped at collection time, where no item exists to carry a marker."""
    if report.failed:
        # A module that fails to import has already been reported loudly, with a non-zero exit
        # of its own. It did not vanish silently, so the disappearance rule must not pile on.
        location = getattr(report, "fspath", None)
        if location is not None:
            _ERRORED_FILES.add(str(pathlib.Path(str(location)).resolve()))
        return
    if not report.skipped:
        return
    location = getattr(report, "fspath", None)
    if location is not None and _declared_absent(pathlib.Path(str(location))):
        _ATTRIBUTED[report.nodeid] = "module declares an absent optional dependency"
        return
    _REASONS.setdefault(report.nodeid, _skip_reason(report))
    _record_skip(report.nodeid, None)


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    """Name every unattributed skip and every vanished module, so the report is actionable."""
    if not _UNATTRIBUTED and not _VANISHED:
        return
    terminalreporter.section("unattributed skips (CONTRACT.md G4, A32/A36/A54)", red=True)
    if _UNATTRIBUTED:
        terminalreporter.write_line(
            f"{len(_UNATTRIBUTED)} skipped test(s) carry neither a source-visible registered "
            f"marker from {sorted(ATTRIBUTING_MARKERS)} nor a verified absent dependency:"
        )
        for nodeid, reason in _UNATTRIBUTED[:50]:
            terminalreporter.write_line(f"  {nodeid}: {reason}")
        if len(_UNATTRIBUTED) > 50:
            terminalreporter.write_line(f"  ... and {len(_UNATTRIBUTED) - 50} more")
    if _VANISHED:
        terminalreporter.write_line(
            f"{len(_VANISHED)} module(s) holding tests vanished from collection with no report:"
        )
        for module in _VANISHED[:50]:
            terminalreporter.write_line(f"  {module}")


def _reconcile(session: pytest.Session) -> list[str]:
    """Return the collected node ids that never produced a report of any kind (A69).

    The backstop. Every earlier rule watches one MECHANISM by which a test stops running, and
    pytest has more hooks than this file has rules; this asks the outcome question instead --
    did it run? -- so a hook nobody has thought of yet is covered by construction.
    """
    if session.config.option.collectonly:
        return []
    if session.testsfailed and getattr(session.config.option, "maxfail", 0):
        # The run was cut short on purpose; the tests after the stop never ran by request.
        return []
    if getattr(session, "shouldstop", False) or getattr(session, "shouldfail", False):
        return []
    silent: list[str] = []
    for nodeid, item in _COLLECTED_IDS.items():
        if nodeid in _REPORTED_IDS or nodeid in _DESELECTED_IDS:
            continue
        if _attribution(item) is not None:
            continue
        silent.append(nodeid)
    return silent


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail the session when a skip or a disappearance could not be attributed."""
    for nodeid in _reconcile(session):
        entry = (nodeid, "collected but never reported")
        if entry not in _UNATTRIBUTED:
            _UNATTRIBUTED.append(entry)
    if not _USER_FILTERED:
        for module in _EXPECTED_MODULES:
            if str(module) in _COLLECTED_FILES or str(module) in _ERRORED_FILES:
                continue
            if _declared_absent(module):
                continue
            _VANISHED.append(str(module))
    if (_UNATTRIBUTED or _VANISHED) and exitstatus == 0:
        session.exitstatus = 1
