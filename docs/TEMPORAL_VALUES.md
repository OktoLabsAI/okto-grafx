# Native temporal values (0.0.6 development)

[Query language](QUERY_LANGUAGE.md) · [Full temporal integration plan](specs/TEMPORAL_VALUES_V1.md)

Registered procedures now admit all six native temporal families directly and
inside LIST/MAP/ANY signatures, with detached components and the same native
storage/recovery rules. [Procedure value contract](specs/PROCEDURE_NATIVE_VALUES_V1.md).
This does not widen scalar UDF declarations or add a new storage format.

Six immutable Python values are exported from `okto_grafx`. They can be supplied
as query parameters, returned as detached results, and persisted in their typed
columns or within ANY/list/map properties. They retain their native binary type,
not a text representation. Native query constructors, clock variants, truncation,
duration differences, property access, arithmetic, comparison/ordering and explicit
text rendering are wired to the engine. The [support/refusal matrix](TYPE_SUPPORT.md)
and [installed type-package qualification](reports/FP6_TYPE_WHEEL_QUALIFICATION.md)
define the supported consumers and format admission; unsupported index/transport
combinations must not be inferred from expression support. Final integrated
acceptance remains separate from those recorded qualifications.

| DDL type | Python value | Representation |
| --- | --- | --- |
| DATE | `DateValue(year, month=1, day=1)` | Proleptic Gregorian date; years −999999999 through +999999999, including zero |
| LOCALTIME | `LocalTimeValue(nanoseconds)` | Nanoseconds since midnight, 0 through 86399999999999 |
| TIME | `TimeValue(time, offset_seconds)` | LocalTimeValue plus fixed offset within ±18 hours |
| LOCALDATETIME | `LocalDateTimeValue(date, time)` | DateValue and LocalTimeValue, without a zone |
| DATETIME | `DateTimeValue(local, offset_seconds, zone=None)` | LocalDateTimeValue, recorded offset and optional ASCII logical zone key |
| DURATION | `DurationValue(months=0, days=0, seconds=0, nanoseconds=0)` | Independent calendar months/days and elapsed seconds/nanos; normalized nanos |

The existing `Timestamp(micros)` / TIMESTAMP is unchanged and is a distinct type.
Python `datetime` objects and clock-only `TemporalInstant` are not implicitly
converted. `.isoformat()` provides explicit text rendering. No new connection
setting is introduced here. Query clocks default explicitly to UTC; named/fixed
zones can be selected per call. There is no connection-level timezone option;
use the explicit per-call timezone map when UTC is not the intended interpretation.

## Query functions

Names are case-insensitive. Temporal functions accept positional arguments, not
arbitrary named function arguments; component selection is a map argument.

| Function family | Arguments and behavior |
| --- | --- |
| `date`, `localtime`, `time`, `localdatetime`, `datetime` | Zero arguments captures the current statement value; one ISO string, component/selection map or supported native temporal value constructs that family. A timezone-only map selects the current value in that zone. |
| `duration` | Exactly one ISO duration string, component map or native DurationValue; months and days remain distinct from elapsed seconds. |
| `date.transaction`, `.statement`, `.realtime` (also on the other four instant families) | Zero arguments or one `{timezone: '...'}` map. Transaction and statement modes reuse captured instants; realtime reads the wall clock for each evaluation. |
| `date.truncate(unit, value[, fields])` (also on the other four instant families) | Native truncation and permitted smaller-component overrides. Zone rules and supported units depend on the target family; [precise contract](specs/TEMPORAL_VALUES_V1.md#internal-truncation). |
| `duration.between(a,b)`, `.inMonths(a,b)`, `.inDays(a,b)`, `.inSeconds(a,b)` | Native duration difference with calendar/elapsed and mixed-family rules; [precise contract](specs/TEMPORAL_VALUES_V1.md#internal-differences-between-instants). |
| `datetime.fromEpoch(seconds,nanoseconds)`, `datetime.fromEpochMillis(milliseconds)` | Exact integer epoch construction; no float precision loss. |
| `toString(temporal)` | Explicit ISO rendering of a native temporal result; does not change the stored type. |

NULL arguments propagate after arity validation. Invalid components, text, zones
and argument shapes refuse with native Grafx errors; a late failure in a writing
statement rolls that statement back. Unknown dotted functions remain unsupported:
this is a closed temporal namespace, not arbitrary CALL/UDF dispatch.

```cypher
RETURN date({year:2024, ordinalDay:60}) AS leap_day,
       date.truncate('month', date('2024-02-29')) AS month_start,
       duration.inDays(date('2024-02-28'), date('2024-03-01')) AS elapsed_days,
       toString(datetime('2024-01-01T12:00[Europe/Paris]')) AS rendered
```

The transaction instant is captured at native transaction begin; one statement
instant is shared across its operators and cursor fetches. The wall clock is
separate from lease/deadline/OCC/WAL clocks and can move backwards. Named zones
use the bounded package-backed tzdata provider, not the machine's local timezone.
The current planner conservatively treats temporal calls as nonconstant, avoiding
compile-time evaluation or inappropriate reuse of clock-dependent results.

## Fields, operators and ordering

Dot access applies to native values from constructors, parameters, stored
properties, aliases, CASE and list elements. NULL propagates. Temporal field
names are case-insensitive; ordinary map keys remain case-sensitive. Unknown
fields and fields unavailable on that family refuse when actually evaluated.
An unselected CASE arm does not evaluate an invalid field or overflowing value.

| Family | Fields |
| --- | --- |
| Date, local/zoned datetime | `year`, `quarter`, `month`, `week`, `weekYear`, `day`, `ordinalDay`, `weekday` / `dayOfWeek`, `dayOfQuarter` |
| Local/offset time, local/zoned datetime | `hour`, `minute`, `second`, `millisecond`, `microsecond`, `nanosecond` |
| Offset time, zoned datetime | `timezone`, `offset` (strings), `offsetMinutes`, `offsetSeconds` (integers) |
| Zoned datetime | `epochSeconds`, `epochMillis` |
| Duration | `years`, `quarters`, `months`, `weeks`, `days`, `hours`, `minutes`, `seconds`, `milliseconds`, `microseconds`, `nanoseconds`; component remainders `quartersOfYear`, `monthsOfQuarter`, `monthsOfYear`, `daysOfWeek`, `minutesOfHour`, `secondsOfMinute`, `millisecondsOfSecond`, `microsecondsOfSecond`, `nanosecondsOfSecond` |

Integer fields must fit INT64; overflow refuses instead of wrapping. Duration
fields do not silently convert calendar months/days into elapsed seconds.

Supported arithmetic: temporal ± duration, duration + temporal, duration ±
duration, duration × number (either order), duration ÷ number and unary ± duration.
NULL propagates. Division by zero, nonfinite scale factors, unsupported pairs
and result overflow refuse with native errors. List concatenation keeps its
ordinary semantics, including `date('2024-01-01') + [1]`.

Calendar arithmetic applies months with month-end clamping, then days, then
elapsed seconds/nanos. A named-zone calendar day may differ from 24 elapsed hours
at a daylight-saving transition. No timezone/lease/OCC policy is relaxed.
Fractional duration scaling carries smaller units exactly before the final
nanosecond truncation. [Detailed arithmetic contract](specs/TEMPORAL_VALUES_V1.md#internal-temporal-arithmetic).

```cypher
WITH datetime('2024-03-30T12:00[Europe/Paris]') AS d
RETURN (d + duration('P1D')).hour AS calendar_hour,
       (d + duration('PT24H')).hour AS elapsed_hour
// calendar_hour = 12; elapsed_hour = 13
```

Equality retains the value family, recorded offset/zone and duration components;
equal instants with different zone identities need not be equal values. Different
temporal families are relationally incomparable (`<`, `<=`, `>`, `>=` yield NULL).
For durations, strict `<`/`>` are undefined even for equal values; `<=`/`>=` are
true for equal components and otherwise NULL. Thus `P1D` is not equal to `PT24H`.

ORDER BY has a separate total order: zoned datetime, local datetime, date, offset
time, local time, duration. This temporal group sorts after numbers and before the
legacy TIMESTAMP group. Duration ordering uses exact average-month seconds and
component tie-breaks, not predicate semantics. DISTINCT/grouping keep component
identity; min/max use ordering. Bounded external sort/grouping preserves native
payloads and exact wide internal comparison keys rather than sorting ISO strings.

## Python values and persistence

```python
from okto_grafx import connect, DateValue, DurationValue

with connect("./temporal-demo") as db:
    db.ensure_identity_indexes()  # explicit catalog-v2 prerequisite
    with db.begin("write") as tx:
        tx.execute("CREATE NODE TABLE Event(id INT64, day DATE, details ANY, PRIMARY KEY(id))")
        tx.execute(
            "CREATE (:Event {id:1, day:$day, details:$details})",
            {"day": DateValue(2026, 9, 11),
             "details": {"duration": DurationValue(months=1, nanoseconds=123)}},
        )
    assert db.execute("MATCH (e:Event) RETURN e.day").rows == ((DateValue(2026, 9, 11),),)
```

Typed temporal DDL records `temporal_values_v1` (catalog bit 23). The first temporal
value written into an existing ANY/nested property adds the same capability in
the data transaction, preserving any schema already staged there. Rollback does
not publish it; original-snapshot OCC still rejects conflicting schema writers.
After activation, ordinary row commits do not need another temporal schema lock.
Two concurrent first-time activations may conflict on catalog publication and
must use the normal transaction retry contract. Close/commit lifecycle ownership
is unchanged: a commit that already won the participant section may finish while
close waits; new operations after close are refused.
Old readers that do not recognize the capability refuse before applying WAL pages.
The [isolated wheel matrix](V006_COMPATIBILITY.md#later-functional-parity-temporal-boundary)
checks materialized files, pending durable WAL and already-open older handles.
Legacy catalogs must be explicitly upgraded; do not rewrite their bytes manually.

Encoding validates native components and decoding rejects malformed frames.
DATETIME preserves its recorded zone key and offset without consulting current
timezone rules on read. Its value constructor does not certify that the offset
agrees with that zone's rules: use an explicit rule-aware constructor/provider when
binding named zones. NaN/infinity remain forbidden in stored properties, including
nested values. Normal transaction/query depth and byte limits still apply.

Whole node/relationship/path results also retain native temporal properties,
including nested values; they are not limited to scalar projections.
Their `to_dict()` uses [explicit lossless temporal tags](ENTITY_VALUES.md#json-grammar),
with decimal strings for wide coordinates and no timezone-rule lookup.
CLI scalar/nested `--json` observations now share these temporal tags, without
changing CLI parameter decoding. Arrow/Pandas/Polars/Parquet use explicit
[components-v1 temporal structs](EXTENSIONS_AND_ARROW.md#exact-native-temporal-values-006-development),
with mandatory type/encoding metadata, exact coordinates and native import
validation. Host datetime ranges and timezone conversions are not involved.

## Transfer, copy and history

[Logical transfer](LOGICAL_TRANSFER.md) preserves all six native families in typed
columns, nested ANY values and flexible property maps. Import activates the required
catalog version before installing typed temporal schemas. Values retain exact wide
coordinates, nanoseconds and recorded zone/offset identity; transfer does not resolve
zone rules or convert values to strings. Custom exact-index declarations are rebuilt
against the imported records, not copied as physical index pages. Tests cover both
pure and NumPy codecs and equality lookups on all six temporal column types.

[Catalog copy](CATALOG_COPY.md) preserves the same values, remaps entity/endpoints
and records the normal idempotency receipt. It requires **existing compatible target
tables**. Enable source identity indexes and commit history before the captured data
commit; activating history after that commit does not retroactively give it provenance.

[System-time history](SYSTEM_TIME_HISTORY.md) retains native property values in old
and new row versions. Both scan and indexed as-of/diff reads preserve them across
reopening and [physical backup/restore](BACKUP_RESTORE.md). This is distinct from the
temporal value of a property: it does not introduce valid-time/bitemporal semantics.
Logical transfer/copy still require `history="current-only"` when system-time history
is enabled; they do not transfer that history. Physical restore remains an offline
replacement, not a second independently writable copy of the same database identity.

CSV, JSONL and SQLite readers/importers now accept all six explicitly declared
families using [canonical tagged JSON fields](LOCAL_TEXT_IMPORT.md#native-temporal-fields-006-development).
CSV cells and SQLite TEXT contain the encoded object; JSONL also accepts the object
directly. These imports preserve recorded values, not ISO-text inference or timezone
construction. Existing NULL rules, source bounds and whole-call staging remain.

Temporal typed primary keys and secondary/composite hash keys now have explicit
[support and refusal policies](INDEXES_AND_VECTORS.md#native-temporal-key-support-006-development),
with lifecycle, snapshot, competing-writer and process-crash qualification.
Ordered/full-text temporal indexes and indexes over ANY/flexible properties remain
explicitly unsupported, not silently narrowed or stringified.

Remaining qualification includes the complete package/lifecycle acceptance audit,
the combined FP-5/FP-6 cross-version checkpoint and Pulse
consumption. These focused integrations do not close FP-5. See the linked
specification for exact semantics, wire frames, test receipts and the unchanged
1,004-case temporal matrix.
