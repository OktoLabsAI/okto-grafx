# Okto Grafx — Frozen Architecture Contract (v1)

**This document is normative and FROZEN.** Every builder agent implements against it; every critic
agent reviews against it. If a builder believes the contract is wrong, it must STOP and report the
conflict rather than silently deviating — a unilateral interface change breaks every other component.

Authority chain: `docs/specs/SPEC-M1.md` and `docs/specs/SPEC-VEC.md` are the requirements;
this contract is the single agreed realization of them.

---

## 0. Non-negotiables (from the board guidelines and binding decisions D1–D9)

| ID | Rule | Enforcement |
|----|------|-------------|
| G1 | **All product surfaces are en-US**: class names, function names, exception messages, metric names & descriptions, CLI help, docstrings. | Code review + `tests/test_language_surface.py` |
| G2 | **Hexagonal**: `okto_grafx/domain/**` and `okto_grafx/engine/**` contain NO mechanism. Forbidden: `open()`, `os`, `pathlib` I/O, `mmap`, `socket`, `sys.platform`, `os.name`, `threading`, `time.time`, `time.monotonic`, `random` (unseeded), any third-party import. | `tests/test_import_boundary.py`, budget **ZERO**, fails closed |
| G3 | **Pure-Python core, single universal wheel.** Runtime deps: stdlib only. `numpy` only under extra `[accel]`, imported only in `adapters/vectormath_numpy.py`. `ladybug` only under extra `[bench]`. | `pyproject.toml` + import-boundary test |
| G4 | **Windows and POSIX are equal citizens.** No test may be silently skipped on a family; a family-specific test must be explicitly marked `@pytest.mark.platform_specific` and have a counterpart. | `tests/test_platform_parity.py` |
| G5 | **Fail-closed ports**: an unfilled port slot refuses startup with `GrafxPortNotConfigured`. No silent default, no no-op fallback (except the explicitly selected `NoOpMetricsSink`). | `runtime/registry.py` + tests |
| G6 | **No sanctioned operation destroys the main data file.** Recovery, quarantine, recycling and purge never move/rename/delete `heap.dat`, `catalog.dat` or `index/*`. | `tests/test_main_file_untouched.py` |
| G7 | **Metric is a contract**: `oktografx_` prefix, snake_case, unit suffix (`_seconds`/`_bytes`/`_total`/`_ratio`), en-US description, and every label must declare a bounded domain at registration. | `MetricRegistry.register()` raises |
| G8 | **Every discard leaves a trace**: a WAL record discarded by recovery ALWAYS produces a ledger entry. Missing entry is a test failure. | `tests/test_recovery_ledger_trace.py` |

---

## 1. Repository layout and component ownership

```
okto_grafx/
├── pyproject.toml
├── README.md
├── dashboards/
│   ├── oktografx-m1.json
│   └── oktografx-vector.json
├── docs/
│   ├── specs/{SPEC-M1.md,SPEC-VEC.md}
│   └── architecture/{CONTRACT.md,COMPONENTS.md,DECISIONS.md}
├── src/okto_grafx/
│   ├── __init__.py                 # C11 public API re-exports
│   ├── errors.py                   # C0 public alias of domain.errors
│   ├── domain/                     # PURE (G2)
│   │   ├── errors.py               # C0
│   │   ├── ids.py                  # C0
│   │   ├── ports/                  # C0  storage, coordination, clock, metrics, codec, vectormath, events
│   │   ├── model/                  # C1  values, schema, catalog model, record model
│   │   ├── page/                   # C1  page layout, slotted page, checksum
│   │   ├── wal/                    # C4  record format, codec, segment naming, replay decisions
│   │   ├── txn/                    # C5  snapshot, visibility, partitions, OCC validator
│   │   ├── ledger/                 # C6  entry model, classification
│   │   ├── recovery/               # C6  recovery state machine, report
│   │   ├── verify/                 # C6  verification walk, findings
│   │   ├── index/                  # C7  secondary-index contract, exact hash index logic
│   │   ├── vector/                 # C9  spaces, hnsw graph logic, two-regime planner
│   │   └── query/                  # C10 lexer, parser, ast, planner, operators
│   ├── engine/                     # PURE (G2) — orchestrates domain over ports
│   │   ├── buffer_pool.py          # C1
│   │   ├── heap_store.py           # C1
│   │   ├── catalog_store.py        # C1
│   │   ├── wal_manager.py          # C4
│   │   ├── txn_manager.py          # C5
│   │   ├── coordination.py         # C3
│   │   ├── recovery_manager.py     # C6
│   │   ├── ledger_store.py         # C6
│   │   ├── quarantine.py           # C6
│   │   ├── verifier.py             # C6
│   │   ├── index_manager.py        # C7
│   │   ├── metrics_catalog.py      # C8
│   │   ├── vector_engine.py        # C9
│   │   ├── query_engine.py         # C10
│   │   └── database.py             # C11 engine-level Database
│   ├── adapters/                   # MECHANISM LIVES HERE
│   │   ├── storage_local.py        # C2
│   │   ├── storage_memory.py       # C2
│   │   ├── storage_fault.py        # C2 deterministic fault-injecting twin (FR-16)
│   │   ├── coordination_local.py   # C3
│   │   ├── clock_system.py         # C3
│   │   ├── codec_v1.py             # C1
│   │   ├── metrics_noop.py         # C8
│   │   ├── metrics_openmetrics.py  # C8 (+ tiny stdlib http publisher)
│   │   ├── metrics_json.py         # C8
│   │   ├── events_logging.py       # C8
│   │   ├── vectormath_pure.py      # C9 THE ORACLE
│   │   └── vectormath_numpy.py     # C9 [accel]
│   ├── runtime/                    # composition root
│   │   ├── config.py               # C0
│   │   ├── registry.py             # C0
│   │   └── bootstrap.py            # C0/C11
│   ├── api/                        # C11 public facade
│   └── cli/                        # C12
├── bench/                          # C13 harness (extra [bench])
└── tests/
```

**Ownership rule:** a builder writes ONLY inside the paths listed for its component, plus its own
tests under `tests/<component>/`. Touching another component's files is a contract violation —
report the needed change instead.

---

## 2. Error taxonomy — `src/okto_grafx/domain/errors.py` (C0, verbatim)

```python
class GrafxError(Exception):
    """Base class for every error raised by Okto Grafx. Never let an adapter kill the host process."""
    code: str = "grafx_error"
    retryable: bool = False

    def __init__(self, message: str, *, retryable: bool | None = None, **details: object) -> None:
        super().__init__(message)
        self.message = message
        self.details = dict(details)
        if retryable is not None:
            self.retryable = retryable

    def __str__(self) -> str: ...     # "<message> [code=<code> retryable=<bool>]"
    def to_dict(self) -> dict[str, object]: ...
```

Concrete classes (`code`, `retryable`) — **exact names, en-US messages**:

| Class | code | retryable |
|---|---|---|
| `GrafxWriteConflict` | `write_conflict` | **True** |
| `GrafxLeaseTimeout` | `lease_timeout` | **True** |
| `GrafxLeaseStolen` | `lease_stolen` | False |
| `GrafxStaleEpoch` | `stale_epoch` | False |
| `GrafxCorruptionDetected` | `corruption_detected` | False |
| `GrafxDeviceFull` | `device_full` | **True** |
| `GrafxDurabilityBarrierFailed` | `durability_barrier_failed` | False |
| `GrafxRecoveryRefused` | `recovery_refused` | False |
| `GrafxBufferBudgetExceeded` | `buffer_budget_exceeded` | **True** |
| `GrafxSchemaVersionMismatch` | `schema_version_mismatch` | False |
| `GrafxPortNotConfigured` | `port_not_configured` | False |
| `GrafxTransactionStateError` | `transaction_state` | False |
| `GrafxLedgerError` | `ledger_error` | False |
| `GrafxQuarantineError` | `quarantine_error` | False |
| `GrafxIndexError` | `index_error` | False |
| `GrafxQueryError` | `query_error` | False |
| `GrafxVectorValidationError` | `vector_validation` | False |
| `GrafxEmbeddingSpaceMismatch` | `embedding_space_mismatch` | False |
| `GrafxSpaceRetired` | `space_retired` | False |
| `GrafxConfigurationError` | `configuration_error` | False |
| `GrafxUnsupportedOperation` | `unsupported_operation` | False |

`GrafxQueryError` gets subclasses `GrafxParseError` (`parse_error`) and `GrafxPlanError` (`plan_error`).

---

## 3. Identifiers — `domain/ids.py` (C0)

```python
Lsn      = int    # monotonic, starts at 1; 0 means "none"
Csn      = int    # commit sequence number == LSN of the COMMIT record
Epoch    = int    # writer epoch, starts at 1
TxnId    = int    # process-local monotonic; not durable identity
PageIndex= int    # 0-based within a file
SlotId   = int
RecordId = int    # stable logical identity across versions

NO_LSN: Lsn = 0
NO_CSN: Csn = 0
NO_PAGE: PageIndex = 0xFFFFFFFF

@dataclass(frozen=True, slots=True)
class RecordRef:      # physical location of one version
    page: PageIndex
    slot: SlotId
    def encode(self) -> int   # (page << 16) | slot
    @classmethod
    def decode(cls, raw: int) -> "RecordRef"
NULL_REF = RecordRef(NO_PAGE, 0)
```

---

## 4. Ports — `domain/ports/*.py` (C0, FROZEN signatures)

All ports are `typing.Protocol` with `@runtime_checkable`. All are synchronous.

### 4.1 `ports/storage.py`

```python
@runtime_checkable
class StorageDevice(Protocol):
    """Every byte the engine persists goes through here. There is deliberately NO
    positional write primitive: pages must be allocated first, logs are append-only.
    This makes the NTFS beyond-EOF zero-fill signature structurally impossible."""

    @property
    def name(self) -> str: ...
    @property
    def page_size(self) -> int: ...

    # --- namespace ---
    def exists(self, file: str) -> bool: ...
    def create(self, file: str, *, exclusive: bool = True) -> None: ...
    def remove(self, file: str) -> None: ...
    def list_files(self, prefix: str = "") -> tuple[str, ...]: ...
    def file_size(self, file: str) -> int: ...
    def atomic_replace(self, source: str, target: str) -> None: ...
    def recycle(self, file: str) -> bool:
        """Release a file. Returns True when the space is reclaimed immediately,
        False when the platform deferred it (Windows pending-delete). Never raises
        for a still-open handle."""

    # --- paged space ---
    def page_count(self, file: str) -> int: ...
    def allocate(self, file: str, count: int = 1) -> PageIndex:
        """Grow the file by `count` pages, zero-filled. Returns the first new index."""
    def read_page(self, file: str, page_index: PageIndex) -> bytes: ...
    def write_page(self, file: str, page_index: PageIndex, data: bytes) -> None:
        """`data` must be exactly page_size bytes and page_index must already be allocated,
        else GrafxCorruptionDetected / IndexError-free typed failure."""

    # --- append-only log space ---
    def append_log(self, file: str, payload: bytes) -> int:
        """Append and return the new total size. Partial append must raise GrafxDeviceFull."""
    def read_log(self, file: str, offset: int, length: int) -> bytes: ...
    def log_size(self, file: str) -> int: ...
    def truncate_log(self, file: str, size: int) -> None:
        """Shrink only. Growing is a programming error -> GrafxUnsupportedOperation."""

    # --- durability ---
    def durable_barrier(self, file: str | None = None) -> None:
        """fsync the file (and its directory on POSIX). None = every open file.
        Failure raises GrafxDurabilityBarrierFailed."""
```

### 4.2 `ports/clock.py`

```python
@runtime_checkable
class Clock(Protocol):
    def monotonic(self) -> float:
        """Local, never comparable across processes. The ONLY source for liveness/lease timing."""
    def wall(self) -> float:
        """Unix epoch seconds. For human-facing timestamps ONLY. Never for liveness."""
```

### 4.3 `ports/coordination.py`

```python
@dataclass(frozen=True, slots=True)
class Lease:
    owner_id: str
    epoch: Epoch
    acquired_monotonic: float
    heartbeat_seq: int
    ttl_seconds: float

@dataclass(frozen=True, slots=True)
class ReaderHandle:
    reader_id: str
    snapshot_lsn: Lsn

@dataclass(frozen=True, slots=True)
class DeadOwnerReport:
    owner_id: str
    last_heartbeat_seq: int
    observed_stall_seconds: float   # measured with the OBSERVER's own monotonic clock

@runtime_checkable
class ProcessCoordinator(Protocol):
    def owner_id(self) -> str: ...
    def current_epoch(self) -> Epoch: ...

    def acquire_writer_lease(self, *, timeout: float) -> Lease:
        """GrafxLeaseTimeout on expiry. Does NOT serialize whole transactions —
        it identifies the epoch holder."""
    def renew_lease(self, lease: Lease) -> Lease:
        """GrafxLeaseStolen if another owner took over."""
    def release_lease(self, lease: Lease) -> None: ...
    def validate_epoch(self, epoch: Epoch) -> None:
        """Raise GrafxStaleEpoch BEFORE any byte can reach the device."""

    def detect_dead_owner(self, *, stall_threshold: float) -> DeadOwnerReport | None: ...
    def takeover(self) -> Lease:
        """Increment epoch and become the owner. Must be atomic against concurrent takeovers."""

    def register_reader(self, snapshot_lsn: Lsn) -> ReaderHandle: ...
    def refresh_reader(self, handle: ReaderHandle) -> None: ...
    def unregister_reader(self, handle: ReaderHandle) -> None: ...
    def reader_horizon(self) -> Lsn | None:
        """Minimum snapshot_lsn over LIVE readers; None when there is no live reader.
        Dead readers (stalled heartbeat, observer-local monotonic) are pruned, never evicted."""

    def exclusive(self, name: str, *, timeout: float) -> AbstractContextManager[None]:
        """Short cross-process critical section. Used for the commit window and for takeover."""
```

### 4.4 `ports/metrics.py`

```python
class MetricKind(str, Enum):
    COUNTER = "counter"; GAUGE = "gauge"; HISTOGRAM = "histogram"

@dataclass(frozen=True, slots=True)
class LabelSpec:
    name: str
    allowed_values: frozenset[str] | None = None
    max_cardinality: int = 8
    # Registration REJECTS: forbidden names, or allowed_values is None with max_cardinality > 64.

@dataclass(frozen=True, slots=True)
class MetricDescriptor:
    name: str                 # must start with "oktografx_", snake_case, unit suffix
    kind: MetricKind
    description: str          # en-US, non-empty, ends with '.'
    unit: str = ""
    labels: tuple[LabelSpec, ...] = ()
    buckets: tuple[float, ...] = ()   # HISTOGRAM only

@runtime_checkable
class MetricsSink(Protocol):
    @property
    def enabled(self) -> bool:
        """False for the no-op sink; hot paths MUST guard with this to avoid allocation."""
    def register(self, descriptor: MetricDescriptor) -> None: ...
    def increment(self, name: str, value: float = 1.0, labels: Mapping[str, str] | None = None) -> None: ...
    def set_gauge(self, name: str, value: float, labels: Mapping[str, str] | None = None) -> None: ...
    def observe(self, name: str, value: float, labels: Mapping[str, str] | None = None) -> None: ...
    def time(self, name: str, labels: Mapping[str, str] | None = None) -> AbstractContextManager[None]: ...
    def snapshot(self) -> Mapping[str, object]:
        """Machine-readable current values; the CI gate reads THIS, never a log."""
```

Forbidden label names (rejected at registration): `node_id`, `record_id`, `id`, `key`, `path`,
`file`, `query`, `text`, `message`, `vector`, `embedding`, `uuid`, `lsn`, `offset`.

### 4.5 `ports/codec.py`

```python
@runtime_checkable
class PageCodec(Protocol):
    @property
    def format_version(self) -> int: ...
    def checksum(self, payload: bytes) -> int: ...              # CRC-32C (Castagnoli)
    def encode_page(self, page: "Page") -> bytes: ...           # returns exactly page_size bytes
    def decode_page(self, raw: bytes, *, verify: bool = True) -> "Page": ...
```

### 4.6 `ports/vectormath.py`

```python
class DistanceMetric(str, Enum):
    COSINE = "cosine"; DOT = "dot"; EUCLIDEAN = "euclidean"

@runtime_checkable
class VectorMath(Protocol):
    @property
    def name(self) -> str: ...
    def dot(self, a: Sequence[float], b: Sequence[float]) -> float: ...
    def cosine(self, a: Sequence[float], b: Sequence[float]) -> float: ...
    def euclidean(self, a: Sequence[float], b: Sequence[float]) -> float: ...
    def norm(self, a: Sequence[float]) -> float: ...
    def normalize(self, a: Sequence[float]) -> tuple[float, ...]: ...
    def score(self, a, b, metric: DistanceMetric) -> float:
        """HIGHER IS BETTER for every metric: cosine/dot as-is, euclidean returns -distance."""
    def top_k(self, query, candidates: Sequence[tuple[int, Sequence[float]]], k: int,
              metric: DistanceMetric) -> list[tuple[int, float]]:
        """Descending by score; ties broken by ascending candidate id for determinism."""
```

### 4.7 `ports/events.py`

```python
@runtime_checkable
class EventSink(Protocol):
    def emit(self, event: str, payload: Mapping[str, object]) -> None: ...
```

---

## 5. Runtime composition — `runtime/` (C0)

```python
@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    path: str                              # ":memory:" selects MemoryStorageDevice
    page_size: int = 8192
    partitions_per_table: int = 64         # calibrated by FR-15, frozen in calibration.json
    buffer_budget_bytes: int = 64 * 1024 * 1024
    recovery_policy: str = "replay"        # "replay" (DEFAULT) | "refuse"
    lease_ttl_seconds: float = 5.0
    lease_timeout_seconds: float = 10.0
    commit_lock_timeout_seconds: float = 30.0
    reader_stall_threshold_seconds: float = 15.0
    wal_segment_bytes: int = 4 * 1024 * 1024
    checkpoint_interval_records: int = 512
    metrics: str = "noop"                  # "noop" | "openmetrics" | "json"
    vector_math: str = "auto"              # "auto" | "pure" | "numpy"
    vector_exact_scan_threshold: int = 4096   # calibrated (SPEC-VEC FR-5/FR-8)
    vector_recall_target: float = 0.90        # calibrated
    read_only: bool = False

class PortRegistry:
    """Fail-closed (G5). Every required slot must be bound before open_database returns."""
    REQUIRED = ("storage", "clock", "coordinator", "codec", "metrics", "vector_math", "events")
    def bind(self, slot: str, instance: object) -> None: ...
    def get(self, slot: str) -> object:  # GrafxPortNotConfigured when empty
    def require_complete(self) -> None:  # GrafxPortNotConfigured listing every missing slot
```

`bootstrap.open_database(config, *, registry=None) -> engine.database.Database` builds the default
adapters when `registry is None`, then always calls `require_complete()`.

---

## 6. On-disk formats (FROZEN, little-endian)

### 6.1 Files inside a database directory

```
grafx.meta                 1 page: identity + format
heap.dat                   paged heap
catalog.dat                paged catalog
index/<index_name>.idx     paged secondary index
wal/<000000000001>.wal     append-only WAL segments
ledger/ledger.log          append-only unapplied-work ledger
quarantine/<stamp>-<name>/{manifest.json, <copied bytes>}
control/writer.lease       lease + epoch
control/readers/<id>.reader
control/commit.state       published {last_committed_lsn, last_csn, checkpoint_lsn} (atomic_replace)
```

### 6.2 Meta page (`grafx.meta`, page 0)

`magic "OKTOGRFX" (8B) | format_version u16 | page_size u32 | database_uuid 16B |
 created_at_wall f64 | partitions_per_table u16 | granularity_descriptor_len u16 |
 granularity_descriptor UTF-8 | reserved | crc32c u32`

### 6.3 Page header (32 bytes, every paged file)

| off | type | field |
|---|---|---|
| 0 | u32 | `checksum` — CRC-32C over bytes[4:page_size] |
| 4 | u16 | `page_type` 0 free · 1 meta · 2 heap · 3 catalog · 4 index_hash · 5 index_hnsw · 6 overflow |
| 6 | u16 | `flags` |
| 8 | u64 | `page_lsn` — LSN of the last WAL record applied to this page (redo idempotence) |
| 16 | u32 | `seq` — even = stable, odd = being written (torn-read detection helper) |
| 20 | u16 | `slot_count` |
| 22 | u16 | `free_start` |
| 24 | u16 | `free_end` |
| 26 | u16 | `reserved` |
| 28 | u32 | `next_page` (`NO_PAGE` = none) |

Slotted layout: payloads grow up from `free_start`; the slot directory grows **down** from
`page_size`, each slot `(u16 offset, u16 length)`.

**Torn-read protocol (readers never block writers):** `read_page` → if `seq` is odd or the checksum
fails, re-read (bounded retries, default 8, with no sleep in domain). After the budget is exhausted
raise `GrafxCorruptionDetected` with the page location.

### 6.4 Heap record (inside a slot) — 40-byte header

| off | type | field |
|---|---|---|
| 0 | u8 | `flags` bit0 deleted · bit1 has_overflow |
| 1 | u8 | `reserved` |
| 2 | u16 | `schema_version` |
| 4 | u32 | `payload_len` |
| 8 | u64 | `record_id` |
| 16 | u64 | `xmin_csn` |
| 24 | u64 | `xmax_csn` (0 = live) |
| 32 | u64 | `prev_version` (`RecordRef.encode()`, 0 = none) |

Payload = positional tuple encoded per the table schema (see §7.2).

### 6.5 WAL record (48-byte header, self-describing — TR-4/SD-1)

| off | type | field |
|---|---|---|
| 0 | u32 | `magic` = `0x5852474F` |
| 4 | u16 | `format_version` = 1 |
| 6 | u16 | `record_type` |
| 8 | u16 | `header_len` = 48 |
| 10 | u16 | `flags` |
| 12 | u32 | `total_length` (header + descriptor + payload + 4) |
| 16 | u64 | `lsn` |
| 24 | u64 | `epoch` |
| 32 | u64 | `txn_id` |
| 40 | u32 | `descriptor_len` |
| 44 | u32 | `payload_len` |

then `descriptor` (UTF-8, e.g. `"hash-v1;partitions_per_table=64"`), then `payload`, then
`crc32c u32` over `bytes[0:total_length-4]`.

Record types: `1 BEGIN · 2 WRITE_PAGE · 3 COMMIT · 4 ABORT · 5 CHECKPOINT · 6 INDEX_WRITE ·
7 INDEX_RECONCILE · 8 LEDGER_APPEND · 9 CATALOG_WRITE · 10 SPACE_DDL · 11 VECTOR_WRITE ·
12 SEGMENT_HEADER · 13 PAGE_ALLOC`.

**Decoder rule:** the decoder must accept every `format_version <= CURRENT`. Round-trip tests per
version are mandatory (TR-4). Changing `partitions_per_table` changes only the descriptor string,
never the format — no migration.

`COMMIT` payload (canonical, versioned): `snapshot_lsn u64 | read_partition_count u32 |
write_partition_count u32 | read_partitions[u64...] | write_partitions[u64...] | page_touch_count u32 |
(file_id u16, page_index u32)[...]`. A partition key is `u64 = (table_id << 32) | partition_index`.

### 6.6 Ledger entry (`ledger/ledger.log`, append-only, own checksums — TR-5)

`magic u32 0x4C475258 | format_version u16 | entry_type u16 | total_length u32 | entry_id u64 |
 origin_class u8 (1 reapplicable, 2 forensic) | reason_code u8 | reserved u16 |
 lsn_start u64 | lsn_end u64 | epoch u64 | captured_at_wall f64 |
 digest 32B (sha256 of payload) | payload_len u32 | payload | crc32c u32`

Reason codes: `1 truncated_tail · 2 checksum_failure · 3 stale_epoch · 4 device_full ·
5 quarantined_segment · 6 index_reconcile_orphan`.

---

## 7. Domain model (C1)

### 7.1 Values

```python
class ValueType(IntEnum):
    NULL=0; BOOL=1; INT64=2; DOUBLE=3; STRING=4; BYTES=5; LIST=6; MAP=7
    VECTOR_F32=8; VECTOR_F64=9; TIMESTAMP=10; UUID=11
```
Encoding: `tag u8` then type-specific body; STRING/BYTES/LIST/MAP prefix `u32 length`.
`VECTOR_F32` body = `u32 dimension | u32 space_ref | f32[dimension]`; `VECTOR_F64` likewise with f64.
`space_ref` is the catalog-assigned numeric id of the embedding space (0 = unassigned is invalid).

### 7.2 Schema / catalog

```python
@dataclass(frozen=True, slots=True)
class ColumnDef:
    name: str; type: ValueType; nullable: bool = True
    vector_space: str | None = None      # embedding space name for VECTOR_* columns

@dataclass(frozen=True, slots=True)
class TableDef:
    table_id: int; name: str; kind: str          # "node" | "rel"
    columns: tuple[ColumnDef, ...]
    primary_key: str | None                       # node tables
    from_table: str | None; to_table: str | None  # rel tables
    schema_version: int = 1

@dataclass(frozen=True, slots=True)
class EmbeddingSpaceDef:
    space_id: int; name: str
    dimension: int
    metric: DistanceMetric
    normalized: bool
    storage_dtype: str          # "float32" (default) | "float64"
    state: str                  # "active" | "retired"
    created_at_wall: float
    # IMMUTABLE after creation except `state` (active -> retired only).
```

`Catalog` exposes `tables()`, `table(name)`, `spaces()`, `space(name)`, `next_table_id()`,
`next_space_id()` and is itself persisted through `catalog.dat` as normal WAL-covered pages.

---

## 8. Engine interfaces (FROZEN — cross-component contracts)

Every engine class takes its ports by constructor injection; none of them import adapters.

### 8.1 `engine/buffer_pool.py` (C1)
```python
class BufferPool:
    def __init__(self, storage: StorageDevice, codec: PageCodec, metrics: MetricsSink,
                 *, budget_bytes: int, db_label: str) -> None
    def pin(self, file: str, page_index: PageIndex) -> Page       # GrafxBufferBudgetExceeded
    def unpin(self, file: str, page_index: PageIndex, *, dirty: bool = False) -> None
    def pinned(self, file: str, page_index: PageIndex) -> AbstractContextManager[Page]
    def allocate(self, file: str, page_type: int) -> Page
    def flush(self, file: str | None = None) -> int               # returns pages written
    def invalidate(self, file: str | None = None) -> None         # drop cache; force re-read
    def used_bytes(self) -> int
```
Budget is **per Database instance**. No module-level/global state anywhere (BR-8/FR-13).

### 8.2 `engine/heap_store.py` (C1)
```python
class HeapStore:
    def __init__(self, pool: BufferPool, catalog: "CatalogStore", *, file: str = "heap.dat")
    def insert(self, table: TableDef, record_id: RecordId, values: tuple[Value, ...],
               xmin: Csn) -> RecordRef
    def update(self, table, ref: RecordRef, values, xmin: Csn) -> RecordRef   # new version, chains prev
    def delete(self, table, ref: RecordRef, xmax: Csn) -> None
    def read(self, ref: RecordRef) -> HeapVersion
    def scan(self, table: TableDef, snapshot: "Snapshot") -> Iterator[tuple[RecordRef, HeapVersion]]
    def lookup(self, table: TableDef, record_id: RecordId, snapshot: "Snapshot") -> HeapVersion | None
```
`HeapVersion` carries `record_id, xmin, xmax, values, prev`. **Visibility is applied by the caller
through `Snapshot.visible(version)` — the store never hides rows on its own.**

### 8.3 `engine/wal_manager.py` (C4)
```python
class WalManager:
    def __init__(self, storage, clock, metrics, *, directory: str = "wal",
                 segment_bytes: int, descriptor: str)
    @property
    def last_lsn(self) -> Lsn
    def open(self) -> None                       # discovers segments, sets last_lsn
    def append(self, record: WalRecord) -> Lsn   # assigns LSN; NO barrier
    def append_many(self, records: Sequence[WalRecord]) -> Lsn
    def barrier(self) -> None                    # durable_barrier + metrics
    def read_from(self, lsn: Lsn) -> Iterator[WalRecord]
    def scan_all(self) -> Iterator["ScanItem"]   # ScanItem = (segment, offset, record|None, failure|None)
    def segments(self) -> tuple["SegmentInfo", ...]
    def truncate_after(self, lsn: Lsn) -> "TruncationReport"
    def recycle(self, horizon_lsn: Lsn) -> "RecycleReport"
    def total_bytes(self) -> int
```
`recycle` is allowed to drop a segment **iff** `segment.last_lsn < horizon_lsn`, where the horizon is
`min(reader_horizon or checkpoint_lsn, checkpoint_lsn)`. Never based on reader absence (BR-10).
Windows deferral is invisible here: `StorageDevice.recycle` returns `False` and the segment is
retried on the next pass; `RecycleReport.deferred` counts them.

### 8.4 `engine/coordination.py` (C3)
Thin engine-side helper over the port: lease renewal scheduling driven by the caller (no background
threads in the engine — the API layer may drive renewal), epoch validation helpers, reader
registration lifecycle.

### 8.5 `engine/txn_manager.py` (C5)
```python
@dataclass(frozen=True, slots=True)
class Snapshot:
    read_lsn: Lsn
    def visible(self, xmin: Csn, xmax: Csn) -> bool:
        return xmin != 0 and xmin <= self.read_lsn and (xmax == 0 or xmax > self.read_lsn)

class TransactionManager:
    def __init__(self, wal, pool, heap, catalog, coordinator, clock, metrics, index_manager,
                 *, partitions_per_table: int, commit_lock_timeout: float)
    def begin(self, mode: str) -> "TransactionContext"      # "read" | "write"
    def commit(self, txn: "TransactionContext") -> "CommitReport"
    def rollback(self, txn: "TransactionContext") -> None
    def published_lsn(self) -> Lsn
    def partition_of(self, table_id: int, key: bytes) -> int
```
`TransactionContext` accumulates `read_partitions: set[int]`, `write_partitions: set[int]`,
`pending_records: list[WalRecord]`, `page_images: dict[(file, page), bytes]`.

**Commit protocol (FROZEN — implement exactly):**
1. read-only txn → unregister reader, return `CommitReport(csn=snapshot.read_lsn, durable=True, wrote=False)`.
2. `coordinator.validate_epoch(lease.epoch)` — **before any device call** (BR-7/AC-6).
3. `with coordinator.exclusive("commit", timeout=commit_lock_timeout):`
   1. re-`validate_epoch`.
   2. `current = published_lsn()`.
   3. **OCC validation**: for every `COMMIT` record with `lsn > txn.snapshot.read_lsn`, conflict iff
      `record.write_partitions ∩ (txn.read_partitions | txn.write_partitions) != ∅`
      → raise `GrafxWriteConflict` (retryable), metrics `oktografx_write_conflicts_total`.
      Nothing has been written to the device at this point.
   4. `lsn = wal.append_many([...WRITE_PAGE..., COMMIT])`.
   5. `wal.barrier()` — **BR-4: no acknowledgement before this returns**.
   6. apply page images: for each, `if page.page_lsn < lsn: write with page_lsn = lsn`.
      (Data files are NOT fsynced here; the WAL is the authority, redo is idempotent.)
   7. publish `control/commit.state` via `atomic_replace`.
4. return `CommitReport(csn=lsn, durable=True, wrote=True)`.

Disjoint partition sets never conflict → both commit (BR-6/AC-1).

### 8.6 `engine/recovery_manager.py` + `ledger_store.py` + `quarantine.py` + `verifier.py` (C6)
```python
@dataclass(frozen=True, slots=True)
class RecoveryReport:
    outcome: str                 # "clean" | "truncated" | "quarantined" | "refused"
    records_replayed: int
    records_discarded: int
    ledger_entries_created: int
    last_good_lsn: Lsn
    findings: tuple["RecoveryFinding", ...]

class RecoveryManager:
    def run(self) -> RecoveryReport
```
Algorithm (FROZEN):
1. read meta; mismatch → `GrafxSchemaVersionMismatch`.
2. scan WAL from `checkpoint_lsn`; stop at the first record that fails magic/length/CRC/LSN-contiguity.
3. If a tail was discarded: copy the affected byte range into `quarantine/` **with a manifest**,
   then `truncate_log`. `heap.dat`/`catalog.dat`/`index/*` are never touched (G6/BR-1).
4. Ledger classification per discarded record (SD-4):
   * CRC valid but not usable (stale epoch, post-truncation) → **REAPPLICABLE** (decoded operation).
   * CRC invalid / undecodable → **FORENSIC** (raw bytes + offset + expected LSN + reason + sha256).
   Every discarded record produces exactly one entry (G8/BR-3).
5. Redo committed transactions in LSN order, idempotent via `page_lsn`.
6. Uncommitted transactions require **no undo** — pages are only written at commit.
7. `recovery_policy="refuse"` raises `GrafxRecoveryRefused` **instead of step 3** and leaves
   everything on disk untouched.

```python
class LedgerStore:
    def append(self, entry: LedgerEntry) -> int
    def list(self, *, origin_class=None, reason=None, limit=100, offset=0) -> tuple[LedgerEntry, ...]
    def inspect(self, entry_id: int) -> LedgerEntry
    def reprocess(self, entry_id: int, applier) -> "ReprocessReport"   # reapplicable only, idempotent
    def export(self, entry_id: int) -> bytes                            # forensic bytes + digest
    def purge(self, *, confirm_token: str, entry_ids=None) -> int       # explicit operator act only
    def depth(self) -> Mapping[str, int]
```
`reprocess` on a forensic entry raises `GrafxLedgerError`. Idempotence is keyed on the origin LSN,
recorded durably: repeating is a no-op (AC-12).

```python
class Verifier:
    def verify(self, scope: str = "all") -> "VerificationReport"   # "pages"|"records"|"indexes"|"all"
```
Findings carry `kind`, `location` (`file`, `page`, `slot`, `lsn`, `index`), `detail` (en-US).
A clean database returns an empty findings tuple.

### 8.7 `engine/index_manager.py` (C7)
```python
class IndexVisibility(str, Enum):
    EXACT = "exact"; PROXIMITY = "proximity"

@runtime_checkable
class SecondaryIndex(Protocol):
    @property
    def name(self) -> str
    @property
    def visibility(self) -> IndexVisibility
    def apply(self, record: WalRecord) -> None                  # redo path, idempotent by page_lsn
    def stage_insert(self, txn, key: bytes, ref: RecordRef, csn: Csn) -> WalRecord
    def stage_delete(self, txn, key: bytes, ref: RecordRef, csn: Csn) -> WalRecord
    def lookup(self, key: bytes, snapshot: Snapshot) -> Iterable[RecordRef]
    def reconcile(self, horizon: Lsn) -> "ReconcileReport"       # PROXIMITY only; WAL-covered
    def walk(self) -> Iterable["IndexEntry"]                     # for verify()
```
**Dual visibility rule (SD-3):**
* `EXACT` — entries are unversioned; `lookup` returns a **superset** and the caller MUST validate each
  hit against the heap under its snapshot.
* `PROXIMITY` — entries are versioned with tombstones; `reconcile(horizon)` removes only entries whose
  tombstone CSN is below the snapshot horizon, and every removal is an `INDEX_RECONCILE` WAL record.

M1 ships `HashIndex` (EXACT) as the reference implementation proving the contract end to end.

### 8.8 `engine/vector_engine.py` (C9)
```python
@dataclass(frozen=True, slots=True)
class VectorHit:
    record_id: RecordId; score: float; ref: RecordRef; retired: bool

@dataclass(frozen=True, slots=True)
class VectorSearchResult:
    hits: tuple[VectorHit, ...]
    regime: str            # "exact" | "approximate"      <- NEVER omitted (BR-2)
    achieved_k: int        # actual number returned       <- NEVER omitted (BR-2)
    requested_k: int
    space: str
    filter_cardinality: int | None

class VectorEngine:
    def create_space(self, definition: EmbeddingSpaceDef) -> None
    def retire_space(self, name: str) -> None
    def validate_vector(self, space: EmbeddingSpaceDef, values: Sequence[float]) -> tuple[float, ...]
        # dimension mismatch / NaN / inf -> GrafxVectorValidationError, nothing persisted (BR-5)
    def search(self, *, space: str, query: Sequence[float], k: int, snapshot: Snapshot,
               candidate_filter: "CandidateFilter | None" = None) -> VectorSearchResult
```
**Two-regime planner (dec_fc3351c3, FROZEN):**
* `filter_cardinality <= config.vector_exact_scan_threshold` → **exact scan** over the filtered set,
  `regime="exact"`, recall 1.0 by construction, `oktografx_vector_exact_fallback_total` incremented.
* otherwise → **filter-aware HNSW traversal** (ACORN-style): the predicate is evaluated *during*
  neighbour expansion; a node failing the predicate is **not returned but remains traversable as a
  bridge**; `regime="approximate"`.
* A query whose reference vector belongs to another space raises `GrafxEmbeddingSpaceMismatch`
  **before any distance computation** (BR-1).
* Writes to a retired space raise `GrafxSpaceRetired`; reads succeed with `retired=True` (FR-3).

### 8.9 `engine/query_engine.py` (C10)
```python
class QueryEngine:
    def parse(self, text: str) -> "Statement"
    def plan(self, statement: "Statement", snapshot: Snapshot) -> "PlanNode"
    def execute(self, text: str, txn, parameters: Mapping[str, object] | None = None) -> "QueryResult"
    def explain(self, text: str) -> "PlanNode"     # single operator tree, inspectable (AC-7)
```
Cypher subset (openCypher, Kùzu dialect): `CREATE NODE TABLE` / `CREATE REL TABLE` /
`CREATE VECTOR SPACE`, `CREATE`, `MATCH` (+ variable-length `-[:R*1..3]->`), `WHERE`, `RETURN`
(`DISTINCT`, aliases), `ORDER BY`, `SKIP`, `LIMIT`, `SET`, `DELETE`, `MERGE`, parameters `$name`,
aggregates `count/sum/avg/min/max/collect`, and the similarity extension:
```
MATCH (n:Chunk)-[:BELONGS_TO]->(d:Doc)
WHERE n.layer = $layer AND d.active = true
  AND similarity(n.embedding, $q, space => 'minilm-v2') > 0.7
RETURN n.id, similarity_score() AS score
ORDER BY score DESC LIMIT 10
```
`explain()` must return **one** operator tree; a plan containing an over-fetch node followed by a
post-filter node is a contract violation (BR-6/AC-7).

---

## 9. Metric catalog (C8 owns registration; every component emits)

M1: `oktografx_lease_wait_seconds`{outcome=granted|timeout|takeover} ·
`oktografx_write_conflicts_total` · `oktografx_commit_retries_total` ·
`oktografx_active_transactions`{mode=read|write} · `oktografx_fsync_duration_seconds`{target=wal|data} ·
`oktografx_barrier_failures_total` · `oktografx_wal_size_bytes` · `oktografx_wal_segments` ·
`oktografx_wal_truncation_lag_segments`{reader_present=true|false} ·
`oktografx_checksum_verifications_total`{kind=page|record} ·
`oktografx_checksum_failures_total`{kind=page|record} · `oktografx_recovery_replays_total` ·
`oktografx_recovery_discarded_records_total`{origin_class=reapplicable|forensic} ·
`oktografx_ledger_depth`{origin_class} · `oktografx_ledger_oldest_entry_age_seconds`{origin_class} ·
`oktografx_quarantine_entries` · `oktografx_buffer_budget_used_bytes`{db} ·
`oktografx_buffer_budget_exceeded_total`{db} · `oktografx_database_opens_total` ·
`oktografx_recoveries_total`{outcome} ·
`oktografx_baseline_ceiling_multiple`{ceiling=durable_commit|point_read|open_replay|vector_recall}

VEC: `oktografx_vector_recall_ratio` · `oktografx_vector_query_latency_seconds`{regime,phase} ·
`oktografx_vector_exact_fallback_total` · `oktografx_vector_achieved_k` ·
`oktografx_vector_filter_selectivity` · `oktografx_vector_tombstone_backlog` ·
`oktografx_vector_reconciliation_total` · `oktografx_vector_index_entries`{space} ·
`oktografx_vector_space_retired_total` · `oktografx_vector_space_coverage_ratio`{space} ·
`oktografx_vector_index_age_seconds`{space}

Query: `oktografx_query_phase_duration_seconds`{phase=parse|plan|execute} ·
`oktografx_query_rows_returned` · `oktografx_query_errors_total`{code}

`db` and `space` labels carry a **short hash / catalog name**, never a path or free text (TR-7).
Every metric here MUST appear in a `dashboards/*.json` panel (OR-5/OR-3) — the CI test asserts it.

---

## 10. Public API (C11) — `okto_grafx/__init__.py`

```python
from okto_grafx import connect, Database, Transaction, QueryResult, __version__
from okto_grafx.errors import GrafxError, GrafxWriteConflict, ...

db = connect("./mydb", partitions_per_table=64)          # or ":memory:"
with db.begin("write") as txn:
    txn.execute("CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))")
    txn.execute("CREATE (:Person {id: 1, name: 'Ada'})")
result = db.execute("MATCH (p:Person) RETURN p.name")     # autocommit read
report  = db.verify(scope="all")
entries = db.ledger.list(origin_class="forensic")
db.close()
```
`Database` is a context manager. `close()` releases the lease and the reader registration; closing
with an open transaction aborts it and never corrupts. Every public method has an en-US docstring.

---

## 11. Definition of Done (applies to every component)

1. **Implements the contract exactly** — no signature drift, no invented public names.
2. **Tests**: unit tests for every public behaviour + the spec scenarios the component owns.
   `pytest -q` green. Deterministic (seeded), no `sleep` longer than 50 ms, no network.
3. **Type annotations on every public symbol**; `from __future__ import annotations` at the top.
4. **No mechanism in `domain/` or `engine/`** — the import-boundary test must stay at zero.
5. **Errors**: only `Grafx*` types escape; never a bare `Exception`, never `assert` for control flow.
6. **Metrics**: every operational behaviour the component owns emits through `MetricsSink`,
   guarded by `if metrics.enabled:` on hot paths.
7. **en-US** for every identifier, docstring and message.
8. **No TODO / FIXME / `pass  # stub` / `NotImplementedError`** left in delivered code
   (except explicit `Protocol` bodies, which use `...`).
9. **Windows + POSIX**: no path separator assumptions, no `os.name` outside adapters,
   file names case-insensitive-safe.
10. **Zero regressions**: the whole suite must stay green, not just the component's own tests.
