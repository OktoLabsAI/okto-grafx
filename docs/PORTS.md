# Okto Grafx — Ports and adapters

Everything Okto Grafx needs from the outside world arrives through one of **seven ports**. A port is
a `typing.Protocol` declared in `src/okto_grafx/domain/ports/`; an adapter is any object that
satisfies it. `domain/**` and `engine/**` import no mechanism at all — no `os`, no `time`, no
`socket`, no threading primitive — and `tests/test_import_boundary.py` fails the build if that stops
being true.

This document lists each port, what it abstracts, the adapters that ship with the package, and how
to substitute your own.

- [The registry](#the-registry)
- [The seven ports](#the-seven-ports)
  - [storage](#storage--storagedevice) · [clock](#clock--clock) ·
    [coordinator](#coordinator--processcoordinator) · [codec](#codec--pagecodec) ·
    [metrics](#metrics--metricssink) · [events](#events--eventsink) ·
    [vector_math](#vector_math--vectormath)
- [The checksum slot](#the-checksum-slot-not-a-registry-port)
- [Writing an adapter](#writing-an-adapter)
- [Composing a custom registry](#composing-a-custom-registry)

---

## The registry

`PortRegistry` holds one adapter per slot and is **fail-closed by design**:

> An empty slot is not filled with a silent default and never degrades into a no-op. Opening a
> database with an incomplete registry raises `GrafxPortNotConfigured` naming **every** slot that is
> missing, in one error, so an operator fixes the composition once instead of once per attempt.

The check is **structural**, not nominal. The registry compares the attributes an object exposes
against the protocol's members, so you do not inherit from anything and you do not register
anywhere — an object that has the members *is* the adapter.

`build_default_registry(config)` produces the registry the defaults describe; `registry.replace(...)`
returns a new one with a slot swapped.

| Slot | Protocol | Default adapter |
|---|---|---|
| `storage` | `StorageDevice` | `LocalStorageDevice`, or `MemoryStorageDevice` for `":memory:"` |
| `clock` | `Clock` | `SystemClock` |
| `coordinator` | `ProcessCoordinator` | `LocalProcessCoordinator` |
| `codec` | `PageCodec` | `PageCodecV1` |
| `metrics` | `MetricsSink` | `NoOpMetricsSink` (selector `metrics=`) |
| `events` | `EventSink` | `LoggingEventSink` |
| `vector_math` | `VectorMath` | `PureVectorMath` (selector `vector_math=`) |

---

## The seven ports

### `storage` — `StorageDevice`

Everything that is a file. Paged random access for the data files, append-only access for the log,
plus the two operations that carry the durability and atomicity guarantees.

```
name              page_size         exists(file)        create(file)       remove(file)
page_count(file)  file_size(file)   list_files()        allocate(file, n)
read_page(file, index)              write_page(file, index, image)
truncate_log(...)                   append_log(...)     read_log(...)      log_size(...)
recycle(...)                        atomic_replace(source, destination)
durable_barrier(file | None)
```

Two members deserve their own note, because the engine's guarantees rest on them.

**`durable_barrier`** must not return until what was written is on the platter. A failure must be
raised as `GrafxDurabilityBarrierFailed` — never swallowed — because a failed barrier means nothing
may be acknowledged as durable.

**`atomic_replace`** must make the destination *either* the old content *or* the new one, never a
mixture, for a reader in another process. This is how the control files (lease, commit state) are
published, and it is the one operation whose implementation genuinely differs between families.

| Adapter | Module | Use |
|---|---|---|
| **`LocalStorageDevice`** *(default)* | `adapters/storage_local.py` | A directory on the real filesystem. Creates it when it does not exist. Handles the Windows and POSIX differences in open modes, locking, publication and unlink-while-open. |
| `MemoryStorageDevice` | `adapters/storage_memory.py` | Selected by `connect(":memory:")`. **The same transactional semantics** as the local device — it is not a stub. Nothing survives the process. |
| `FaultInjectingStorageDevice` | `adapters/storage_fault.py` | Wraps another device and refuses the *n*-th operation, reports a full device, or fails a barrier. This is how the suite proves a hostile device produces a typed refusal and never a half-written page. |

**Cross-process note.** A long-lived participant caches file descriptors. `LocalStorageDevice`
verifies that the directory entry for a name is still the file the descriptor holds — identity is
`(st_dev, st_ino)` on both families — because a cached descriptor reading a file another process has
since replaced answers from a file nobody can see any more.

---

### `clock` — `Clock`

```
monotonic()     # seconds from an arbitrary origin, never goes backwards
wall()          # seconds since the epoch, for human-facing stamps ONLY
```

**The split is load-bearing.** Every liveness decision — lease expiry, dead-owner detection, reader
stall — is taken on `monotonic()`. A wall-clock adjustment (NTP, a manual change, a VM resuming from
a snapshot) must never make a live writer look dead or a dead one look live. `wall()` exists to stamp
things a human reads, and nothing decides anything on it.

| Adapter | Module | Use |
|---|---|---|
| **`SystemClock`** *(default)* | `adapters/clock_system.py` | `time.monotonic` and `time.time` |
| `ManualClock` | `tests/` | Advances exactly as much as a test says, which is what makes a lease-expiry race deterministic rather than slept on |

---

### `coordinator` — `ProcessCoordinator`

The multi-process substrate: who may write, under what epoch, and which readers are holding the log
down.

```
owner_id                                current_epoch()
acquire_writer_lease(...)  renew_lease(...)  release_lease(...)
validate_epoch(...)        detect_dead_owner()  takeover(...)
exclusive(name, timeout)                # an exclusive section, across processes
register_reader(...)  refresh_reader(...)  unregister_reader(...)  reader_horizon()
```

**The epoch is the whole safety argument.** A lease is granted with an epoch; every byte a writer
sends to the device is authorised by an epoch validated *inside* the commit section. A writer that
was paused past its TTL — swapped out, SIGSTOPped, or on a machine that went to sleep — wakes to find
its epoch stale and is refused with `GrafxStaleEpoch`. It cannot write over the work of the writer
that took over.

**`reader_horizon()`** is what keeps the log from recycling a segment a live reader still needs.

| Adapter | Module | Use |
|---|---|---|
| **`LocalProcessCoordinator`** *(default)* | `adapters/coordination_local.py` | Lock files under `<db>/control`. `flock` on POSIX, `LockFileEx` on Windows; both families tested, and the branch each one cannot exercise is declared. |
| same class, `lock_directory=None` | | Selected for `":memory:"` — process-wide sections instead of files, which is what that mode requires |

---

### `codec` — `PageCodec`

```
format_version                          checksum(data)
encode_page(page) -> bytes              decode_page(raw, verify=True) -> Page
```

The codec owns the on-disk representation of one page, checksum included. `decode_page(verify=True)`
must refuse damaged bytes with `GrafxCorruptionDetected` naming the location — a codec that returned
a best-effort page would turn detected damage into a wrong answer.

| Adapter | Module | Use |
|---|---|---|
| **`PageCodecV1`** *(default)* | `adapters/codec_v1.py` | Format version 1: a CRC-32C in the first four bytes, an even sequence counter in a durable image, the page type, and the slot directory |

---

### `metrics` — `MetricsSink`

```
enabled                                 register(descriptor)
increment(name, value, labels)          set_gauge(name, value, labels)
observe(name, value, labels)            time(name, labels)      # a context manager
snapshot()
```

**`enabled` is a property the engine checks before it formats anything.** A disabled sink must cost
nothing on a hot path — not "be fast", but allocate and format nothing at all.

Every metric name comes from a **frozen catalogue** in `domain/ports/metrics.py`. A metric is a
contract, so there is exactly one declaration of each name in the engine, and a name that leaves the
catalogue breaks the import of the module that uses it rather than a scrape in production.

| Adapter | Module | Use |
|---|---|---|
| **`NoOpMetricsSink`** *(default, `metrics="noop"`)* | `adapters/metrics_noop.py` | `enabled` is `False`; nothing is allocated or formatted |
| `OpenMetricsSink` (`metrics="openmetrics"`) | `adapters/metrics_openmetrics.py` | Aggregates into the OpenMetrics text format, exposed at `GET /metrics` on a loopback port. The default port is **zero** — the OS picks a free one, so a database never fails to open because a fixed port was taken, and `Database.metrics_endpoint` reports the address actually bound. |
| `JsonMetricsSink` (`metrics="json"`) | `adapters/metrics_json.py` | Writes documents to the file `metrics_destination` names, through a rotating writer |

---

### `events` — `EventSink`

```
emit(event)
```

Structured, **sanitised** and **bounded** records: an event carries a code, a severity and located
details, never a full page image, a row value, or a path outside the database directory. The bound
matters as much as the sanitisation — an event sink that could be handed an unbounded payload is a
way to fill a disk from a query.

| Adapter | Module | Use |
|---|---|---|
| **`LoggingEventSink`** *(default)* | `adapters/events_logging.py` | Standard-library `logging`, so a host that already configures logging gets these for free |

---

### `vector_math` — `VectorMath`

```
name                                    normalize(v)      norm(v)
cosine(a, b)     dot(a, b)     euclidean(a, b)
score(metric, a, b)                     top_k(query, candidates, k, metric)
```

| Adapter | Module | Use |
|---|---|---|
| **`PureVectorMath`** *(default; `vector_math="auto"` and `"pure"`)* | `adapters/vectormath_pure.py` | Pure Python. The **oracle**: the answer that is the same on every machine. |
| `NumpyVectorMath` (`vector_math="numpy"`) | `adapters/vectormath_numpy.py` | numpy. Requires `pip install "okto-grafx[accel]"`, and **refuses** when the extra is absent rather than falling back. |

**Why `auto` does not accelerate here, when `checksum="auto"` does.** The two adapters agree to a
*stated tolerance*, not exactly — floating-point summation order differs. A selector that silently
bound whichever adapter happened to be installed would make the **ranking of a query** depend on the
machine it ran on. So `auto` means "let the composition root choose", and the composition root
chooses the answer that is the same everywhere. Asking for `"numpy"` is a decision the caller makes
with that in mind.

---

## The checksum slot (not a registry port)

CRC-32C is installed **process-wide** rather than injected per object, because every component of one
database must compute the same checksum. It is therefore not a registry slot, and it is worth knowing
about because it behaves differently from the seven above.

| Selector | Behaviour |
|---|---|
| `checksum="pure"` | The pure-Python reference, always |
| `checksum="auto"` *(default)* | The native implementation when `google-crc32c` is installed; the reference otherwise |

**`auto` accelerates here and does not for `vector_math`, and the difference is not inconsistency.**
`install_crc32c` replays an acceptance corpus against the reference and **refuses** a candidate that
disagrees on any input *before* it becomes the implementation. A provider either produces
byte-identical digests or never gets installed — so there is no machine-dependent answer to protect
against, and accelerating is free.

**One consequence a caller should know:** two databases in one process do not get independent
checksum implementations. `connect(a, checksum="pure")` followed by `connect(b)` leaves both on
whatever the second call installed. No digest changes — they are byte-identical by construction —
but `crc32c_implementation()` reports the installed name, not the requested one. This is recorded in
`docs/architecture/PUNCHLIST.md`.

---

## Writing an adapter

Satisfy the protocol. That is all there is: no base class, no registration, no decorator.

```python
class CountingMetrics:
    """A metrics sink that counts calls. Structural typing: no import from Okto Grafx needed."""

    def __init__(self) -> None:
        self.counts: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return True

    def register(self, descriptor) -> None:
        self.counts.setdefault(descriptor.name, 0.0)

    def increment(self, name, value=1.0, labels=None) -> None:
        self.counts[name] = self.counts.get(name, 0.0) + value

    def set_gauge(self, name, value, labels=None) -> None:
        self.counts[name] = value

    def observe(self, name, value, labels=None) -> None:
        self.counts[name] = self.counts.get(name, 0.0) + value

    def time(self, name, labels=None):
        from contextlib import nullcontext
        return nullcontext()

    def snapshot(self):
        return dict(self.counts)
```

### Rules an adapter must honour

1. **Raise the taxonomy, not your own exceptions.** Everything that escapes must be a `GrafxError`
   subclass with the right `code` and `retryable` flag. A `PermissionError` reaching the engine is a
   defect in the adapter, not a case the engine handles.
2. **Be honest about `retryable`.** A caller retries on it. Marking a permanent failure retryable
   turns a clear error into a livelock.
3. **Never partially apply.** `write_page` either writes the whole image or fails. The engine's
   recovery assumes a page is whole or absent, never a mixture of two images.
4. **`durable_barrier` must not lie.** Returning before the data is on the platter converts a crash
   into data loss that the log cannot repair, because the log's own barrier is the thing you would
   be lying about.
5. **Do not hold a lock while calling back into the host.** Amendment A91: a metrics sink, an event
   sink or a device is supplied by the caller and may do anything at all, including re-entering the
   API.
6. **Be deterministic where the engine depends on order.** `list_files` must return a stable order;
   an index walk must answer identically across two runs over one file.

---

## Composing a custom registry

```python
from okto_grafx import DatabaseConfig, connect
from okto_grafx.runtime.bootstrap import build_default_registry

config = DatabaseConfig(path="./mydb", page_size=8192)
registry = build_default_registry(config).replace(metrics=CountingMetrics())

db = connect("./mydb", registry=registry)
```

Building one from nothing is also supported, and the fail-closed rule is what makes it safe to try:

```python
from okto_grafx import PortRegistry
from okto_grafx.domain.errors import GrafxPortNotConfigured

try:
    PortRegistry(storage=my_device, clock=my_clock).require()
except GrafxPortNotConfigured as refused:
    print(refused.details["slots"])
    # ('codec', 'coordinator', 'events', 'metrics', 'vector_math')
```

Every missing slot is named in **one** error, so the composition is fixed once rather than one
`connect()` at a time.

### Inspecting what is bound

A `Database` exposes every adapter it was composed with, which is how a test asserts the composition
rather than assuming it:

```python
db = connect("./mydb")
db.storage        # the StorageDevice in effect
db.clock          # the Clock
db.codec          # the PageCodec
db.metrics        # the MetricsSink
db.events         # the EventSink
db.vector_math    # the VectorMath
db.coordinator    # the ProcessCoordinator
```
