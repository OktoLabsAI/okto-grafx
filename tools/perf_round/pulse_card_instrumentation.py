"""Bounded, reversible hooks for the post-drain Pulse single-card profiler.

Importing this module installs nothing. RAW runs never install the hooks. Instrumented runs
record only aggregate counts, bounded timings and engine statistics: never query text,
parameters, rows, keys, record references or commit numbers. The hooks do not perform extra
heap/index/WAL reads, so a measurement cannot manufacture the traversal it claims to observe.

This is deliberately not a database census. Header observations may include repeated decodes
of the same version. The physical distance-to-tail and unique live/dead-version census required
by P0.3 is a separate, untimed operation over a second authenticated disposable clone.
"""

from __future__ import annotations

import functools
import inspect
import math
import threading
import time
from collections.abc import Mapping
from typing import Any

MAX_SAMPLES = 100_000
MAX_STATEMENTS = 100_000
MAX_DATABASE_OPENS = 64
MAX_ENDPOINT_HITS = 100_000

_INSTALL_LOCK = threading.Lock()
_ACTIVE_INSTRUMENTATION: PulseCardInstrumentation | None = None


class InstrumentationError(RuntimeError):
    """The expected Grafx hook surface is absent or cannot be installed safely."""


class _BoundedSamples:
    def __init__(self, limit: int = MAX_SAMPLES) -> None:
        self._limit = limit
        self._values: list[int] = []
        self.total = 0

    def add(self, value: int) -> None:
        self.total += 1
        if len(self._values) < self._limit:
            self._values.append(int(value))

    def report(self) -> dict[str, Any]:
        ordered = sorted(self._values)

        def percentile(fraction: float) -> int | None:
            if not ordered:
                return None
            index = max(
                0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1)
            )
            return ordered[index]

        return {
            "total": self.total,
            "retained": len(ordered),
            "truncated": self.total > len(ordered),
            "min": ordered[0] if ordered else None,
            "p50": percentile(0.50),
            "p90": percentile(0.90),
            "p99": percentile(0.99),
            "max": ordered[-1] if ordered else None,
        }


class PulseCardInstrumentation:
    """Install process-local Grafx hooks and restore every exact descriptor on exit."""

    def __init__(self) -> None:
        self._event_lock = threading.RLock()
        self._counter_local = threading.local()
        self._counter_shards: list[dict[str, int]] = []
        self._counter_shards_lock = threading.Lock()
        self._lookup_local = threading.local()
        self._patches: list[tuple[object, str, object, object]] = []
        self._installed = False
        self._instrumentation_complete = True
        self._observation_failures: dict[str, int] = {}
        self._statistics: dict[str, int] = {}
        self._statements: list[dict[str, Any]] = []
        self._statement_total = 0
        self._database_opens: list[dict[str, Any]] = []
        self._database_open_total = 0
        self._baseline_handles: list[dict[str, Any]] = []
        self._baseline_handle_total = 0
        self._lookup_headers = _BoundedSamples()
        self._index_candidates = _BoundedSamples()
        self._query_duration_ns = _BoundedSamples()
        self._commit_duration_ns = _BoundedSamples()
        self._endpoint_hit_coordinates: list[tuple[int, int]] = []
        self._endpoint_hit_total = 0
        self._target_heap: Any | None = None

    def __enter__(self) -> PulseCardInstrumentation:
        self.install()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _counter_shard(self) -> dict[str, int]:
        shard = getattr(self._counter_local, "counters", None)
        if shard is None:
            shard = {}
            with self._counter_shards_lock:
                self._counter_shards.append(shard)
            self._counter_local.counters = shard
        return shard

    def _mark_observation_failure(self, name: str) -> None:
        # Observation is best-effort: an allocation failure in a probe must not
        # replace the result (or original exception) of the operation observed.
        self._instrumentation_complete = False
        try:
            with self._event_lock:
                self._observation_failures[name] = (
                    self._observation_failures.get(name, 0) + 1
                )
        except BaseException:
            pass

    def _bump(self, name: str, amount: int = 1) -> None:
        try:
            shard = self._counter_shard()
            shard[name] = shard.get(name, 0) + amount
        except BaseException:
            self._mark_observation_failure("counter")

    def _patch(self, owner: object, name: str, replacement: object) -> None:
        try:
            original = inspect.getattr_static(owner, name)
        except AttributeError as failure:
            raise InstrumentationError(
                f"missing hook surface {owner!r}.{name}"
            ) from failure
        setattr(owner, name, replacement)
        self._patches.append((owner, name, original, replacement))

    def _lookup_stack(self) -> list[dict[str, Any]]:
        stack = getattr(self._lookup_local, "stack", None)
        if stack is None:
            stack = []
            self._lookup_local.stack = stack
        return stack

    def _endpoint_context_active(self) -> bool:
        try:
            return int(getattr(self._lookup_local, "endpoint_depth", 0)) > 0
        except BaseException:
            self._mark_observation_failure("endpoint_context")
            return False

    def _record_endpoint_hit(
        self, store: Any, table: Any, state: Mapping[str, Any]
    ) -> None:
        """Retain a bounded private coordinate for post-workload aggregation.

        Coordinates never enter :meth:`report`.  The P0.3 driver resolves them against
        ``HeapStore.pages_of`` only after the timed workload and publishes an aggregate
        distance histogram, never table/page/slot identifiers.
        """

        try:
            if self._target_heap is None or store is not self._target_heap:
                raise ValueError("endpoint hit came from an unbound heap store")
            table_id = getattr(table, "table_id")
            page = state.get("hit_page")
            if (
                type(table_id) is not int
                or type(page) is not int
                or table_id < 0
                or page < 0
            ):
                raise ValueError("invalid endpoint hit coordinate")
            with self._event_lock:
                self._endpoint_hit_total += 1
                if len(self._endpoint_hit_coordinates) < MAX_ENDPOINT_HITS:
                    self._endpoint_hit_coordinates.append((table_id, page))
        except BaseException:
            self._mark_observation_failure("endpoint_hit_coordinate")

    def _endpoint_hit_snapshot(self) -> tuple[tuple[int, int], ...]:
        """Freeze private coordinates after every hook and workload thread stopped."""
        self._counter_snapshot()
        with self._event_lock:
            return tuple(self._endpoint_hit_coordinates)

    def endpoint_hit_locality(self, database: Any) -> dict[str, Any]:
        """Resolve actual endpoint hits against post-workload chain order.

        This is deliberately called after :meth:`close`, outside the timed card operation.  It
        performs the extra ``pages_of`` walks needed to turn private coordinates into a safe
        aggregate.  A truncated or unreconciled coordinate set is refused rather than presented
        as evidence for the P1.4 locality threshold.
        """

        from tools.perf_round.heap_census import summarize_tail_distances

        if self._target_heap is None or database._heap is not self._target_heap:
            raise InstrumentationError(
                "endpoint locality was requested for a database other than the observed target"
            )
        coordinates = self._endpoint_hit_snapshot()
        with self._event_lock:
            total = self._endpoint_hit_total
        if len(coordinates) != total:
            raise InstrumentationError(
                "endpoint hit coordinates were truncated; locality is not exact"
            )

        counters = self._counter_snapshot()
        if counters.get("endpoint_lookup_hits", 0) != total:
            raise InstrumentationError(
                "endpoint hit coordinates do not reconcile with observed lookup hits"
            )

        table_weights: dict[int, dict[int, int]] = {}
        for table_id, page in coordinates:
            weights = table_weights.setdefault(table_id, {})
            weights[page] = weights.get(page, 0) + 1

        tables = {
            int(table.table_id): table for table in database.catalog.catalog.tables()
        }
        samples = []
        for table_id, weights in table_weights.items():
            table = tables.get(table_id)
            if table is None:
                raise InstrumentationError(
                    "an endpoint hit names a table absent after the workload"
                )
            samples.append((database._heap.pages_of(table), weights))

        aggregate = summarize_tail_distances(samples).as_dict()
        ratio = aggregate["last_10_percent_ratio"]
        return {
            "semantics": "actual_endpoint_lookup_hits_to_post_workload_tail_pages",
            "exact": True,
            "extra_reads_outside_timed_workload": True,
            "total": total,
            "distance_pages": aggregate,
            "p1_4_last_10_percent_threshold_met": (ratio is not None and ratio >= 0.50),
            "p1_4_threshold_evaluable": total > 0,
        }

    def install(self) -> None:
        global _ACTIVE_INSTRUMENTATION
        with _INSTALL_LOCK:
            if self._installed:
                raise InstrumentationError("instrumentation is already installed")
            if _ACTIVE_INSTRUMENTATION is not None:
                raise InstrumentationError("another instrumentation instance is active")
            _ACTIVE_INSTRUMENTATION = self
            try:
                self._install_hooks()
            except BaseException:
                try:
                    self._restore_patches()
                finally:
                    _ACTIVE_INSTRUMENTATION = None
                raise
            self._installed = True

    def _install_hooks(self) -> None:
        import okto_grafx
        from okto_grafx.domain.ids import is_committed_csn, is_open_end_csn
        from okto_grafx.domain.model.record import RecordHeader
        from okto_grafx.engine import heap_store, query_engine
        from okto_grafx.engine.buffer_pool import BufferPool
        from okto_grafx.engine.database import Database, Transaction
        from okto_grafx.engine.heap_store import HeapStore
        from okto_grafx.engine.index_manager import IndexManager
        from okto_grafx.engine.query_engine import QueryEngine
        from okto_grafx.engine.vector_engine import VectorEngine

        original_connect = inspect.getattr_static(okto_grafx, "connect")

        @functools.wraps(original_connect)
        def connect(*args: object, **kwargs: object) -> Any:
            database = original_connect(*args, **kwargs)
            self._record_database_open(database)
            return database

        self._patch(okto_grafx, "connect", connect)

        record_decode_descriptor = inspect.getattr_static(RecordHeader, "decode")
        if not isinstance(record_decode_descriptor, classmethod):
            raise InstrumentationError(
                "RecordHeader.decode is not the expected classmethod"
            )
        original_record_decode = record_decode_descriptor.__func__

        @functools.wraps(original_record_decode)
        def record_decode(cls: type, raw: bytes) -> Any:
            header = original_record_decode(cls, raw)
            try:
                self._bump("record_header_decode_calls")
                if is_committed_csn(header.xmin):
                    classification = (
                        "record_header_observed_committed_open"
                        if is_open_end_csn(header.xmax)
                        else "record_header_observed_committed_ended"
                    )
                    self._bump(classification)
                else:
                    self._bump("record_header_observed_noncommitted_xmin")
                stack = self._lookup_stack()
                if stack:
                    stack[-1]["headers"] += 1
            except BaseException:
                self._mark_observation_failure("record_header")
            return header

        self._patch(RecordHeader, "decode", classmethod(record_decode))

        record_peek_descriptor = inspect.getattr_static(RecordHeader, "peek", None)
        if record_peek_descriptor is not None:
            if not isinstance(record_peek_descriptor, classmethod):
                raise InstrumentationError(
                    "RecordHeader.peek is not the expected classmethod"
                )
            original_record_peek = record_peek_descriptor.__func__

            @functools.wraps(original_record_peek)
            def record_peek(cls: type, raw: bytes) -> Any:
                result = original_record_peek(cls, raw)
                try:
                    self._bump("record_header_peek_calls")
                    stack = self._lookup_stack()
                    if stack:
                        stack[-1]["headers"] += 1
                except BaseException:
                    self._mark_observation_failure("record_header_peek")
                return result

            self._patch(RecordHeader, "peek", classmethod(record_peek))

        original_decode_tuple = inspect.getattr_static(heap_store, "decode_tuple")

        @functools.wraps(original_decode_tuple)
        def decode_tuple(*args: object, **kwargs: object) -> Any:
            result = original_decode_tuple(*args, **kwargs)
            self._bump("tuple_decode_calls")
            return result

        self._patch(heap_store, "decode_tuple", decode_tuple)

        original_read_page = inspect.getattr_static(BufferPool, "_read_page")

        @functools.wraps(original_read_page)
        def read_page(pool: Any, *args: object, **kwargs: object) -> Any:
            self._bump("buffer_pool_read_page_calls")
            return original_read_page(pool, *args, **kwargs)

        self._patch(BufferPool, "_read_page", read_page)

        original_walk = inspect.getattr_static(HeapStore, "_walk")

        @functools.wraps(original_walk)
        def walk(store: Any, *args: object, **kwargs: object):
            self._bump("heap_walk_started")
            try:
                for item in original_walk(store, *args, **kwargs):
                    self._bump("heap_walk_yields")
                    try:
                        stack = self._lookup_stack()
                        if (
                            stack
                            and stack[-1].get("endpoint") is True
                            and stack[-1].get("store") is store
                            and args
                            and stack[-1].get("table") is args[0]
                            and "hit_page" not in stack[-1]
                        ):
                            stack[-1]["hit_page"] = int(item[0].page)
                    except BaseException:
                        self._mark_observation_failure("endpoint_hit_observation")
                    yield item
            except GeneratorExit:
                raise
            except BaseException:
                self._bump("heap_walk_failed")
                raise

        self._patch(HeapStore, "_walk", walk)
        self._wrap_counted(HeapStore, "read", "heap_read_calls")

        original_scan = inspect.getattr_static(HeapStore, "scan")

        @functools.wraps(original_scan)
        def scan(store: Any, *args: object, **kwargs: object):
            self._bump("heap_scan_started")
            try:
                for item in original_scan(store, *args, **kwargs):
                    self._bump("heap_scan_yields")
                    yield item
            except GeneratorExit:
                raise
            except BaseException:
                self._bump("heap_scan_failed")
                raise

        self._patch(HeapStore, "scan", scan)

        original_materialise_edge = inspect.getattr_static(
            query_engine, "_materialise_edge"
        )

        @functools.wraps(original_materialise_edge)
        def materialise_edge(*args: object, **kwargs: object) -> Any:
            # The optimized query path no longer routes through HeapStore.require_endpoints.
            # Preserve the counter's semantic unit -- one edge validation -- at its executor
            # boundary, including pending/physical mixtures and self-loops.
            self._bump("endpoint_validation_calls")
            previous_depth = 0
            context_installed = False
            try:
                previous_depth = int(getattr(self._lookup_local, "endpoint_depth", 0))
                self._lookup_local.endpoint_depth = previous_depth + 1
                context_installed = True
            except BaseException:
                self._mark_observation_failure("endpoint_context")
            try:
                result = original_materialise_edge(*args, **kwargs)
            except BaseException:
                self._bump("endpoint_validation_failed")
                raise
            finally:
                if context_installed:
                    try:
                        self._lookup_local.endpoint_depth = previous_depth
                    except BaseException:
                        self._mark_observation_failure("endpoint_context")
            self._bump("endpoint_validation_succeeded")
            return result

        self._patch(query_engine, "_materialise_edge", materialise_edge)

        original_visible_identity = inspect.getattr_static(
            query_engine, "_visible_identity_with_ref"
        )

        @functools.wraps(original_visible_identity)
        def visible_identity(
            engine: Any, context: Any, table: Any, record_id: int
        ) -> Any:
            state: dict[str, Any] | None = None
            stack: list[dict[str, Any]] | None = None
            try:
                state = {
                    "headers": 0,
                    "endpoint": True,
                    "store": engine.heap,
                    "table": table,
                }
                stack = self._lookup_stack()
                stack.append(state)
            except BaseException:
                state = None
                stack = None
                self._mark_observation_failure("lookup_stack")
            self._bump("heap_lookup_calls")
            self._bump("endpoint_lookup_calls")
            try:
                result = original_visible_identity(engine, context, table, record_id)
            except BaseException:
                self._bump("heap_lookup_failed")
                self._bump("endpoint_lookup_failed")
                raise
            finally:
                if stack is not None and state is not None:
                    try:
                        stack.pop()
                        self._record_sample(
                            self._lookup_headers,
                            int(state["headers"]),
                            "lookup_headers_sample",
                        )
                    except BaseException:
                        self._mark_observation_failure("lookup_stack")
            if result is None:
                self._bump("heap_lookup_misses")
                self._bump("endpoint_lookup_misses")
            else:
                self._bump("heap_lookup_hits")
                self._bump("endpoint_lookup_hits")
                if state is not None:
                    try:
                        state["hit_page"] = int(result[0].page)
                        self._record_endpoint_hit(engine.heap, table, state)
                    except BaseException:
                        self._mark_observation_failure("endpoint_hit_observation")
            return result

        self._patch(query_engine, "_visible_identity_with_ref", visible_identity)

        original_require_endpoints = inspect.getattr_static(
            HeapStore, "require_endpoints"
        )

        @functools.wraps(original_require_endpoints)
        def require_endpoints(store: Any, *args: object, **kwargs: object) -> Any:
            self._bump("endpoint_validation_calls")
            context_installed = False
            previous_depth = 0
            try:
                previous_depth = int(getattr(self._lookup_local, "endpoint_depth", 0))
                self._lookup_local.endpoint_depth = previous_depth + 1
                context_installed = True
            except BaseException:
                self._mark_observation_failure("endpoint_context")
            try:
                result = original_require_endpoints(store, *args, **kwargs)
            except BaseException:
                self._bump("endpoint_validation_failed")
                raise
            finally:
                if context_installed:
                    try:
                        self._lookup_local.endpoint_depth = previous_depth
                    except BaseException:
                        self._mark_observation_failure("endpoint_context")
            self._bump("endpoint_validation_succeeded")
            return result

        self._patch(HeapStore, "require_endpoints", require_endpoints)

        original_lookup = inspect.getattr_static(HeapStore, "lookup")

        @functools.wraps(original_lookup)
        def lookup(store: Any, table: Any, record_id: int, snapshot: Any) -> Any:
            state: dict[str, Any] | None = None
            stack: list[dict[str, Any]] | None = None
            try:
                state = {
                    "headers": 0,
                    "endpoint": self._endpoint_context_active(),
                    "store": store,
                    "table": table,
                }
                stack = self._lookup_stack()
                stack.append(state)
            except BaseException:
                state = None
                stack = None
                self._mark_observation_failure("lookup_stack")
            self._bump("heap_lookup_calls")
            if state is not None and state.get("endpoint") is True:
                self._bump("endpoint_lookup_calls")
            try:
                result = original_lookup(store, table, record_id, snapshot)
            except BaseException:
                self._bump("heap_lookup_failed")
                if state is not None and state.get("endpoint") is True:
                    self._bump("endpoint_lookup_failed")
                raise
            finally:
                if stack is not None and state is not None:
                    try:
                        stack.pop()
                        self._record_sample(
                            self._lookup_headers,
                            int(state["headers"]),
                            "lookup_headers_sample",
                        )
                    except BaseException:
                        self._mark_observation_failure("lookup_stack")
            self._bump(
                "heap_lookup_hits" if result is not None else "heap_lookup_misses"
            )
            if state is not None and state.get("endpoint") is True:
                if result is None:
                    self._bump("endpoint_lookup_misses")
                else:
                    self._bump("endpoint_lookup_hits")
                    self._record_endpoint_hit(store, table, state)
            return result

        self._patch(HeapStore, "lookup", lookup)

        original_index_lookup = inspect.getattr_static(IndexManager, "lookup")

        @functools.wraps(original_index_lookup)
        def index_lookup(manager: Any, *args: object, **kwargs: object) -> Any:
            self._bump("index_lookup_calls")
            try:
                result = original_index_lookup(manager, *args, **kwargs)
            except BaseException:
                self._bump("index_lookup_failed")
                raise
            try:
                candidate_count = len(result)
            except BaseException:
                self._mark_observation_failure("index_candidates_sample")
            else:
                self._record_sample(
                    self._index_candidates,
                    candidate_count,
                    "index_candidates_sample",
                )
            return result

        self._patch(IndexManager, "lookup", index_lookup)
        self._wrap_counted(VectorEngine, "search", "vector_search_calls")

        original_query_execute = inspect.getattr_static(QueryEngine, "execute")

        @functools.wraps(original_query_execute)
        def query_execute(
            engine: Any, text: str, *args: object, **kwargs: object
        ) -> Any:
            started_ns = time.perf_counter_ns()
            try:
                result = original_query_execute(engine, text, *args, **kwargs)
            except BaseException as failure:
                self._record_statement(
                    time.perf_counter_ns() - started_ns, None, failure
                )
                raise
            self._record_statement(time.perf_counter_ns() - started_ns, result, None)
            return result

        self._patch(QueryEngine, "execute", query_execute)

        self._wrap_counted(Database, "begin", "transaction_begin_calls")
        self._wrap_counted(Database, "retry", "transaction_retry_calls")
        self._wrap_counted(Database, "rebuild_vector_index", "vector_rebuild_calls")
        self._wrap_commit(Transaction)
        self._wrap_counted(Transaction, "rollback", "transaction_rollback_calls")

    def _wrap_counted(self, owner: object, name: str, counter: str) -> None:
        original = inspect.getattr_static(owner, name)

        @functools.wraps(original)
        def counted(*args: object, **kwargs: object) -> Any:
            self._bump(counter)
            try:
                return original(*args, **kwargs)
            except BaseException:
                self._bump(f"{counter}_failed")
                raise

        self._patch(owner, name, counted)

    def _wrap_commit(self, transaction_type: type) -> None:
        original = inspect.getattr_static(transaction_type, "commit")

        @functools.wraps(original)
        def commit(transaction: Any, *args: object, **kwargs: object) -> Any:
            self._bump("transaction_commit_calls")
            started_ns = time.perf_counter_ns()
            try:
                report = original(transaction, *args, **kwargs)
            except BaseException:
                self._record_sample(
                    self._commit_duration_ns,
                    time.perf_counter_ns() - started_ns,
                    "commit_duration_sample",
                )
                self._bump("transaction_commit_failed")
                raise
            self._record_sample(
                self._commit_duration_ns,
                time.perf_counter_ns() - started_ns,
                "commit_duration_sample",
            )
            self._bump("transaction_commit_succeeded")
            try:
                if getattr(report, "wrote", False) is True:
                    self._bump("transaction_commit_wrote")
                if getattr(report, "durable", False) is True:
                    self._bump("transaction_commit_durable")
            except BaseException:
                self._mark_observation_failure("commit_report")
            return report

        self._patch(transaction_type, "commit", commit)

    def _record_sample(
        self, samples: _BoundedSamples, value: int, failure_name: str
    ) -> None:
        try:
            with self._event_lock:
                samples.add(value)
        except BaseException:
            self._mark_observation_failure(failure_name)

    @staticmethod
    def _database_observation(database: Any) -> dict[str, Any]:
        from okto_grafx.domain.page import crc32c_implementation

        return {
            "page_size": int(database.identity.page_size),
            "buffer_budget_bytes": int(database.pool.budget_bytes),
            "descriptor_revalidation": str(database.descriptor_revalidation),
            "checksum_implementation": str(crc32c_implementation()),
        }

    def _record_database_open(self, database: Any) -> None:
        try:
            observation = self._database_observation(database)
            with self._event_lock:
                self._database_open_total += 1
                if len(self._database_opens) < MAX_DATABASE_OPENS:
                    self._database_opens.append(observation)
        except BaseException:
            self._mark_observation_failure("database_open")

    def observe_database(self, database: Any) -> None:
        """Record an already-open pooled handle without changing its lifecycle."""
        try:
            observation = self._database_observation(database)
            with self._event_lock:
                heap = database._heap
                if self._target_heap is not None and self._target_heap is not heap:
                    raise InstrumentationError(
                        "more than one target heap was bound to the instrumentation"
                    )
                self._target_heap = heap
                self._baseline_handle_total += 1
                if len(self._baseline_handles) < MAX_DATABASE_OPENS:
                    self._baseline_handles.append(observation)
        except BaseException:
            self._mark_observation_failure("baseline_handle")

    def _record_statement(
        self,
        duration_ns: int,
        result: Any | None,
        failure: BaseException | None,
    ) -> None:
        try:
            statistics: dict[str, int] = {}
            rows: int | None = None
            if result is not None:
                rows = len(result.rows)
                raw_statistics = result.statistics
                if isinstance(raw_statistics, Mapping):
                    statistics = {
                        str(name): int(value)
                        for name, value in raw_statistics.items()
                        if type(name) is str and type(value) is int and value >= 0
                    }
            with self._event_lock:
                self._statement_total += 1
                ordinal = self._statement_total
                self._query_duration_ns.add(max(0, int(duration_ns)))
                for name, value in statistics.items():
                    self._statistics[name] = self._statistics.get(name, 0) + value
                if len(self._statements) < MAX_STATEMENTS:
                    self._statements.append(
                        {
                            "ordinal": ordinal,
                            "duration_ns": max(0, int(duration_ns)),
                            "rows": rows,
                            "statistics": dict(sorted(statistics.items())),
                            "error_type": (
                                type(failure).__name__ if failure is not None else None
                            ),
                        }
                    )
        except BaseException:
            self._mark_observation_failure("statement")

    def _restore_patches(self) -> None:
        conflicts: list[str] = []
        while self._patches:
            owner, name, original, replacement = self._patches.pop()
            current = inspect.getattr_static(owner, name)
            if current is not replacement:
                conflicts.append(f"{owner!r}.{name}")
                continue
            setattr(owner, name, original)
        if conflicts:
            raise InstrumentationError(
                "instrumented descriptors changed before restoration: "
                + ", ".join(conflicts)
            )

    def close(self) -> None:
        global _ACTIVE_INSTRUMENTATION
        with _INSTALL_LOCK:
            if not self._installed and not self._patches:
                return
            try:
                self._restore_patches()
            finally:
                self._installed = False
                if _ACTIVE_INSTRUMENTATION is self:
                    _ACTIVE_INSTRUMENTATION = None

    def _counter_snapshot(self) -> dict[str, int]:
        if self._installed:
            raise InstrumentationError(
                "close hooks and await all workload threads before requesting the report"
            )
        with self._counter_shards_lock:
            frozen = tuple(dict(shard) for shard in self._counter_shards)
        merged: dict[str, int] = {}
        for shard in frozen:
            for name, value in shard.items():
                merged[name] = merged.get(name, 0) + value
        return dict(sorted(merged.items()))

    def report(self) -> dict[str, Any]:
        counters = self._counter_snapshot()
        with self._event_lock:
            return {
                "bounded": True,
                "instrumentation_complete": self._instrumentation_complete,
                "observation_failures": dict(
                    sorted(self._observation_failures.items())
                ),
                "counters": counters,
                "query_statistics": dict(sorted(self._statistics.items())),
                "statements": list(self._statements),
                "statement_total": self._statement_total,
                "statements_truncated": self._statement_total > len(self._statements),
                "database_opens": list(self._database_opens),
                "database_open_total": self._database_open_total,
                "database_opens_truncated": self._database_open_total
                > len(self._database_opens),
                "baseline_handles_observed": list(self._baseline_handles),
                "baseline_handle_total": self._baseline_handle_total,
                "baseline_handles_truncated": self._baseline_handle_total
                > len(self._baseline_handles),
                "endpoint_hit_coordinates": {
                    "total": self._endpoint_hit_total,
                    "retained": len(self._endpoint_hit_coordinates),
                    "truncated": self._endpoint_hit_total
                    > len(self._endpoint_hit_coordinates),
                    "serialized": False,
                },
                "lookup_headers_examined": self._lookup_headers.report(),
                "index_candidates_returned": self._index_candidates.report(),
                "query_duration_ns": self._query_duration_ns.report(),
                "commit_duration_ns": self._commit_duration_ns.report(),
                "vector_activity": {
                    "observed": True,
                    "search_calls": counters.get("vector_search_calls", 0),
                    "search_failures": counters.get("vector_search_calls_failed", 0),
                    "rebuild_calls": counters.get("vector_rebuild_calls", 0),
                    "rebuild_failures": counters.get("vector_rebuild_calls_failed", 0),
                },
                "census_semantics": "repeated_decode_observations_not_unique_versions",
            }


def null_instrumentation_report() -> dict[str, Any]:
    """Explicit RAW marker; importing this module installs no hooks."""

    return {
        "bounded": True,
        "raw_uninstrumented": True,
        "instrumentation_complete": True,
        "observation_failures": {},
        "counters": {},
        "query_statistics": {},
        "statements": [],
        "statement_total": 0,
        "statements_truncated": False,
        "database_opens": [],
        "database_open_total": 0,
        "database_opens_truncated": False,
        "baseline_handles_observed": [],
        "baseline_handle_total": 0,
        "baseline_handles_truncated": False,
        "vector_activity": {
            "observed": False,
            "search_calls": None,
            "search_failures": None,
            "rebuild_calls": None,
            "rebuild_failures": None,
        },
        "census_semantics": "not_collected_in_raw_run",
    }
