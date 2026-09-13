# FP-5 native temporal values: execution matrix and integration contract

[Functional parity plan](FUNCTIONAL_PARITY_PLAN.md#fp-5--complete-temporal-values-not-string-only-constructors)
· [Roadmap](../../ROADMAP.md#functional-parity-expansion-plan)

Status: native public query/value/storage integration implemented in the 0.0.6
development worktree, **not an accepted FP-5 package**. Remaining index, transport,
cross-version and consumer qualification below is still required. This document
refines the existing authorized package; it does not replace its exit criteria or
reduce the mandatory case profile.

## Current foundation and boundaries

`domain/model/temporal_values.py` now contains internal immutable `DateValue`,
`LocalTimeValue`, `DurationValue`, `LocalDateTimeValue`, `TimeValue` and
`DateTimeValue` primitives. Calendar calculations use integer
proleptic-Gregorian days, support year zero and expanded signed years, and do not
depend on Python datetime's representable year interval. Date operations cover
epoch-day inversion, calendar/week/ordinal construction, weekday/week-year/week/quarter access,
day shifts and month-end clamping. Local time preserves nanoseconds. Duration
normalizes signed subsecond carry while retaining months and days separately from
elapsed seconds. Component type/range errors are structured native refusals.

Local/zoned containers retain exact nanos, second-resolution offsets and named-zone
identity. Fixed-offset and named-zone datetime conversion support the full native year range;
time-of-day comparison keys do not wrap UTC values across midnight. Equal instants
with different offsets/names remain distinct values. Named-zone offset agreement
belongs to the explicit rule provider, not ambient I/O in the value constructor.

These classes are now exported from `okto_grafx`, admitted as native parameters,
and assigned durable tags for typed/ANY/nested storage. Cypher constructors, clock
variants, truncation/differences, toString, property access, arithmetic and
comparison/ordering now use native execution. The existing microsecond UTC `Timestamp`
is unchanged. [Public parameter/storage contract](../TEMPORAL_VALUES.md).
Qualification of all consumption routes remains required before this package can
be accepted. The implementation sections below preserve chronological preparatory
boundaries; their earlier “pending” statements are superseded by this current
status and the newest receipts in [Current evidence](#current-evidence).

### Internal text construction

`domain/temporal_text.py` adds native `parse_date`, `parse_localtime`, `parse_time`,
`parse_localdatetime`, `parse_datetime`, `parse_duration` and `parse_offset` helpers.
These are internal integration building blocks, **not exported public functions
or enabled Cypher constructors**. Each helper requires an exact ASCII string of
1–1,024 characters and reports structured syntax/type/range refusals; it never
calls host object string conversions. NULL handling belongs to the future query
overload, not this text-only layer.

| Value | Internal accepted text forms / behavior |
| --- | --- |
| Date | Compact/separated calendar, week, ordinal and quarter forms; signed expanded years; omitted trailing components use their documented calendar defaults. Invalid week/day/quarter combinations refuse. |
| Local time | Compact or colon-separated hour/minute/second forms, optional leading `T`, decimal point/comma for up to nine second-fraction digits. No offset is discarded. |
| Offset time | Local time plus `Z` or signed hour/minute/second offset, including compact offsets; absent offset uses explicit `default_offset=0`. Named zones additionally require an injected resolver and reference date, never the machine clock. |
| Local datetime | Native date and optional `T` clock, with midnight when the clock is absent. |
| Zoned datetime | Native date/clock plus fixed offset or bracketed zone; named resolution checks an explicit offset against rules and preserves zone identity. Missing provider refuses instead of substituting UTC. |
| Duration | Ordered ISO unit quantities, overall/component signs, fractional final component and alternate numeric date/time quantities. Duplicate/out-of-order units, empty `T`, or smaller components after a fractional component refuse. |

Duration construction uses exact decimal rational arithmetic. Integral months
and days stay separate; only a fractional month uses 2,629,746 seconds to carry
into days/elapsed time. Remaining sub-nanosecond fractions of larger units truncate
toward zero. The seconds field itself allows at most nine fractional digits.
This policy does not turn a stored whole calendar month/day into a fixed elapsed
duration. Serialization keeps independent component signs, does not fold elapsed
hours into calendar days, and renders zero as `PT0S`. Local clock fractions use
lossless 3/6/9-digit groups; duration fractions omit insignificant trailing zeros.
[Temporal string forms](https://neo4j.com/docs/cypher-manual/current/values-and-types/temporal/)
and [duration constructors](https://neo4j.com/docs/cypher-manual/current/functions/temporal/duration/).

The 53 input/result pairs from frozen Temporal2 and the 11 duration-rendering
pairs from Temporal6 are covered at this internal layer. Their original native
queries remain mandatory and unqualified. Public map/selection overloads,
clock overloads, accessors, arithmetic/truncation/between operations, query type
integration and durable storage are still required.

### Internal map construction and selection

`domain/temporal_components.py` adds `build_date`, `build_localtime`, `build_time`,
`build_localdatetime`, `build_datetime`, `build_duration`, `datetime_from_epoch`
and `datetime_from_epoch_millis`. These remain internal native-value functions;
they do not enable query constructors, public parameters or new persistent tags.

| Builder | Component / source contract |
| --- | --- |
| Date | `year`, calendar `month`/`day`, ISO `week`/`dayOfWeek`, `quarter`/`dayOfQuarter`, or `ordinalDay`; `date` selects a native date or the date part of a local/zoned datetime. |
| Local time | `hour`, `minute`, `second`, `millisecond`, `microsecond`, `nanosecond`; `time` selects the clock part of a local/offset time or local/zoned datetime. |
| Local datetime | Calendar and clock fields plus separate `date`/`time` selections, or one `datetime` source. Combined and separate sources cannot conflict. |
| Offset time | Clock fields/selection plus optional string `timezone`. A selected zoned value supplies its offset; a selected local value does not invent one. Named resolution requires the explicit resolver and an available reference date. |
| Zoned datetime | Local datetime fields/selections plus string `timezone`, or exclusive `epochSeconds` with optional `nanosecond`, or `epochMillis`. Epoch input cannot be mixed with calendar/clock sources. |
| Duration | `years`, `months`, `weeks`, `days`, `hours`, `minutes`, `seconds`, `milliseconds`, `microseconds`, `nanoseconds`; finite exact Python int/float inputs, never bool, NaN, infinity or host-coerced numbers. |

Recognized field names are case-insensitive ASCII, bounded to 12 characters; empty
maps, unknown fields and normalized duplicates refuse. Required leading calendar
and clock components cannot be skipped when constructing without a selected
source. A selected source supplies its existing components before explicit
overrides. Mixed calendar families refuse. Date selection preserves the ISO week
year when it differs from the calendar year, and clamps an inherited day when a
year/month/quarter change shortens its month; an explicitly invalid day refuses.

Subsecond components compose by significance. Milliseconds have a 0–999 range;
microseconds alone may span 0–999,999 and nanoseconds alone 0–999,999,999. When a
larger subsecond component is also supplied, each smaller field is bounded to its
remaining portion (all three together therefore use 0–999 each). No excess is
silently carried into the next second. Plain clock construction requires `second`
before subsecond fields. Epoch helpers use integer arithmetic, including negative
milliseconds, without float timestamp conversion.

Assigning a zone to a local value preserves its local clock. Overriding the zone
of a zoned selection preserves its instant after component edits; composing a
new date with a named-zone time source resolves that zone on the new date first.
A date-only source never transfers its zone to a separately selected local time.
Unchanged zoned datetime selection retains the original value/recorded offset
without consulting current timezone rules. This prevents silent reinterpretation
of an unchanged stored representation after rules-data updates.

`default_offset=0` is explicit and validated for the internal zoned builders, not
a new installed connection setting. Duration maps permit fractions in multiple
fields; finite float inputs use their shortest decimal spelling for rational
composition, while integer inputs retain exactness. They share the documented
calendar/elapsed separation and final sub-nanosecond truncation policy of the text
layer. Current-time/timezone-only map overloads still require the upcoming query
clock context; no ambient clock is introduced by these builders.
[Reference component and selection semantics](https://neo4j.com/docs/cypher-manual/current/functions/temporal/).

### Internal temporal clock scopes

The exact `TemporalInstant(seconds, nanosecond)` record and `TemporalClock.now()`
port are separate from the existing `Clock.wall()`/`Clock.monotonic()` contract.
`SystemTemporalClock` obtains integer nanoseconds directly from `time.time_ns()`;
it does not reconstruct them from floating-point seconds. Invalid clock values
refuse, and an OS read failure is reported as a typed unavailable operation.
Nanosecond representation does not promise one-nanosecond physical resolution.
[Python integer wall-clock source](https://docs.python.org/3/library/time.html#time.time_ns).

`domain/temporal_runtime.py` implements immutable capture contexts:

| Internal operation | Capture / reuse rule |
| --- | --- |
| `TemporalTransactionContext.begin(clock, resolver=None, timezone="Z")` | Validate timezone configuration and capture one transaction instant. |
| `transaction.begin_statement()` | Capture one fresh statement instant while retaining the transaction instant. Existing statement contexts are unchanged. |
| `statement.instant(mode="statement")` | Reuse the statement or transaction capture; `realtime` reads a new instant on every invocation. |
| `statement.current(kind, mode="statement", timezone=None)` | Resolve the selected instant in the explicit timezone, then extract date/local time/offset time/local datetime/zoned datetime. A per-call timezone override does not mutate context defaults. |

Wall-clock adjustments may move time backwards; contexts do not clamp, reorder or
replace captured instants. Captured modes do not reread the source for each row or
reader. Shared clock providers must permit concurrent reads. Zone conversion must
return a native value with the same instant/nanoseconds and requested zone identity;
violations refuse rather than silently changing a captured time. None of this
clock data is used for leases, deadlines, snapshot/OCC ordering or WAL authority.
[Reference clock scopes](https://neo4j.com/docs/cypher-manual/current/functions/temporal/#functions-temporal-instant-types).

**Lifecycle integration:** the composition root now supplies a dedicated wall-clock
source and package-backed resolver. TransactionManager captures the native temporal
context at successful begin; QueryEngine captures one statement context for normal
and cursor execution. `date.statement()` and the corresponding clock overloads,
timezone-only maps and conservative planner volatility rules are enabled. The
default timezone is explicit UTC. A connection timezone setting and the remaining
full-suite temporal qualification are still open. No lease/deadline/OCC/WAL clock
is replaced by this query clock.

### Internal accessors and comparison

`domain.temporal_access.temporal_field(value, field)` reads native immutable
values without clocks or rule-provider lookups. Field names are case-insensitive
ASCII strings of 1–32 characters. Missing/other-family fields refuse with
`temporal_field_unavailable`; invalid names use `temporal_field_name`. No host
attribute name supplied by the caller is evaluated directly.

| Family/component group | Fields |
| --- | --- |
| Date, local/zoned datetime calendar | `year`, `quarter`, `month`, `week`, `weekYear`, `day`, `ordinalDay`, `weekDay` / `dayOfWeek`, `dayOfQuarter` |
| Local/offset time, local/zoned datetime clock | `hour`, `minute`, `second`, `millisecond`, `microsecond`, `nanosecond` |
| Offset time and zoned datetime | `timezone`, `offset`, `offsetMinutes`, `offsetSeconds` |
| Zoned datetime epoch | `epochSeconds`, `epochMillis` |
| Duration whole component groups | `years`, `quarters`, `months`, `weeks`, `days`, `hours`, `minutes`, `seconds`, `milliseconds`, `microseconds`, `nanoseconds` |
| Duration remainders | `quartersOfYear`, `monthsOfQuarter`, `monthsOfYear`, `daysOfWeek`, `minutesOfHour`, `secondsOfMinute`, `millisecondsOfSecond`, `microsecondsOfSecond`, `nanosecondsOfSecond` |

Zone fields expose the recorded name/offset, not today's interpretation of that
name. Offset-only UTC renders `Z`. Negative offset minutes and duration integral
groups truncate toward zero; signed group remainders retain their sign. Duration
subsecond remainders use the normalized nonnegative nanos. Month/day/second groups
stay independent: duration hours do not include calendar days. Total subsecond
fields use normalized seconds plus nanos, so negative fractional quantities retain
the normalization's floor semantics at the requested precision.
[Duration accessor reference](https://raw.githubusercontent.com/neo4j/neo4j/5.26.0/community/values/src/main/java/org/neo4j/values/storable/DurationFields.java).

Epoch seconds expose the exact POSIX second coordinate; epoch millis floor to
millisecond precision, including pre-epoch instants. All integer field results
must fit int64. An expanded datetime may have a valid `year`/`epochSeconds` but an
out-of-range `epochMillis`; reading the latter refuses rather than wraps. The same
applies to duration total milliseconds/microseconds/nanoseconds. Reading one field
does not evaluate or overflow an unrelated field. Existing storage admission is
unchanged; these internal helpers do not implement query property dispatch yet.

`domain.temporal_comparison` exposes two distinct internal contracts:

| Function | Contract |
| --- | --- |
| `temporal_order_key(value)` | Total order among native temporal families: zoned datetime, local datetime, date, offset time, local time, duration. Not yet integrated with the executor's mixed-type order or spill keys. |
| `temporal_predicate(left, right, operator)` | `=`, `<>`, `<`, `<=`, `>`, `>=` for native/NULL operands. NULL propagates; different families are unequal but relationally incomparable. Invalid host operands/operators refuse. |

Instant-family order compares their exact coordinate. Zoned values break ties by
offset, then fixed-offset before named zones, then the stored zone name. Zone
aliases are not silently rewritten. Offset time comparison does not wrap its UTC
coordinate at midnight. Duration order uses average seconds (2,629,746 per month,
86,400 per day), nanos, then month/day/second components to break ties. Wide
internal integer keys avoid arithmetic overflow; they are not exposed as int64
field values or persisted as a new format.
[Reference type ordering](https://raw.githubusercontent.com/neo4j/neo4j/5.26.0/community/values/src/main/java/org/neo4j/values/storable/ValueGroup.java).

Duration equality compares components, not average length. `<`/`>` are undefined
even for equal durations. `<=`/`>=` are true for equal durations and undefined for
unequal durations; ORDER BY's average-length key must not leak into these filters.
[Reference predicate semantics](https://raw.githubusercontent.com/neo4j/neo4j/5.26.0/community/values/src/main/java/org/neo4j/values/utils/ValueBooleanLogic.java).
Native query/property/operator typing, NULL wiring, grouping/DISTINCT, spill,
index comparisons and stored temporal values remain required before qualification.

### Internal temporal arithmetic

`domain.temporal_arithmetic` implements pure component operations, still separate
from query operator dispatch and stored-value admission:

| Internal function | Contract |
| --- | --- |
| `add_duration(value, duration, resolver=None)` | Add a native duration to one of the six native temporal families; preserve the input family. |
| `subtract_duration(value, duration, resolver=None)` | Subtract months, days and elapsed time in the same order as addition. |
| `scale_duration(duration, number, divide=False)` | Multiply by a finite exact Python int/float, or divide when explicitly requested. Integers must fit int64; booleans and host numeric coercions refuse. |

Months shift the calendar and clamp the day to the destination month's end.
Calendar days follow months, and elapsed seconds/nanos follow both. For named
datetimes, each calendar step resolves its own gap/overlap: retain the previous
offset when valid, otherwise use the explicit provider's earlier-overlap or
forward-gap policy. Elapsed operations preserve the instant while applying zone
rules. Consequently one calendar day differs from 24 elapsed hours across DST,
and separate month operations are not generally associative or invertible.
Zero duration preserves an existing zoned value without reinterpreting its
recorded offset; nonzero named operations require an injected rule provider.

For dates, the pinned reference uses calendar days **plus whole days from the
duration's normalized seconds**, with integer division truncated toward zero;
remaining clock components are discarded. This detail is required by Temporal8
case [1]'s fractional-map example and is more precise than the manual's shorthand
about ignoring hours. Offset/local times ignore months and calendar days and
wrap elapsed time at midnight, retaining the offset for an offset time.
[Date reference](https://raw.githubusercontent.com/neo4j/neo4j/5.26.0/community/values/src/main/java/org/neo4j/values/storable/DateValue.java),
[duration total-day reference](https://raw.githubusercontent.com/neo4j/neo4j/5.26.0/community/values/src/main/java/org/neo4j/values/storable/DurationValue.java).

Duration addition/subtraction retains independent month/day/elapsed components.
Scaling uses exact rational arithmetic; fractional months use 2,629,746 seconds
per average month, fractional days use 86,400 seconds, and subnanosecond residues
truncate toward zero. The integer/nanosecond result is normalized before final
bounds validation; no float epoch conversion occurs. Division by zero, nonfinite
factors and out-of-range results refuse. Expression-level NaN support does not
make NaN a valid duration component or scale factor.
[Operator reference](https://neo4j.com/docs/cypher-manual/current/expressions/temporal-operators/).

The component APIs do not implement query NULL propagation, symmetric operand
dispatch, planner typing/error phases, temporal comparison, truncation or between
operations. Those integrations and native query/rollback/storage tests remain
required. No persisted tags, runtime configuration or concurrency policy changed
in this increment; original Temporal8 remains open for native qualification.

The reference supports separate calendar/local/zoned value families and expanded
years from -999,999,999 to +999,999,999. Grafx follows that calendar range for the
new foundation rather than silently clipping it to the host library. Named zones
and stored offsets require the explicit policy below.
[Reference temporal model](https://neo4j.com/docs/cypher-manual/current/values-and-types/temporal/).

### Internal truncation

`domain.temporal_truncation.truncate_temporal(kind, unit, value, fields=None,
resolver=None, reference_date=None, default_offset=0)` implements truncation of
an **explicit native source**. Kind/unit selectors are case-insensitive bounded
ASCII names. This internal function does not capture a clock when a source is
missing and is not yet dispatched by public `*.truncate()` query overloads.

| Target kind | Units | Source families |
| --- | --- | --- |
| `date` | `millennium`, `century`, `decade`, `year`, `weekYear`, `quarter`, `month`, `week`, `day` | Date, local/zoned datetime |
| `datetime`, `localdatetime` | All date units plus `hour`, `minute`, `second`, `millisecond`, `microsecond` | Date for calendar units; local/zoned datetime for all units |
| `time`, `localtime` | `day`, `hour`, `minute`, `second`, `millisecond`, `microsecond` | Local/offset time, local/zoned datetime |

Calendar truncation clears the smaller date fields and, for datetime targets,
uses midnight. Weeks start Monday; `weekYear` selects Monday of ISO week 1 of
the source's week-year, which can differ from its calendar year. Signed negative
year grouping for decade/century/millennium follows the pinned reference's
division toward zero (e.g. year -1984 truncated to century yields -1900); it must
not be described as universally rounding to a preceding instant.
[Signed year-group reference](https://raw.githubusercontent.com/neo4j/neo4j/5.26.0/community/values/src/main/java/org/neo4j/values/storable/Neo4JTemporalField.java).

Optional fields must be a native dict/read-only mapping of supported, strictly
smaller components, except `timezone` on zoned targets. Unknown, duplicate after
normalization, same/larger-unit or source-selection fields refuse. Numeric values
retain constructor bounds. Overrides are applied after truncation; at millisecond
or microsecond precision, smaller subsecond fields fill the remaining precision
without replacing the retained prefix. No fractional component silently carries
into a larger one. Invalid selectors/map shape/configuration refuse before zone
provider calls; all operations leave the source and supplied map unchanged.
[Truncation function reference](https://neo4j.com/docs/cypher-manual/current/functions/temporal/).

Named datetime boundaries require the explicit provider and are resolved anew
(earlier overlap, forward gap), rather than preserving an old overlap offset as
duration arithmetic can. A timezone override preserves the truncated local clock,
not the instant. Offset-time results retain only an offset; named overrides need
a reference date, explicitly provided or taken from a zoned datetime source, and
keep the truncated clock even when reference-date resolution crosses a gap.
Unzoned sources use the explicit fixed default offset. No ambient machine date,
timezone or clock is read. Missing named-zone rules/context refuse without a UTC
fallback. Clock/default-zone query integration, original native query execution
and temporal stored-value admission remain open.

### Internal differences between instants

`domain.temporal_between.temporal_between(left, right, mode="between",
resolver=None)` accepts native date/local/offset-time/local/zoned-datetime values
and NULL. Supported case-insensitive modes are `between`, `inMonths`, `inDays`
and `inSeconds`; invalid modes/host operands refuse. NULL operands propagate
after input validation. No clock or default timezone is read by the operation.

| Mode | Native result semantics |
| --- | --- |
| `between` | Complete calendar months, then complete calendar days, then exact seconds/nanos remaining after those shifts. Calendar splitting occurs only when both original operands have dates. |
| `inMonths` | Complete calendar months; discard smaller residual units. |
| `inDays` | Complete calendar days; discard the residual clock interval. |
| `inSeconds` | Exact elapsed seconds and nanoseconds, with no month/day splitting or floating-point epochs. |

A missing time uses midnight. When only one operand has a date, its date supplies
the other's missing calendar coordinate. When only one has a zone, that zone is
bound to the other's local fields. Named binding resolves using the explicit
provider at the borrowed value's own date/time, so crossing a DST transition does
not reuse a constant offset incorrectly. Two dateless values remain dateless:
`between`/`inSeconds` work, while `inMonths`/`inDays` refuse rather than invent a
calendar date. Offset-time differences preserve UTC day displacement without
midnight wrapping. [Reference difference functions](https://neo4j.com/docs/cypher-manual/current/functions/temporal/duration/).

Complete calendar units are measured on the starting operand's local timeline;
a zoned target is converted to that zone when necessary. Clock/nanosecond
comparison determines whether a day boundary is complete. Complete months do
not assume that clamped month addition is invertible: January 31 to February 29
can be 29 days rather than one complete month. `between` applies its month and day
shifts to the original source before recomputing each remainder, retaining the
same calendar-versus-elapsed rules as duration arithmetic. Arithmetic is bounded
by the native calendar and signed component ranges, not the platform datetime
year range; seconds/nanos normalize exactly even for reversed fractional spans.

Already-zoned elapsed operands use their recorded instant coordinates without
refreshing rules. Named binding, cross-zone calendar conversion or nonzero named
calendar shifts require a provider. Missing/failed rules propagate; no UTC or
machine-zone fallback is introduced. These component functions neither register
public query overloads nor enable durable tags, parameter/spill/index handling,
or transaction/statement clock lifecycle. Those integrations remain required.

### V1 binary frames and pending durable admission

`domain.model.temporal_codec` defines the temporal wire contract, now integrated
with the general `ValueType` registry, tuple encoder, native query parameters and
transactional persisted writes. All existing value tags
0–11 retain their interpretation. The following tags and the capability name
`temporal_values_v1` / catalog bit 23 define the format fence. The catalog now
recognizes that fence and has an internal transactional metadata activation path;
native temporal storage is enabled; full temporal query/transport qualification
remains pending.

All fields use little endian; signed integer fields are two's complement. Each
frame starts with its one-byte tag:

| Reserved tag | Family | Body | Total bytes |
| --- | --- | --- | --- |
| 12 | Date | `epoch_day:i64` | 9 |
| 13 | Local time | `nanoseconds_of_day:u64` | 9 |
| 14 | Offset time | `nanoseconds_of_day:u64`, `offset_seconds:i32` | 13 |
| 15 | Local datetime | `epoch_day:i64`, `nanoseconds_of_day:u64` | 17 |
| 16 | Zoned datetime | `epoch_seconds:i64`, `nanosecond:u32`, `offset_seconds:i32`, `zone_length:u8`, ASCII zone bytes | 18–273 |
| 17 | Duration | `months:i64`, `days:i64`, `seconds:i64`, `nanosecond:u32` | 29 |

Zero zone length means offset-only datetime; otherwise the existing bounded
logical-zone-key syntax applies. Decode preserves the recorded instant, offset
and key even when current timezone data does not know that key. It never performs
a rule lookup or derives a new offset. Constructor/binding authority and future
write admission must prove appropriate zone provenance, not the binary reader.
The existing `TIMESTAMP` tag remains microsecond UTC, not a substitute for these
families.

Internal `encode_temporal_value(value)` produces one frame and revalidates exact
native components before binary packing. `decode_temporal_value(raw, offset=0)`
accepts exact immutable bytes and an exact integer cursor, returning `(value,
next_offset)` so enclosing tuple/list/map decoders can parse siblings. Unknown
tags, truncated bodies/names, invalid calendar/offset ranges, non-ASCII/bad zone
keys and noncanonical nanos refuse with `GrafxCorruptionDetected`. Nanos in stored
datetime/duration frames must already be within 0–999,999,999; corruption must not
be normalized into a different valid value. Maximum frame size is 273 bytes.
Enclosing page/record checksums provide integrity protection; a valid changed
payload is not detectable from a type frame alone.

**Encoding alone is not durable admission.** General value and tuple encoding/
decoding now support these native tags, including nested ANY/list/map values.
Typed temporal column creation/append records the capability in the staged schema.
The first temporal value written to an existing ANY/nested column merges the fence
into that transaction's schema before the first OCC pass; no second transaction is
created. Readers without bit-23 support reject the catalog before applying pages.
Legacy catalog migration remains explicit. Wider index/spill/history/transfer and
accelerated/interop qualification remain required; native row evidence is not
evidence that every consumer already supports the format.

### Internal transactional temporal metadata fence

`TransactionManager._prepare_temporal_values_activation(context)` is an internal
integration primitive, **not a public maintenance API or automatic first-write
admission**. It requires a fresh, dedicated write transaction and a persistent
v2 catalog. It does not implicitly migrate a legacy catalog. The detached catalog
copy adds `temporal_values_v1`; staging does not publish that copy to other readers.
The normal transaction commit publishes catalog pages through the existing
WAL/durability barrier and OCC checks. A concurrent schema winner makes an older
prepared activation conflict, rather than letting it overwrite the winner.

The prepared page set must remain exact: later row intents, pending index records
or schema-page replacement refuse the dedicated commit. Partial staging restores
pages, proofs, partitions and staging marks, and does not retain a new maintenance
registration. An already-active capability is a no-write/no-registration no-op;
rollback and terminal transaction cleanup discard the plan. A pre-append failure
can be retried in the same active transaction. Failure to apply pages after a
durable COMMIT reports the committed outcome; reopen recovers it from WAL rather
than manufacturing another commit. Committed replay is idempotent and uncommitted
page images do not become recovery effects.

This dedicated primitive adds no public maintenance operation. The later native
storage integration uses the first-row preparation below, not a dedicated metadata
transaction. Metadata-only tests remain narrower than native row recovery and
do not establish full query qualification or cross-version interoperability.

### Internal first-value metadata preparation

`domain.temporal_admission.temporal_storage_required(row_values)` walks a native
row's scalar/list/map values, including map keys, with the existing value-depth
bound. All six temporal models are recognized and defensively validated by their
binary encoder, without looking up timezone rules. The scan checks the entire
row, not just a prefix ending at the first temporal value: a later malformed
component, excessive nesting or cycle still refuses. The outer row tuple does not
consume an extra collection depth. Ordinary schema/type/finiteness checks and byte
quotas remain the tuple encoder's responsibility.

`TransactionManager._prepare_temporal_row_admission(context)` provides the
**internal preparation step**, now invoked by commit after validating staged
input provenance and before acquiring the write lease/first OCC interest freeze.
It reduces INSERT/UPDATE/DELETE intents before scanning: an insert subsequently
deleted does not activate the format, and an updated insert uses its surviving
values. Transaction-local relationship endpoint references are handled by the
existing endpoint planner, not mistaken for temporal properties. DELETE's empty
tuple is never encoded or scanned as a replacement row.

When needed, it combines bit 23 with the transaction's already-staged catalog,
or with the durable catalog if no schema was staged. Exact page-image provenance
is required before decoding/merging staged schema. The candidate preserves the
statement's table changes and carries ordinary page staging proofs and original-
snapshot OCC interests. It neither opens a second transaction nor marks the user
transaction as maintenance. Failed staging restores the preceding schema pages,
proofs and partition interests; rollback publishes neither the fence nor the new
schema. A competing schema commit is not forgiven by a newer physical baseline.
Legacy catalogs refuse rather than triggering an implicit index migration.

An already-active durable capability with no staged catalog uses a no-scan/no-schema-
lock fast path. Staged schema still requires inspection so an older schema image
cannot lose a newly added capability. The preparation preserves staged schema
across pre-WAL retry. Its earlier isolated tests are historical preparatory
evidence; the native storage receipt additionally exercises actual parameter/row
commit, reopen, original reader snapshots and durable-fault recovery. Typed DDL
and general codecs are now enabled; full query operators and transport integration
remain pending.

## Frozen original-case matrix

Source: pinned openCypher 2024.3 revision
`677cbafabb8c3c5eed458fd3b1ec0daec8d67d23`, unchanged V2/V1 inventory. Counts are
expanded cases, not independent missing features or projected gains. Prefixes
are relative to `tck/features/expressions/temporal/`.

| Original family | Cases | Required capabilities | Original native query status |
| --- | ---: | --- | --- |
| Temporal1 | 207 | Map components, calendar/week dates, fractional time units, defaults, offsets/named zones, epoch construction, durations | 207 passed |
| Temporal2 | 53 | String constructors for all six families; compact/expanded forms, week/ordinal/quarter dates, fractional durations | 53 passed |
| Temporal3 | 183 | Selection/composition from temporal values, overriding components, zone conversion and consistency | 183 passed |
| Temporal4 | 39 | Node property storage and arrays for all six families, NULL; separate API commit/reopen coverage | 39 passed |
| Temporal5 | 7 | Calendar, clock, zone and duration accessors | 7 passed |
| Temporal6 | 17 | Canonical toString and constructor round-trip, named/offset zones | 17 passed |
| Temporal7 | 18 | Equality and ordered comparisons; duration equality | 18 passed |
| Temporal8 | 27 | Temporal ± duration; duration ± duration; numeric duration scaling/division | 27 passed |
| Temporal9 | 322 | Date/time truncation units with component overrides and zones | 322 passed |
| Temporal10 | 131 | between/inMonths/inDays/inSeconds, signed fractions, DST boundaries, large durations and NULL | 131 passed |
| **Total** | **1,004** | Native temporal family, not string-returning substitutes | **1,004 passed; full FP-5 still open** |

Temporal usages elsewhere in the profile, including ordering fixtures in FP-2,
remain additional mandatory coverage. No case is moved out of FP-2 to claim its
closure. Each family must run its original query, fixtures, results and effects.

## Ordered integration work

1. **Value semantics:** complete the six internal models with full component
   and textual constructors, precision/range checks and canonical serialization.
   Keep calendar months/days distinct from elapsed seconds; define fractional
   component conversion and rounding explicitly. Complete boundary and negative
   tests, including unsupported host values that must not invoke callbacks.
2. **Clock and zone provider:** use a statement-captured clock by default, with
   explicit query clock variants and an injectable test source. Default timezone
   is explicit UTC, never inferred from the machine. Plan a documented connection
   timezone setting; no new setting is exposed by the current foundation.
   Resolve named zones through an infrastructure provider, not a domain singleton.
   Version/pin the data used for repeatable tests, validate offset/name agreement,
   and freeze explicit overlap/gap behavior. Store enough information to preserve
   the instant and prevent silent reinterpretation after timezone-data changes.
3. **Query integration:** native scalar registry and dedicated dotted temporal
   function names, type inference, parameters, property access, expressions,
   comparison, equality/hash, grouping/DISTINCT, sorting/spill, aggregates and
   toString. Implement all matrix operations, not just the constructors exercised
   by the 65 historical FP-2 temporal blockers. Do not route dotted names to
   unrestricted CALL/UDF execution.
4. **Durable values:** allocate/version new tags and catalog capability before
   admitting writes; add schema names, typed columns, flexible ANY/nested values,
   pure and accelerated codecs, size validation and malformed-byte refusals.
   Old binaries must fail before mutation. Define supported identity/exact/ordered
   index combinations and refuse unsupported definitions without hidden coercion.
5. **Lifecycle and transport:** commit/OCC, WAL replay, crash/recovery, verification,
   retained-history codecs/schema recovery, catalog copy, logical transfer and
   resume, JSON/CLI and supported Arrow/Pandas/Polars/Parquet routes. Preserve
   nanoseconds, zone identity and calendar duration components in round-trips.
6. **Qualification and consumption:** execute all original families plus cross-
   family temporal fixtures; run grouped query/storage/transaction regressions and
   isolated Pulse consumers. Document every public constructor/type, parameter,
   result DTO, setting, error and transport mapping. Pulse Core remains generic;
   engine-specific conversions stay in Community adapters.

The original FP-5 exit remains atomic query **and** stored-value support. These
integration steps are implementation order, not permission to mark a string-only,
query-only or single-date subset as the completed feature.

## Zone, ordering and host-library constraints

Temporal ordering must distinguish comparison operators from heterogeneous
ORDER BY and reproduce tie-breaking for offsets/named zones. Duration ordering
also needs an explicit reference-compatible key, not lexicographic dataclass
field ordering or arbitrary conversion of calendar months to a fixed day count.
[Reference ordering](https://neo4j.com/docs/cypher-manual/current/values-and-types/ordering-equality-comparison/).

Python `zoneinfo` obtains rules from system data or the `tzdata` package, and
Windows deployments often need that package. The internal `TemporalZoneResolver`
port and `ZoneInfoTemporalResolver` adapter now load exclusively from the declared
`tzdata>=2024.1` package, not system TZPATH. Each
resolver identifies its package/IANA version and caps its synchronized rule cache
at 128 entries. Missing rules must refuse
explicitly; silently using UTC for an unknown named zone is prohibited.
[Python zoneinfo data sources](https://docs.python.org/3/library/zoneinfo.html#data-sources).

The dependency is declared in the development package; no Pulse installation,
release or ambient-timezone setting is changed by this increment. The tests below
used `tzdata:2026.3;iana:2026c`; this identifies the evidence, not a permanently
frozen runtime dependency. Current local resolution selects the earlier instant
in overlaps; an explicit offset must match a valid candidate. A gap without an
explicit offset shifts forward by the actual transition length (including
half-hour and full-day gaps); an explicit invalid offset refuses. Instant
conversion preserves the original nanoseconds. Historical offsets retain seconds.

Malformed/truncated rule files raise `GrafxCorruptionDetected`; unreadable data
raises `GrafxUnsupportedOperation`; invalid/unknown names or offset disagreement
raise `SchemaMismatchError` with structured reasons. A failed load is not cached.
None of these failures permits a silent UTC fallback.

The expanded-year limitation of the initial bridge is now closed. A bounded TZif
reader checks headers, counts, lengths, transition ordering, type indices,
designations and footer framing. It reads at most 256 KiB plus one overflow-detection
byte per rule file. Recorded transitions use exact integer instants and binary
lookup. Only the POSIX annual footer is evaluated through public
`ZoneInfo.from_file`, in an isolated rule object with no historical transitions.
Its Gregorian 400-year cycle is used solely to obtain the recurring offset; the
native calendar date, instant and nanoseconds are never replaced by a surrogate.
Historical rules are never extrapolated from a modern surrogate year.
[TZif format and annual footer](https://www.rfc-editor.org/rfc/rfc9636.html#section-3).

Local resolution checks the finite set of offsets in the rules (at most 256 type
records plus two footer offsets), validates each candidate against its actual
instant and handles overlaps/gaps without scanning calendar years. UTC instants
just outside the native local-date boundary remain valid when their resolved
local representation fits; no intermediate host datetime clips them.

Explicit `-00` unspecified intervals in the source data refuse with
`field=temporal_timezone_interval`, without disabling the zone's other known
intervals. Leap-adjusted rule files, oversized files and offsets outside the
native ±18-hour range refuse explicitly; none is interpreted as ordinary POSIX
time or silently clipped. The package-backed POSIX dataset is the supported
source, not arbitrary uploaded timezone files.

**Still required:** query clocks, timezone configuration, full temporal operations,
storage activation and all other integrations in the matrix remain open. Closing
the internal provider range gap does not qualify FP-5 query/storage support.

## Current evidence

Receipts below are chronological. Earlier statements that general encoding or
public parameters were closed describe the preparatory revision tested then;
the current native row contract is [documented here](../TEMPORAL_VALUES.md).
Native query/TCK qualification remains separate from these storage receipts.

`fp5-temporal-primitives.xml`: **36 passed**, zero failures/errors/skips, 0.383 s;
SHA-256 `9a398a21914d2e93436578047aff847de20a3d440adbdca3a05a5cedd0974ec9`.
The calendar test independently compares 5,000 deterministic samples with Python's
date implementation inside its supported range; separate 400-year-cycle and
round-trip tests cover negative/zero/expanded/boundary years. Additional tests
cover week/ordinal construction, month-end clamping, exact nanosecond formatting,
duration sign normalization, bounds, overflow and bool/float refusal.

This is foundation evidence, **not** a passing native temporal family, durable
round-trip proof, public API qualification or Pulse validation. Those receipts
must be added as the corresponding integrations become executable.

The subsequent `fp5-foundation-literals-regression.xml` passes **654 tests** with
zero failures/errors/skips in 8.566 s (SHA-256
`1706076e852db7f09b37c766b96c63d4a74e8435683132bc1b37b2a4c773fb14`).
It includes the primitives, value-type dispatch/codec/schema regressions and the
literal/parser/lexer/error mapper work. Three additional admission tests prove
that the unintegrated temporal primitives are still rejected by `value_type_of`
and `encode_value`, preventing accidental unfenced persistence. No test weakens
current storage admission to make the foundation usable prematurely.

The zoned-foundation regression `fp5-zoned-foundation-regression.xml` passes
**673 tests**, zero failures/errors/skips, 9.848 s; SHA-256
`0f896d1ea9b2cb28143dae9a9490c03ee3f35a2c835f25243f32809c8c7b9771`.
It covers all six internal containers, 1,500 deterministic independent fixed-offset
round-trips, expanded-year fixed-offset boundaries, DST overlap/gap policies,
historical second offsets, bounded/concurrent cache access and injected rule-read
failures, together with selected value/schema/parser/literal/mapper regressions.
All six containers still refuse storage admission. This is a selected regression,
not the final repository or native temporal-family qualification.

The full-range provider continuation passes **1,314 tests**, zero
failures/errors/skips, 12.159 s, in `fp5-expanded-zone-regression-final.xml`;
SHA-256 `9484ccaf8c90d7cc060007a8c7218e4a1bf740d3b44d82df79b80f32313a8678`.
All **598 timezone names** in the recorded package are compared with an independent
stdlib `ZoneInfo` loaded from the complete file, over representative years and
the second before/at/after every recorded transition. Every known offset-changing
transition also exercises local overlap/gap resolution with nanos. Synthetic
tests cover explicit transitions beyond year 9999 (which must not be cycle-folded),
annual Julian/week/extended-hour/all-year rules, expanded/native boundary
round-trips, unspecified intervals, malformed frames/counts/indices and bounded
or leap-adjusted input refusal. Value/schema/parser/mapper regression coverage is
included; native temporal query/storage qualification is still outstanding.

The text/value continuation passes **1,441 tests**, zero failures/errors/skips,
9.972 s, in `fp5-temporal-text-regression-final.xml`; SHA-256
`29df293f98d79bef7e2dc358a85ebfb4563fefa2f623f73f1ab16882f990b203`.
Alongside the 53 Temporal2 and 11 Temporal6 component pairs, 2,000 deterministic
full-range durations and 2,000 calendar/clock/offset samples exercise exact
serialization round-trips. Negative tests cover precision, ordering/duplicate
units, invalid calendars/offsets, discarded suffix/newline prevention, input
budgets, host callback refusal and continued storage-admission rejection. The
grouped run also retains the complete package-zone provider and selected
value/schema/parser/error-mapper regressions. No native query case is marked
passed from these component receipts; the frozen ledger is unchanged.

The component/selection continuation passes **1,552 tests**, zero
failures/errors/skips, 12.564 s, in `fp5-temporal-components-regression.xml`;
SHA-256 `832617ffc39693a0d902679a9dbbfce73d2f66f7cbb7b9c0f65e9766935c9065`.
Coverage includes calendar/clock/zone cross-products, original week-year examples,
duration map quantities, exact epochs, source selection and zone conversion,
unchanged recorded-offset preservation, invalid-component/provider-ordering
checks, 2,000 independent ISO-week comparisons and the earlier text/provider/
value/schema/parser regressions. All six constructed types still refuse durable
admission until catalog/codec integration.

A separate diagnostic evaluation of the pinned example arguments/results found
all 206 map pairs in Temporal1 and all 183 selection pairs in Temporal3 matching
the native component functions; the remaining Temporal1 epoch scenario's two
outputs are covered in unit tests. This diagnostic evaluates constructor inputs,
**not native queries or transaction effects**, and does not update conformance
counts or qualify either original TCK family.

The clock-scope continuation passes **1,594 tests**, zero failures/errors/skips,
13.848 s, in `fp5-temporal-clocks-regression.xml`; SHA-256
`aaeb4d93fa1e7f94db315c5b965e457e79375e7e92a430461b84db3ed83776c3`.
It includes exact system-clock conversion/faults, once-per-scope captures, fresh
realtime reads, backwards wall adjustments, immutable concurrent readers,
timezone extraction across day/DST boundaries, failed capture isolation,
provider-result integrity and continued storage-admission refusal, plus the
earlier component/text/provider/value/schema/parser regressions. This does not
prove actual engine lifecycle wiring or qualify any clock query overload yet.

The arithmetic continuation passes **1,657 tests**, zero failures/errors/skips,
12.543 s, in `fp5-temporal-arithmetic-regression.xml`; SHA-256
`ffb0894f98e1066af80cea58f5172a85f6fafdf2899eb23b1a45a7bb7cac765d`.
All 27 Temporal8 input/result pairs are covered at the component level, plus
1,500 deterministic fixed-offset samples checked against independent stdlib
elapsed-time arithmetic. Negative/edge tests cover signed truncation, duration
component cancellation, precision, bounds, invalid scale factors, month-end
noninvertibility, DST gaps/overlaps, half-hour transitions, a skipped calendar
day, year zero/expanded years, provider instant corruption and continued storage
refusal. The grouped run includes all earlier temporal foundations and selected
value/schema/parser/error-mapper tests. It is not a full repository regression
or native TCK execution receipt; the frozen ledger and query qualification counts
are unchanged.

The field/comparison continuation passes **1,713 tests**, zero failures/errors/skips,
13.169 s, in `fp5-temporal-access-comparison-regression.xml`; SHA-256
`41e65f74c56833063126f43ff52bd7e47facb4fe61e3ed466878e804bb3b32e4`.
Component tests cover all seven original Temporal5 accessor result vectors and
18 Temporal7 comparison pairs. Additional cases validate aliases/case handling,
negative duration groups, stored offsets without rule lookups, pre-epoch precision,
field overflow refusal, duration predicate versus total-order semantics, zone
identity ties, cross-family/NULL operands and host callback refusal. A deterministic
1,500-duration ordering sample checks independent exact total-nanosecond ordering
and equality consistency; selected earlier regressions remain included. This is
not native TCK execution or full repository qualification; property/operator,
spill/index and durable temporal admission remain open.

The truncation continuation passes **1,892 tests**, zero failures/errors/skips,
13.464 s, in `fp5-temporal-truncation-regression.xml`; SHA-256
`95945288c932355d7c40d5c77490cf3cc5d712947a0077eb91f4f983b40a322e`.
The 179 added tests cover target/source/unit cross-products, precision overrides,
ISO week-year boundaries, normalized map fields, same-local zone replacement,
named rule requirements, gaps/overlaps, signed year grouping and storage refusal.
An independent stdlib reference checks week and week-year truncation for 1,500
deterministic calendar samples. A diagnostic evaluation of all **322 original
Temporal9 argument/result pairs** matches, without executing native queries or
changing the frozen ledger. Previous selected temporal/value/schema/parser
regressions remain included; this does not qualify public query overloads,
transaction effects or full repository compatibility.

The difference continuation passes **1,971 tests**, zero failures/errors/skips,
14.405 s, in `fp5-temporal-between-regression.xml`; SHA-256
`94162af5534f59fd93d7a61a468c87ee577b198a9db93e92ab5edac292da72e8`.
Its 79 added tests cover original mixed-family result grids, complete positive/
negative calendar boundaries, subsecond signs, full calendar range, borrowed
dates/zones across DST, NULLs, dateless calendar refusal, rule-provider failures
and continued storage rejection. For 1,500 deterministic pairs, independent
stdlib elapsed differences agree exactly and applying the computed logical
duration reconstructs the target. The diagnostic evaluates all **131 Temporal10
examples**, with zero mismatches, including explicit captured component values
for the five same-clock examples. It does not execute native queries or prove
engine clock lifecycle, transaction effects, spill or durable admission. The
frozen ledger and native qualification counts remain unchanged.

The binary-contract continuation passes **2,125 tests**, zero failures/errors/skips,
14.084 s, in `fp5-temporal-codec-regression.xml`; SHA-256
`68421d8078898dd3e04fe2ab1abb44ec0a190975a48506719454122c551616f8`.
Its 44 new codec/admission tests include independent exact byte vectors, every
truncated prefix of those vectors, sibling cursors, strict corrupt/unknown frame
refusal, full-range fields, forged host component rejection and 9,000 deterministic
frame round-trips plus mutations. Valid mutated frames must reencode canonically;
invalid ones refuse rather than normalize. General value/ANY/nested writes remain
rejected. Catalog and committed-WAL preflight/application paths, simulating a reader
without the new capability, reject it before any page application. Existing catalog/flexible
model/replay and earlier temporal/value/schema/parser regressions are included.
This receipt does not prove activated temporal storage, old-reader interoperability
after activation, transaction rollout or final native temporal qualification.

The internal metadata-activation continuation passes **2,178 tests**, zero
failures/errors/skips, 27.688 s, in
`fp5-temporal-activation-regression.xml`; SHA-256
`b550e0989b213549d9a918fcd47510a9d1f993e9b1e1a1b61d898abf6263b3a3`.
Its 13 new tests cover real disk-backed transaction publication/reopen and ordinary
queries after the fence, no-op/rollback, read/nonfresh/legacy refusal, partial
staging restoration, attempted later schema replacement, competing writer schema,
simulated nonrecognizing reader refusal, retry before WAL append and failure after
the durability barrier with exactly one recovered COMMIT. Native page replay tests
also cover a torn catalog header, idempotence and exclusion of effects without
COMMIT. The grouped run includes the earlier 2,125-test selection plus existing
commit-catalog/commit-state/identity activation regressions. Fault injection and
simulated legacy-reader tests are not an OS process-kill or an old-release binary
interoperability matrix. General temporal storage and original temporal TCK
qualification remain pending; the frozen ledger and native counts are unchanged.

The first-value preparation continuation passes **2,199 tests**, zero
failures/errors/skips, 29.162 s, in
`fp5-temporal-row-admission-regression.xml`; SHA-256
`f820633388204bf08cb941cad947eb90d37dbe391704600e3fbd59d429936d2e`.
Its 21 additional tests cover all six families nested/in map keys, exact depth
boundaries, cycles, forged components and callback avoidance; staged-schema
preservation, complete partial-failure restoration/retry, original-snapshot schema
conflicts, pending endpoint handling, reduced update/delete semantics and refusal
to launder an unproved staged image. The earlier 2,178-test metadata/codec/temporal
and activation regression selection also passes. These tests exercise the internal
preparation primitive, not an enrolled automatic commit hook or successful native
temporal value persistence. Documentation links/configuration/API checks and lint
pass; the native temporal matrix is still open.

The native parameter/storage integration passes **4,121 tests**, zero
failures/errors/skips, 296.426 s, in
`fp5-temporal-native-regression-final.xml`; SHA-256
`5ddbf91c4dee710f97b759cfd406efa37bbe5eab4b797430fce2887dda84764d`.
This run covers all `tests/storage_core`, `tests/txn` and `tests/recovery`, the
temporal and identity activation API suites, and selected parser/schema/ANY/NaN
query regressions. Its 27 new end-to-end temporal cases cover all six types as
typed/ANY parameters, commit/read/reopen/verify, nested values, schema/data merging,
rollback, pre-WAL retry, post-durable-COMMIT recovery, independent old reader
snapshots, the already-active no-schema-lock path, nullable column evolution,
unlabeled nodes/implicit relationships and whole-statement rejection of a later
nested NaN after an earlier temporal row.

The first broader run exposed four real close/commit regressions: the new hook
rechecked the closed latch after a commit had already won the participant section.
Commit-internal admission now preserves that existing winner; standalone
preparation and new public operations still refuse after close. The original
terminal-close cases and an additional threaded first-temporal-update versus
close test pass in the final run, with the temporal value verified after reopen.
Lint, generated API documentation and link/configuration/signature checks pass.
No TCK cases were reclassified or claimed as native temporal query passes. Query
constructors/operators, remaining transport/index/history qualification, isolated
wheel compatibility and Pulse consumption remain part of the unfinished FP-5/FP-8
delivery; no installation, release or production data mutation was performed.

### Native query functions and operators checkpoint

Native query execution now passes **all 1,004 original temporal cases**, zero
failures, with 2,893 outside the selection. Terminal exit 0; frozen V2 and V1
inventories verified, source revision unchanged. Receipt under `.grafx-tmp/`:
`fp5-temporal-operations-native-final.json`, SHA-256
`7756a15d1f8fad310cf47749f91155714ec364569ba31319cf7049e83ef8f723`.
Earlier runs (954/50 and then 998/6) identified missing field, arithmetic and
comparison dispatch and an additional static property guard; the final run
closes those failures without changing fixtures, queries or expected results.

The result adapter renders exact native temporal DTOs into the original TCK's
quoted ISO value notation. It does not rewrite expected literals or make engine
results strings. Public API tests independently assert native result classes,
durable tags, parameter round-trips and reopened values; arbitrary host
`isoformat()` methods are not invoked by the adapter.

The native transaction begin captures a transaction wall instant; statement
execution captures its own instant, retained across cursor fetches. REALTIME
evaluates per call. Package-backed named-zone resolution is explicitly injected;
default query timezone is UTC, no new connection option. Lease/OCC/WAL clocks
and close/commit ownership are unchanged. All temporal calls are conservatively
nonconstant to avoid plan-time clock evaluation/reuse.

Property typing does not evaluate function/CASE subjects as probes. Fields,
arithmetic and relational predicates invoke the previously tested domain
components; ORDER BY uses a separate total key. External sort/grouping retains
native payloads and wide internal comparison coordinates. Missing fields,
overflow and invalid arithmetic refuse at actual evaluation; late write failures
roll back the statement, preserving other statements in the transaction.

| Receipt under `.grafx-tmp/` | Result | SHA-256 |
| --- | --- | --- |
| `fp5-temporal-functions-lifecycle.xml` | 135 passed, zero failures/errors/skips; 4.321 s | `e0baab9aa8a7cc2c0b4a3ee4103ac578217ffcfc8baf81ad2ecf9b47afe576c0` |
| `fp5-temporal-operations-query-regression.xml` | 606 passed, zero failures/errors/skips; 41.485 s | `5bd66cf64226268caa85978ddf1cabd04689b0a1ff2fce27251bdd3037a1794a` |
| `fp5-temporal-query-operations-final.xml` | 45 passed, zero failures/errors/skips; 3.397 s | `059af8a68b2828b04f1fe9f330fd98505d317b1b88fa353b54326371e5d857a3` |

The last receipt includes independent explicit order expectations, temporal
parameters/aliases/CASE/list fields, all arithmetic directions, invalid pairs,
duration NaN/zero refusal, spring-DST calendar versus elapsed arithmetic,
statement rollback/reopen and forced external spill of extreme-year/large-duration
values through sort, DISTINCT, grouping and min/max. These focused receipts
overlap; do not add them to claim unique coverage or full-repository regression.
Complete index/history/copy/transport, isolated old-wheel and Pulse qualification
remain required before FP-5/FP-8 acceptance.

### Whole-entity temporal result integration

Native scalar RETURN support did not yet qualify whole entities: the first
post-temporal FP-2 run reached 1,926 passes/50 failures, all remaining failures
refusing native temporal entity properties during result materialization.
The owned entity grammar now validates/detaches all six native values with the
same codec and preserves them in node/relationship/path/nested/cursor results.
Explicit JSON tags retain wide coordinates as decimal strings, subsecond precision,
recorded offsets/zones and independent duration components; no zone-rule lookup
occurs during serialization. [Public grammar](../ENTITY_VALUES.md#json-grammar).

The 14-test native entity receipt covers all six families as direct and nested
properties, pending/committed observations, scalar/entity agreement, path
components, cursor reads, restart, JSON tags, forged-value refusal and independent
ownership even of nested frozen date objects. Receipt
`fp5-temporal-entity-results.xml`: 14 passed, zero failures/errors/skips, 4.271 s;
SHA-256 `6b4d1b6cc09b019ce3d54421dce182a47924a5e18c6d05d9174322f4d69ab159`.
The adjacent all-NULL entity-inference correction and validation of static versus
dynamic error phases are recorded in [FP-3 evidence](../conformance/FP3_PROGRESS.md#temporal-observations-and-all-null-collection-correction).

The complete FP-2 owner rerun then passes **1,976/1,976 required cases**, with
zero failures and 1,921 outside selection; terminal exit 0. Receipt
`fp2-post-temporal-entities-native.json`; SHA-256
`1fedb130068d2f84d8dbaf12bc3970d9738453c1263a4da1ea5aaaeb51c6b693`.
It closes all 65 previous temporal dependencies under the original source and
ledger expectations, independently of the 1,004-case temporal-family receipt.

Corrective entity/path/null coverage: `fp5-temporal-entity-corrective-regression.xml`,
251 passed, zero failures/errors/skips, 86.896 s; SHA-256
`2f4bbc4b79bff41c9d3c42a39bf1a07d880e966b3cce98f1c11a78e83d24bcf8`.
Three older WITH tests were also updated to assert the already-implemented exact
`no_expression_alias`/`column_name_conflict` reason and planning phase rather than
requiring absence of those fields. No engine behavior was weakened. The related
`fp5-adjacent-with-regression.xml` passes 92 tests, zero failures/errors/skips,
18.814 s; SHA-256
`2497624cebc3c88425123601ba2715b84e4889ca64be7f6232a87e720e2f2ecd`.

The expanded regression exposed a correction boundary: removing all list-kind
proofs for empty lists also removed valid zero-edge ranges and CASE branches with
an empty alternative. Empty collections now carry a neutral structural proof,
not a node/relationship element kind. It preserves zero-edge range use, compatible
entity-list CASE/coalesce/concatenation and aliases/subquery imports, while UNWIND
does not invent an element entity type. All-NULL lists remain separate.
`fp5-empty-list-corrective-regression.xml`: 156 passed, zero failures/errors/skips,
75.791 s; SHA-256
`f63e8259d64043c43c5565c49815b28819be2bc41991176e27c6602621abedf8`.
This focused correction is not substituted for the final grouped regression.

#### Grouped regression trace

The initial broad run selected all query/tools tests, all storage-core temporal
tests, timezone provider tests and the three native temporal API suites. It ran
6,789 tests with eight failures (the five stale phase/detail assertions and three
all-NULL path-function cases described above), zero errors/skips, 1,250.726 s.
Receipt `fp5-temporal-query-tools-regression-final.xml`; despite its early chosen
filename it is a **failed preparatory run**, not final acceptance. SHA-256
`c91e7c304b2107be0a5f59523ab476a122835d4e9692a1041fb1e709cfdcea83`.

The next grouped pass included the 14 entity-temporal tests and three NULL tests:
6,806 tests across three disjoint groups, with two remaining empty-list regressions
in group 1 (groups 0 and 2 passed). Those failures drove the neutral empty-list
proof above. The final rerun uses the same 191-file manifest, without excluding
files/cases, and includes six additional empty-list tests. Manifest
`fp5-temporal-final-shard-selection.json`; SHA-256
`314d1942c1abf652a62d50b8b4e35c3e396d94f89c3417e440c9b14764071e1a`.
Separate receipt prefixes distinguish preparatory `fp5-temporal-final-shard-*`
from the corrected `fp5-temporal-final-v2-shard-*` execution.

The corrected grouped regression is complete: **6,812 passed**, zero failures,
errors or skips across the three disjoint groups below. Each file in the manifest
runs exactly once; the six additional empty-list cases explain the increase from
6,806. These runs use the final implementation, including the neutral empty-list
proof. They qualify the stated selection, **not the entire repository**, isolated
old-wheel compatibility or Pulse. Durations below are per-process pytest times;
they must not be summed as elapsed wall time for concurrent execution.

| Receipt under `.grafx-tmp/` | Passed / failures / errors / skips | Seconds | SHA-256 |
| --- | --- | ---: | --- |
| `fp5-temporal-final-v2-shard-0.xml` | 2,826 / 0 / 0 / 0 | 386.968 | `b86e6f12bc7c0e90c20a4705c61d84eb00da6c646d3f32717bc145b0e194c391` |
| `fp5-temporal-final-v2-shard-1.xml` | 2,070 / 0 / 0 / 0 | 573.483 | `26b4d30ace193603f1069b639e007d24be65c833694b16e3d91a0121d7eb311b` |
| `fp5-temporal-final-v2-shard-2.xml` | 1,916 / 0 / 0 / 0 | 569.710 | `1bf1c49c1f37c4a64132a2708343fbbc7c1073db9d33e77be82fbb05122e51c0` |

The final-code FP-2 rerun also passes all **1,976 required cases**, with 1,921
outside selection and no failures. `fp2-temporal-final-qualified-native.json`
has SHA-256 `1fedb130068d2f84d8dbaf12bc3970d9738453c1263a4da1ea5aaaeb51c6b693`,
byte-identical to the earlier passing FP-2 observation report. Ledger checks,
generated public API documentation, links/anchors, configuration coverage, lint
and diff whitespace checks pass. No commit, push, installation, release or
production data mutation is part of this checkpoint.

### Lifecycle qualification follow-up: transfer, copy and retained history

An integration test exposed a missing catalog-v2 prerequisite in logical transfer:
the detached schema validator and destination installer upgraded for ANY/grouped
schemas but not for typed temporal-only schemas. `transfer._tables` and
`transfer._install_schema` now include `TEMPORAL_VALUE_TYPES` in that prerequisite.
This uses the existing explicit activation/DDL/WAL path; it does not bypass stored
capabilities, rewrite catalog pages directly, or weaken either OCC check.

`tests/api/test_temporal_transfer.py` qualifies all six families using extreme
calendar years, nanosecond resolution, signed offsets, int64 duration components
and a recorded `Future/Recorded` zone deliberately unavailable to rule providers:

- Six pure/NumPy × typed/ANY/flexible logical round-trips, including node/edge
  properties, nested maps/lists, endpoint identity remapping, exact temporal-column
  indexes, verification, checkpoint and read-only reopen.
- Three existing-target copy cases, with tracked source provenance, compatible
  target schemas, durable receipt replay and reopened values.
- Six scan/index × typed/ANY/flexible history cases: update, old/new as-of,
  diff, physical backup and offline restore retain exact property values.
- Six real subprocess cuts across the three models: after the second row batch's
  WAL barrier, or after promotion before acknowledgement. Resume publishes exactly
  two nodes/one edge and repeating resume returns the same report.
- Six malformed native-date payloads across the three models and normal/resumable
  imports. Checksums and framing are recomputed correctly, so semantic decoding
  must reject the impossible date. Tests forbid opening any destination and verify
  that neither the destination nor resume workspace is created.

The pre-fix integration receipt `fp5-temporal-transfer-pre-fix.xml` has 13 passes
and two typed-export failures. The corrected expanded receipt
`fp5-temporal-transfer-adversarial.xml` has **27 passed, zero failures/errors/skips**,
74.116 s; SHA-256
`ea3fcea2bfefd457be87cbe45fb357f95481d71829a8166d030bd548003d3e81`.
Receipts reside under `.grafx-tmp/`. These tests do not claim old-wheel compatibility,
all history crash/retention combinations, ordered temporal indexes, scalar CLI/
tabular transport or Pulse integration. FP-5 remains in progress.

Final adjacent regressions also pass, giving **259 passed**, zero failures,
errors or skips together with the 27 focused tests. The two regression selections
are disjoint and were run concurrently; times are per process, not summed wall time.

| Receipt under `.grafx-tmp/` | Passed | Seconds | SHA-256 |
| --- | ---: | ---: | --- |
| `fp5-temporal-transfer-copy-regression.xml` | 119 | 240.442 | `c729e57f36cdffcb0e86d369f3503901b04e6057a9c97dd24c76fff7208742b4` |
| `fp5-temporal-history-backup-regression.xml` | 113 | 206.095 | `ae3f37db18d91a6b6eae7328f07e39de926bdd8d220a166e9d882b3a267e08be` |

Transfer/copy selection: API `test_logical_transfer`, `test_transfer_resume`,
`test_flexible_graph_transfer`, `test_commit_transfer`, `test_catalog_copy`,
`test_flexible_catalog_copy`, `test_grouped_catalog_copy`; query
`test_ddl_catalog_copy`; storage-core `test_catalog_copy`.
History/backup selection: API `test_system_history_adversarial`,
`test_system_history_compaction`, `test_system_history_documentation`,
`test_system_history_index`, `test_system_history_native`,
`test_system_history_operations`, `test_flexible_system_history`,
`test_physical_backup`; transaction `test_system_history_store`; engine
`test_system_history_access_tree`. These receipts add lifecycle coverage; they do
not replace or inflate the separate original-TCK counts. Generated API reference,
documentation links/anchors/configuration coverage, lint of all changed Python
files and diff whitespace checks pass. No public signature or configuration field
was added in this follow-up.

### Exact CLI and tabular temporal consumption

The next increment implements scalar/nested CLI JSON temporal tags and native
Arrow/Pandas/Polars/Parquet transport. `domain.model.temporal_interchange` centralizes
validated primitive coordinates and the existing entity JSON tags. Whole-entity
observations keep their established grammar; scalar CLI observations no longer
fall back to native object descriptions. CLI parameter parsing is unchanged.

Arrow temporal types use explicit coordinate structs and mandatory
`grafx.type`/`grafx.temporal=components-v1` metadata. The full ordered field matrix,
nullability rules, tariff and usage are documented in
[Extensions and Arrow](../EXTENSIONS_AND_ARROW.md#exact-native-temporal-values-006-development).
Pandas uses ArrowDtype structs; Polars keeps its explicit schema wrapper and only
normalizes documented child-nullability/text-offset representation differences;
Parquet retains the typed structs and metadata. No host datetime, timezone lookup,
ISO parsing, duration normalization or implicit numeric conversion occurs on import.
Native component validation rejects required NULL children and noncanonical values
within the original whole-call executemany savepoint. A parent NULL is preserved.

New tests cover all six types and NULLs across four transports × pure/NumPy,
commit/checkpoint/read-only reopen, exact coordinate/metadata observations, invalid
late-batch metadata, unknown encoding, narrowed Arrow dates, NULL children, invalid
dates, noncanonical duration nanos, oversized zones, structured memory bounds,
cursor snapshot isolation under an independent writer, missing Pandas metadata and
malformed Polars structs. Independent component tests cover all six families with
missing/extra fields and NULL/bool/float/string coercion attempts. CLI tests assert
concrete JSON coordinates, nested values and the approved empty-unknown-label rule.

Testing found and resolved two Polars representation issues: it drops child
non-nullability and widens zone strings. Portable physical children are nullable,
but semantic NULL admission remains enforced by the native decoder. An extra-field
test now explicitly creates the extra field in the first Polars record, avoiding
the library's schema inference discarding that field before Grafx sees it.

Adjacent regressions exposed stale fixtures, not new language failures: four CLI
refusal tests used an unknown label, now legitimately an empty match. They now use
an actually unbound variable, retaining taxonomy, hints and whole-transaction
rollback assertions; a separate test verifies unknown-label success. The isolated
optional-dependency test now loads only the declared required tzdata package by
its package spec, not its site-packages directory; optional imports remain absent
under `-I -S`. Original TCK sources and expectations were not changed.

Preparatory receipts under `.grafx-tmp/`: `fp5-temporal-interchange-regression.xml`
(116 passed/1 failed; missing required tzdata in the fixture) and
`fp5-temporal-cli-entities-regression.xml` (598 passed/4 failed; stale label refusals).
Neither is acceptance. Corrective selection `fp5-temporal-interchange-corrective.xml`
passes **113**, zero failures/errors/skips, 21.618 s; SHA-256
`aaa1944d4d758c33505dba7ffa1c1d937edcff32beeb7aa5beaac29a9bfc9dff`.
Tested optional versions: PyArrow 19.0.1, Pandas 2.2.3, Polars 1.44.2, NumPy 2.5.2.
This is not a minimum/all-version compatibility matrix. Local text/SQLite temporal
ingestion, remaining native index/lifecycle qualification, isolated old-wheel tests
and Pulse integration still require evidence; FP-5 is not complete.

Final regression: **718 distinct tests passed**, zero failures/errors/skips across
the two disjoint selections below (intersection checked from the actual JUnit
case identities). Per-process times must not be summed as concurrent wall time.

| Receipt under `.grafx-tmp/` | Passed | Seconds | SHA-256 |
| --- | ---: | ---: | --- |
| `fp5-temporal-interchange-final.xml` | 115 | 21.615 | `5ec65d9cdb4be967043aacc5d533b68ea173d08471df61e0c7c99f1a83775fb6` |
| `fp5-temporal-cli-entities-final.xml` | 603 | 100.625 | `5e6c9bdc75d11939ca11efcc986c2d02e5391a0910691c69a94918ec9c68592b` |

Interchange selection: API `test_temporal_tabular`, `test_arrow_export`,
`test_arrow_import`, `test_arrow_vectors`, `test_arrow_missing`,
`test_tabular_interop`, `test_tabular_optional`, `test_parquet_interop`,
`test_projection_polars_export`; storage-core `test_temporal_interchange`.
CLI/entity selection: **all `tests/cli`**, query `test_temporal_entity_results`,
`test_temporal_operations`, `test_fp3_entity_scalars`. The earlier 113-test corrective
receipt overlaps these selections and is not added to the distinct total. This is
not a repository-wide, new original-TCK, installed-wheel or Pulse qualification.

Documentation now includes the Arrow field/metadata matrix and tariff, Pandas/
Polars/Parquet consumption, scalar CLI output/parameter boundaries, temporal usage,
README feature inventory and roadmap status. Public API generation, documentation
links/anchors, 39 configuration fields, 11 preserved plans, lint of all changed
Python files and whitespace checks pass. No new public signature, connection
setting, on-disk capability or required package was introduced by this increment.

### Canonical temporal local text and SQLite ingestion

CSV, JSONL and SQLite now accept `DATE`, `LOCALTIME`, `TIME`, `LOCALDATETIME`,
`DATETIME` and `DURATION` in their existing explicit `types` tuples. The shared
`temporal_from_json_value` decoder accepts the same tagged JSON coordinates emitted
by native entity/CLI observations, under a separately declared matching type. Wide
coordinates must be canonical decimal strings; small coordinates are exact integers;
unknown/missing/extra fields, noncanonical duration nanos and implicit coercions refuse.
SQLite TEXT and CSV cells contain encoded JSON; JSONL accepts a temporal object or
encoded JSON string. Bare ISO strings are not guessed. See the full
[local text grammar and examples](../LOCAL_TEXT_IMPORT.md#native-temporal-fields-006-development)
and [SQLite source/transaction contract](../LOCAL_SQLITE_IMPORT.md).

The text parser's duplicate-key/nonfinite-token checks are now shared with encoded
temporal cells. JSONL only permits a nested object for a declared temporal column;
arbitrary MAP/LIST/entity inference remains refused. `max_field_bytes` also checks
the compact UTF-8 representation of object-valued temporal cells. Existing input
record, batch, row/work limits and native executemany savepoint remain active.
SQLite completes bounded validation and closes its source snapshot before Grafx
staging; text imports roll back all rows of the call on a later failure. Neither
path commits caller work or changes WAL/OCC/capability admission.

`tests/api/test_temporal_text_import.py` adds six full-range/native-NULL round-trips
across three source formats × pure/NumPy, direct readers, native commit/checkpoint,
verification and read-only reopen. Thirty late-invalid-cell cases test mismatched
tags, extra/missing fields, numeric instead of string wide coordinates, negative
zero, nonfinite tokens, duplicate keys, required NULL children, ISO inference and
nested coordinate values. Every refusal preserves previously staged caller work;
SQLite refusal also releases the source so an exclusive transaction can begin.
Three field-bound cases cover JSON objects and encoded cells. Seventeen additional
component tests exercise all six tagged round-trips and canonical decimal refusals.

Focused receipt `.grafx-tmp/fp5-temporal-text-focused.xml`: **101 passed**, zero
failures/errors/skips, 13.169 s; SHA-256
`9ac461875d1cc527d4661a1a6938363e9012cdf99bf86023836a5a1aa82563cd`.
Final regressions below are disjoint and total **228 passed**, zero failures/errors/
skips. The focused count overlaps them and is not added to the total. Per-process
times are not additive concurrent wall time.

| Receipt under `.grafx-tmp/` | Passed | Seconds | SHA-256 |
| --- | ---: | ---: | --- |
| `fp5-temporal-local-import-regression.xml` | 160 | 17.186 | `a2dc9e47f7ea9901e073eea6d43b300718aff859e648adfa93c660ccec5a9339` |
| `fp5-temporal-shared-transport-regression.xml` | 68 | 72.088 | `f43cf5f337fafa20fa409a88cedb62a89f7a3398668294afcc18fa2c1362a953` |

Local selection: API `test_temporal_text_import`, `test_local_text_import`,
`test_sqlite_import`, `test_tabular_optional`; storage-core
`test_temporal_interchange`. Shared selection: API `test_temporal_tabular`,
`test_temporal_transfer`; CLI `test_temporal_json`; query `test_temporal_entity_results`.
Documentation includes local formats/type fields, SQL source semantics, bounds,
NULL and failure/rollback behavior, consumption examples, temporal feature inventory,
README and roadmap. API generation, documentation links/anchors/configuration
coverage, lint and whitespace checks pass. No public signature, setting, required
dependency or persistent layout changed. These are scoped native regressions, not
new original-TCK, installed-wheel or Pulse evidence. FP-5 remains open for the
remaining index/lifecycle, cross-version and consumer qualification.

### Native temporal index policies and lifecycle qualification

The support matrix is now explicit in
[Indexes and vectors](../INDEXES_AND_VECTORS.md#native-temporal-key-support-006-development).
All six typed temporal column kinds work as primary keys and as secondary/composite
keys in hash, sparse_hash and posting_hash layouts. Canonical native tags/components
define equality; zone/offset identities and duration components are not normalized
away. The existing key codec/transaction machinery needed **no production change**.

The pre-existing exclusions remain definition-time refusals: ordered keys require
legacy TIMESTAMP then STRING; FTS requires STRING; ANY/flexible properties require
a separately specified mixed-value key normalization contract. The initial local
test draft incorrectly expected an ANY index to work; inspection confirmed the
existing explicit refusal in `Catalog._validated_index_authority` and
`HETEROGENEOUS_PROPERTIES_V1.md`. The finalized tests prove that refusal and correct
scan-based temporal predicates/updates on ANY instead. No original TCK expectation,
profile entry or supported engine behavior was weakened to obtain acceptance.

`tests/api/test_temporal_indexes.py` adds **76 tests**:

- 18 typed temporal secondary-index lifecycles (six types × three layouts):
  actual IndexSeek plans, repeated/missing keys, old-reader/new-writer snapshots,
  updates, transaction rollback, delete/recreate, verification, checkpoint/reopen.
- 18 ANY temporal-value cases prove definition refusal before committed-LSN/catalog
  publication and retain correct mixed-value scan/update behavior.
- 12 typed primary-key cases (six types × pure/NumPy) cover duplicate refusal,
  prior-statement retention, MERGE identity and reopened indexed answers.
- 12 ordered/full-text type refusals verify unchanged committed state/catalog.
- One key-byte test separates recorded zone/offset identities and calendar-versus-
  elapsed duration components.
- Three composite-key maintenance cases include all six families together,
  rehash/rebuild generation replacement, indexed answers and reopen.
- Six real subprocess cuts cover all three hash layouts before COMMIT or after
  durable COMMIT but before page application. Recovery and a repeated reopen prove
  that heap values and old/new composite index keys reflect the same outcome.
- Six independent-writer races prove that duplicate temporal primary keys cannot
  both publish and the surviving graph/index verifies cleanly.

Focused receipts under `.grafx-tmp/`:
`fp5-temporal-index-contract.xml` (61 passed, 24.815 s; SHA-256
`247cf1d67ffef4cf8835d668115ee55dc4b7b33ce3a0cede33d4c6e689a40ff1`)
and `fp5-temporal-index-maintenance-crash.xml` (15 passed, 13.499 s; SHA-256
`e7860a8a71adf7c25e4e34d7b580e412a37b8e909eb1fbe3b16cda4734f98d91`).
Both have zero failures/errors/skips; they overlap the final regression below
and are not added to its count.

Final disjoint regressions: **798 passed**, zero failures/errors/skips.
Times are per process, not summed concurrent wall time.

| Receipt under `.grafx-tmp/` | Passed | Seconds | SHA-256 |
| --- | ---: | ---: | --- |
| `fp5-temporal-index-api-regression.xml` | 157 | 68.392 | `d56dbc4eab7105a46e234babd44d352ad1331d0e072f3048f6d9751cf8ddac26` |
| `fp5-temporal-index-domain-regression.xml` | 641 | 31.706 | `c6e96d78a6d3a9fc7d09a82bc20914bd9817de86b0d07cd123620c3c4894f8a2` |

First selection: API `test_temporal_indexes`, `test_custom_exact_indexes`,
`test_exact_index_multiprocess_fence`; query `test_primary_key_index`,
`test_scalar_primary_key_memo`, `test_primary_key_incremental_memo`; transaction
`test_custom_exact_index_preparation`. Second selection: **all `tests/index`**.
Documentation now distinguishes typed hash-key support from ordered/FTS/ANY refusals,
with usage examples, key identity and lifecycle boundaries. Generated API docs,
documentation links/anchors/configuration coverage, lint and diff checks pass.
This is scoped index qualification, not the remaining full-package acceptance,
old-wheel compatibility or Pulse evidence. FP-5 and the broader plan remain open.

### Installed-wheel temporal fence qualification

The isolated verifier `tools/qualify_temporal_wheels.py` now covers this boundary
using actual installed wheels, not a patched current-reader capability constant.
See [commands and upgrade contract](../V006_COMPATIBILITY.md#later-functional-parity-temporal-boundary).
No runtime code changed in this qualification increment. The wheels and temporary
venvs are local artifacts; neither global packages nor Pulse/data were modified.

Local environment: Windows, CPython 3.13.1. The candidate environment includes
NumPy 2.5.3, google-crc32c 1.8.0 and tzdata 2026.3. All workers use `-I` and verify
their package origin against the selected venv. Byte comparison independently
confirmed that 237 candidate, 153 archived-0.0.5 and 209 archived-0.0.6 package
files match their wheels; the candidate Python files also match the checkout.
Version labels alone do not distinguish the two 0.0.6 binaries:

| Wheel artifact | SHA-256 |
| --- | --- |
| Candidate `.grafx-tmp/fp5-wheel-qualification/candidate/okto_grafx-0.0.6-py3-none-any.whl` | `2a79d9150075a810b22935dbad885015d8966e71fb90b669f9acba7bd365b5b6` |
| Archived `.grafx-tmp/v005-final-dist/okto_grafx-0.0.5-py3-none-any.whl` | `18bde32649aedce143bd083b5c6224347233074ea615b48350201ab2cacc28e8` |
| Archived `.grafx-tmp/language-wheel/okto_grafx-0.0.6-py3-none-any.whl` | `74728ca0dcc93956b84717b46388fac9dad7a48a04a46cc0892fd9b027120f57` |

The matrix passes **24 scenarios**: two old binaries × three states × two old
access modes × two candidate write profiles. States are checkpointed native
temporal data, durable COMMIT before page application (actual process exit 73),
and an old handle opened before activation. Candidate writes use either pure
selectors or NumPy/native CRC; recovery/readback uses pure selectors in both runs.
The control case proves an ordinary idempotent update remains old-readable before
temporal activation; it is not a logical append benchmark.

All old probes leave **every file byte-identical**, including WAL/control files,
with no exclusions and no new/deleted files. Materialized/live probes and writable
pending-WAL probes must report `schema_version_mismatch` with exactly unsupported
bit 23. Read-only pending-WAL probes instead refuse at `read_only_consistency`
before replay; this is not mislabeled as a capability-bit refusal. Unrelated
errors/timeouts cannot pass the verifier. The candidate recovers the original six
values, verifies the store, checkpoints and reopens again. Inputs include extreme
years, signed offsets, nanoseconds, maximal duration months and an unavailable
recorded zone, which must survive without timezone lookup or normalization.

| Receipt under `.grafx-tmp/` | Result | SHA-256 |
| --- | --- | --- |
| `fp5-wheel-qualification/run-4/report.json` | 12 pure-write scenarios passed | `b6a609c78b6c0a034291ef7f71fa52eae4af77f3aebea4477e2f5fb8fa36d9c3` |
| `fp5-wheel-qualification/run-5/report.json` | 12 accelerated-write scenarios passed | `6eb294035f0a084d8502687bd02b0be8585a58389647eb625e5effb5d36865b6` |
| `fp5-temporal-wheel-fences-qualified.xml` | 138 passed, zero failures/errors/skips, 18.184 s | `c6431f3d3dc76f1092aad2d73f80933126321513c2e08be76a4b7b32e439197d` |
| `flexible-match4-current-qualified.json` | Original Match4 #0004 passed; 3,896 outside selection | `e282d942c1871ce7605eb04c0855d6cc309cfa1a656cb681a7696b37e2579c30` |

The pure receipt predates the report's explicit `profile` field; its workers all
used pure selectors. The accelerated receipt records that profile explicitly.
The 138-test selection comprises tools `test_qualify_temporal_wheels`, API
`test_temporal_native_storage`, transaction `test_read_only_capability`, and query
`test_unlabeled_nodes`, `test_any_properties`, `test_nan_expression_storage`.
Verifier tests reject unrelated errors and audit the receipts against the seeded
values. The two retained-receipt tests explicitly skip when local matrix artifacts
are absent; both executed here. Match4 ran statefully against the original pinned
source, with V2 and predecessor V1 ledger verification and no inferred schema.
None of these overlapping evidence sets is added to earlier suite counts.

This closes the scoped temporal old-binary fence qualification. It does not
establish all historical binaries, other OS/Python combinations, the later FP-6
formats, or installed-Pulse compatibility. Drain/upgrade all participants before
activation; old live handles are fail-closed, not supported mixed-version peers.
Combined checkpoint C, broader package acceptance and Pulse validation remain open.
