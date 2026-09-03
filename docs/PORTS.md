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
anywhere — an object that has the members *is* the adapter. Binding checks only that every required
member is present and that methods are callable. It does not execute the adapter or validate its
signatures, return values or runtime behaviour.

`build_default_registry(config)` produces the registry the defaults describe. A later
`registry.bind(slot, instance)` mutates that registry by replacing the adapter in the named slot.

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

Everything that is a file. Paged random access for data files, bounded byte-range reads for any
named file, append-only writes for logs, plus the two operations that carry the durability and
atomicity guarantees.

```
name                                  page_size
exists(file)                          create(file, *, exclusive=True)
remove(file)                          list_files(prefix="")
file_size(file)                       atomic_replace(source, target)
recycle(file)                         page_count(file)
allocate(file, count=1)               read_page(file, page_index)
write_page(file, page_index, data)    append_log(file, payload)
read_log(file, offset, length)        log_size(file)
truncate_log(file, size)
durable_barrier(file=None)
```

Two members deserve their own note, because the engine's guarantees rest on them.

**`read_log`** is historically named for its main consumer, but it is the port's non-mutating,
bounded byte-range read over the unified file namespace. It fills the requested range unless it
reaches the actual EOF, in which case it returns the remaining bytes. It may therefore read an
allocated paged file without changing it; CE-1 legacy control migration already depended on this
property, and ST-2 uses it to fetch one exact three-page control image plus a one-byte length
sentinel. Only the write-side `append_log`/`truncate_log` operations impose append-only log
semantics. A custom storage adapter must preserve this fill-until-EOF behaviour even when it
internally distinguishes file kinds.

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

**Cross-process note.** A long-lived participant caches file descriptors. By default,
`descriptor_revalidation="strict"`, `LocalStorageDevice` verifies on every cached hit that the
directory entry for a name is still the physical file the descriptor holds. This prevents a
descriptor from silently continuing on a file another process has replaced or removed.

`descriptor_revalidation="generation"` is an explicit performance opt-in. It keeps control,
`grafx.meta`, malformed and unknown names strict and amortizes proofs only for a closed whitelist:
`heap.dat`, `catalog.dat`, canonical `index/<identifier>.idx` names and canonical twelve-digit
`wal/<segment>.wal` names. Its adapter-global generation advances on `invalidate(None)`, a full
foreign refresh and a CE-3 baseline-mismatch `every_file` refresh. `invalidate(file)` removes only
that file's proof; same-token, proved-own and valid bounded CE-3 partial views do not advance it.
The bounded paths may still remove only the exact changed/unfenced names' stamps, and
`read_fresh_page` plus a fenced page-0 CAS reprove their one name before the device read; those are
directed checks, not global advancement.
Because an external replacement of a whitelisted file can therefore remain undetected, use the
mode only in a directory exclusively managed by Grafx/Pulse. The complete contract, exact grammar,
risks, coexistence and selection guidance are in
[`architecture/ST2_DESCRIPTOR_REVALIDATION.md`](architecture/ST2_DESCRIPTOR_REVALIDATION.md).

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
owner_id()                              current_epoch()
acquire_writer_lease(*, timeout)        renew_lease(lease)       release_lease(lease)
validate_epoch(epoch)                   detect_dead_owner(*, stall_threshold)
takeover()                              exclusive(name, *, timeout)
register_reader(snapshot_lsn)           refresh_reader(handle)
unregister_reader(handle)               reader_horizon()
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
format_version                          checksum(payload)
encode_page(page) -> bytes              decode_page(raw, *, verify=True) -> Page
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
increment(name, value=1.0, labels=None) set_gauge(name, value, labels=None)
observe(name, value, labels=None)       time(name, labels=None)  # a context manager
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
| `OpenMetricsSink` (`metrics="openmetrics"`) | `adapters/metrics_openmetrics.py` | Aggregates into the OpenMetrics text format, exposed at `GET /metrics` on a loopback-by-default port. The default destination is `127.0.0.1:0` — the OS picks a free port, and `Database.metrics_endpoint` reports the address actually bound. |
| `JsonMetricsSink` (`metrics="json"`) | `adapters/metrics_json.py` | Writes documents to the file `metrics_destination` names, through a rotating writer |

The assembled OpenMetrics adapter validates the bind without DNS. With the default
`allow_remote_metrics=False`, the host in `metrics_destination` must be an IP literal for which
`ipaddress.ip_address(host).is_loopback` is true, for example IPv4 `127/8` or IPv6 `::1`. Hostnames, including
`localhost`, and remote or wildcard addresses are refused. A hostname or non-loopback address is
accepted only when `allow_remote_metrics=True`; the field is rejected for the no-op and JSON sinks.
One `RuntimeWarning` is emitted when each remote-address or hostname publisher admitted by that
override starts. It is bind consent, not authentication, TLS or firewall configuration.

IPv6 destinations are configured in bracketed authority form, for example `[::1]:0`. The brackets
are removed for the `::1`/`AF_INET6` bind and restored in the advertised URL, such as
`http://[::1]:49152/metrics`.

---

### `events` — `EventSink`

```
emit(event, payload)
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
name                                    normalize(a)      norm(a)
cosine(a, b)     dot(a, b)     euclidean(a, b)
score(a, b, metric)                     top_k(query, candidates, k, metric)
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

**The corpus proof runs once per process per provider identity (D-29).** `NativeCrc32c` proves the
closed-list provider (`google_crc32c`, declared in `[accel]`) against the whole acceptance corpus
when it is constructed, and `connect()` constructs one per open. That proof is memoized under the
provider's strong identity -- module name, attribute, the file it was loaded from, its version and
the exact function object -- so the second open in a process pays nothing for it. Only a
successful proof is memoized; a refusal is reproduced on the next construction. An injected
provider and an explicit `verify_runtime=True` are never memoized: they keep the per-construction
proof and the per-call oracle. The domain's installer door keeps an independent memo of its own:
the adapter names the same strong identity explicitly, and the door skips its replay only for the
exact wrapper it already proved -- an injected callable never names an identity, so `install_crc32c`
and the closed-list door prove it every time, as before. Both proof stores are strictly bounded to
one current identity per closed module/attribute slot; a replacement evicts the old wrapper and
both old proofs. Raw function identity is compared with `is`, never its equality or hash hooks.
A process-local lock in the adapter serializes closed-provider lookup, validation and publication,
including the private domain door; the pure domain remains free of threading mechanisms. Thus
concurrent first opens run the corpus exactly once in each independent door, while a failure is
never published or inherited by another opener.

**One consequence a caller should know:** two databases in one process do not get independent
checksum implementations. `connect(a, checksum="pure")` followed by `connect(b)` leaves both on
whatever the second call installed. No digest changes — they are byte-identical by construction —
but `crc32c_implementation()` reports the installed name, not the requested one. This is recorded in
`docs/architecture/PUNCHLIST.md`.

---

## Writing an adapter

Satisfy the protocol. That is all there is: no base class, no registration, no decorator.

<!-- okto-grafx-doc-test -->

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

1. **Treat custom adapters as trusted host code.** The registry validates shape without executing
   adapter code; it does not validate signatures or results and the engine does not translate every
   exception at every port call. An exception raised by a custom adapter may propagate unchanged.
   Adapters shipped with Okto Grafx use the `GrafxError` taxonomy. A custom adapter that wants the
   same integration should translate its host failures to an appropriate `GrafxError`, preserve the
   cause and never catch `BaseException`.
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
7. **Own descriptor identity semantics.** `descriptor_revalidation` configures only the shipped
   `LocalStorageDevice`; it is not a new member of the frozen `StorageDevice` protocol. A custom
   adapter remains responsible for ensuring that a cached logical name observes the current file.
   It may optionally implement
   `invalidate_descriptor_identity(file: str | None = None)` to consume buffer-pool cache signals;
   a wrapper preserving this optimization must forward that optional call.

---

## Composing a custom registry

<!-- okto-grafx-doc-test -->

```python
from okto_grafx import DatabaseConfig, connect
from okto_grafx.runtime.bootstrap import build_default_registry, release_ports

config = DatabaseConfig(path=":memory:", page_size=8192)
registry = build_default_registry(config)
registry.bind("metrics", CountingMetrics())
try:
    with connect(":memory:", registry=registry) as db:
        assert db.metrics.enabled
finally:
    # Database.close() does not release a caller-owned registry.
    release_ports(registry)
```

Building one from nothing is also supported, and the fail-closed rule is what makes it safe to try:

<!-- okto-grafx-doc-test -->

```python
from okto_grafx import PortRegistry
from okto_grafx.errors import GrafxPortNotConfigured

registry = PortRegistry()
try:
    registry.require_complete()
except GrafxPortNotConfigured as refused:
    assert tuple(refused.details["missing"]) == PortRegistry.REQUIRED
```

Every missing slot is named in **one** error, so the composition is fixed once rather than one
`connect()` at a time.

Passing `descriptor_revalidation=` together with a caller-owned registry still validates the
configuration value, but it does not reach into or reconfigure `registry.get("storage")`. Construct
the custom adapter with its intended policy yourself. `connect(":memory:")` also accepts the option,
but it is operationally inert because `MemoryStorageDevice` has no OS descriptor identity.

### Inspecting a composition safely

A caller that supplies a `PortRegistry` already owns and can inspect its adapters through that
registry. `Database` deliberately does not publish those live objects back: a storage device or
coordinator would expose write, truncate, lease and takeover doors outside the WAL and fencing
protocols. Its properties are detached immutable observations instead:

<!-- okto-grafx-doc-test -->

```python
from okto_grafx import connect

with connect(":memory:") as db:
    db.storage        # name, page size and a captured file inventory
    db.clock          # clock implementation identity; property access advances no clock
    db.codec          # format and page-size metadata
    db.metrics        # whether collection is enabled
    db.events         # event destination identity
    db.vector_math    # implementation name
    db.coordinator    # participant identity; no lease, epoch or reader-retirement reads
```

If the caller supplied a registry, its live adapter remains available there through, for example,
`registry.get("storage")`.

The same rule covers engine collaborators: `db.catalog`, `db.indexes`, `db.wal`, `db.ledger`,
`db.quarantine`, `db.vectors` and `db.queries` provide immutable inventories or diagnostics. Safe
operations that need live state are explicit methods on `Database`, including `explain`,
`search_vectors`, `inspect_index`, `read_quarantine`, `snapshot_metrics` and `publish_metrics`.
