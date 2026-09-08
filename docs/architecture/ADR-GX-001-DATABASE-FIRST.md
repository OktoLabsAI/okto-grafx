# ADR GX-001 — Database-first ownership and dependency direction

Status: accepted for GX-CAP-0 contracts; optional packages are not implemented.
Date: 2026-09-08. Source: complementary plan §§2–5; agent-first plan §§2–4;
EVOLUTION_PLAN_CODEX.md, “Governança dos roadmaps complementares pós-Pulse”.

## Decision

The existing database owns typed graph/vector values, query planning, transactional
indexes, commit identity/metadata, temporal history, generic catalog sessions,
search, verification and the public extension SPI. Domain remains pure; engine
orchestrates domain/ports; runtime and adapters implement clocks, files, locks and
external mechanisms; API composes them. New capabilities do not waive the existing
zero-budget import gate or move host mechanisms inward.

The optional workspace package owns project/user conventions, discovery, root
allowlists and platform paths. The optional agent package owns Agent, Session,
Memory, Claim, Evidence, namespaces, policies, idempotent semantic operations,
ContextPack, token accounting and provider integration. The optional MCP package
owns protocol, framing, discovery, tools/resources and cancellation mapping.
Algorithms and interop are optional public-API consumers. Nexus owns messaging,
presence, delegation, handoffs and scheduling, not the database.

Dependency graph (arrows mean “imports”):

- MCP → agent public API → workspace public API / Grafx public API.
- Workspace → Grafx public API.
- Algorithms / interop → Grafx public API / declared extension SPI.
- Grafx API / runtime / adapters → engine → domain.
- No reverse arrow; domain does not import engine.

These are separate distributions/import roots: `okto_grafx`,
`okto_grafx_workspace`, `okto_grafx_agent`, `okto_grafx_mcp`,
`okto_grafx_algorithms`, `okto_grafx_interop`. Optional packages belong under
`packages/<distribution>/`, not inside the core wheel. Existing optional native
accelerators remain implementations of core ports, not product dependencies.

## Resolution of the two plans

The database-first document's “substitui” wording does not delete the agent-first
plan. Both remain required in full. Generic FTS/temporal/catalog/commit work is
implemented once in Grafx; the detailed agent-first requirements remain in the
optional consumer milestones. No Agent/Claim type enters the universal storage
format, transaction manager or physical WAL. Pulse Core also stays backend-agnostic;
all Pulse/Grafx-specific composition remains Community-owned.

## Enforcement and limits

`tests/test_import_boundary.py` continues to enforce the pure layers.
`tests/test_optional_package_boundary.py` adds all-core source coverage, including
API/CLI/runtime/adapters, with hostile dependency examples for future consumers.
It checks static imports and literal dynamic imports, not arbitrary trusted Python
execution; it is not a sandbox. Each optional wheel must run that policy on its
actual sources plus an installed external-consumer conformance test when created.
The currently supported consumer imports are the package-root facade and
`okto_grafx.errors`; `okto_grafx.api.assembly` is internal composition, not an
extension door. New supported submodules require an explicit public contract.

Core package discovery names exactly `okto_grafx` and `okto_grafx.*`.
The actual-wheel test stages sibling optional packages and still requires only
the core and dist-info to ship. No optional capability is advertised by this ADR.

## Consequences

More package lifecycle work is explicit, but installing the embedded database
does not install an agent framework, an MCP SDK, Arrow or an LLM provider.
No release, merge, storage format or performance acceptance criterion changes.
