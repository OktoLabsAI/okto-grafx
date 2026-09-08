# ADR GX-006 — Explicit trusted extensions over public interfaces

Status: accepted semantic contract; GX-CAP-7 implementation pending.
Source: complementary plan §11 in full; agent-first §§3, 14, 20.

## Decision

Python extensions are trusted in-process code, not sandboxed. Loading is explicit,
allowlisted and compatibility-checked; no query downloads code or auto-installs a
missing provider. A manifest declares name/version, supported Grafx API, capability
kinds, determinism, thread safety and side-effect classification.
Duplicate unqualified names refuse; load/unload cannot invalidate active invocations.

V1 covers scalar/aggregate UDFs, read-only table functions/procedures, explicitly
privileged administrative procedures, analyzers, import/export adapters, algorithms
and serializers. It excludes physical WAL/page codecs, mutable catalog access,
commit hooks and dynamically introduced storage/index writers.
Provider callbacks receive immutable public values and a limited public context,
never BufferPool/HeapStore/WAL/coordination authority. Pure expression functions
cannot perform declared side effects; writes use explicit public transactions.

Error translation must preserve native engine refusal and commit-outcome semantics.
Ordinary extension exceptions become typed extension failures; they cannot turn
corruption, lost fencing or an uncertain durable outcome into success/rollback.
Cancellation and process-control exceptions are not disguised as ordinary provider
errors. Cooperative budgets/deadlines are observable, but arbitrary trusted Python
cannot be forcibly sandboxed or safely preempted by a timeout promise.

Installed provider availability and on-disk capability support are different facts.
A store requiring an analyzer must refuse or use an explicitly proved equivalent
path if that implementation is unavailable; never silently change tokenization.
Optional extensions are unnecessary to open stores that do not require them.
External serialization/logging is bounded and sanitized by policy.

## Gate and consequences

Use an external consumer importing only public APIs; static import checks, typing
conformance, incompatible manifests, duplicate names, unavailable providers,
thread/task load/unload races, callback failures and transaction cleanup are required.
Mutable internals remain unsupported even though Python can technically access them.
A truly untrusted-code sandbox would require a separate process/security design and
is not delivered or implied by this SPI. No MCP dependency enters the core wheel.
