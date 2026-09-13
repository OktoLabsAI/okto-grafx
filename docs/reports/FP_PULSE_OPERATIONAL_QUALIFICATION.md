# Current Pulse operational qualification

[Prior value/paging qualification](FP_PULSE_CURRENT_VALUES_QUALIFICATION.md) ·
[Parity plan](../specs/FUNCTIONAL_PARITY_PLAN.md) · [Roadmap](../../ROADMAP.md)

September 13, 2026. This checkpoint exercises the installed current Grafx candidate
through Community, a real HTTP/MCP server and Chrome, using only disposable data.
It does not certify the unfinished full Grafx regression or competitor comparison.
No global install, production data change, release or Core change occurred here.

## Candidate and corrective regression

The private environment is `.grafx-tmp/fp-pulse-current/venv`, CPython 3.13.1.
All package payload files match the checkout, built wheel and installed files,
with a second proof after the installed tests. Package isolation appends existing
user-site dependencies after the private packages; it is not dependency-hermetic.

| Package | Python / total payload files | Wheel SHA-256 |
| --- | ---: | --- |
| Grafx 0.0.6 | 250 / 251 | `3fbd942891d8594feebe998bdddea55b6dd8fff801e004369761a7e2958dbb13` |
| Pulse Community 0.3.3 | 298 / 382 | `09febe3e5f26bc9dfb6e61029d75f483a6b84bdfd056abc1c86e611f58500d29` |
| Pulse Core 0.3.3 | 760 / 825 | `54c1c2e81b2170fde44bd0bb0efb06c2151092125a08c90d43b118cc5425c43b` |

Two actual Community failures were corrected without changing native transaction
semantics or Core contracts:

- `GET /api/v1/kg/schema` offloads blocking provider access and maps neutral graph
  errors to bounded problem responses. A routed board returns its schema; missing
  route/capability returns 503, not an unhandled 500 or fabricated empty schema.
  Authorization remains before provider access. Read-only Cypher shares the same
  mapper, including the existing memory-pressure `Retry-After` behavior.
- Creating a direct spec without its required delivery context now returns the
  existing neutral code-traceability contract as HTTP 409, not an unhandled 500.
  Successful fixtures explicitly supply `delivery_context: greenfield`; no default
  context, admission bypass or weakening of source rules was introduced.

The affected source selection passes **121 tests**, zero failures/errors/skips,
33.515 s. Installed regression passes **158 tests**, zero failures/errors/skips,
46.288 s. Counts overlap earlier selections and are not additive. Coverage includes
schema authorization/thread offload/neutral errors, exact values, general MERGE,
updating subqueries, logical transfer, Settings and source-lineage error contracts.
A Settings test now owns its runtime-composition scope rather than relying on a
repository conftest absent from the installed-package execution.

## Live API, Settings, source lifecycle and search

The isolated server binds only `127.0.0.1:18100/18101`, with stub embeddings,
disabled external metrics and data home `fp-pulse-current/http-data`. The sole test
board is `5176dc59-8739-4670-a12c-ef2a1562dabe`. API identity checks precede mutations.

| Operation | Observed contract |
| --- | --- |
| Settings catalog | All 40 `DatabaseConfig` fields, including path, have entries and descriptions; NumPy codec default, two independent read participants, both graph backends Grafx. |
| Routed schema | Decision and logical `depends_on` present; missing default binding returns typed 503. |
| Missing spec delivery context | Typed 409; before/after spec list identical. |
| Spec create/update | 201 with correct board/draft/context; revision 1 becomes 2 and readback/history preserve the updated content. |
| Ordinary source materialization | Worker creates the spec's working Entity with exact title/content; canonical-only queries exclude it and its source queue entry drains. |
| Spec delete | 204, subsequent source GET 404, empty source list. Native graph preserves the declared sanitized `working_stale` tombstone, not physical absence: blank semantic fields, zero confidence/relevance and `source_deleted`. Canonical view excludes it. |
| Recovery witness search | REST similarity returns exactly `fp-ui-510`, expected title and similarity approximately 1; node detail preserves recovered content. |

The first source's full lifecycle is complete. A second synthetic draft remains in
the disposable home after the recovery test's normal writer trigger. The workers
also materialized the canonical Board root: graph total is now **511**, comprising
510 Decisions and one Entity. This is not a paging duplicate or extra Decision.
The original paging receipt's 510-node total remains correct for its earlier fixture.

Nine authenticated Streamable HTTP MCP calls pass through the neutral v2 outcome:
Decision count 510, logical edge count 20, exact DECIMAL, tagged expression NaN,
read-only MERGE refusal and unchanged count, routed schema, decision similarity,
and natural search. Search returns exactly the recovery witness as a canonical
Decision; natural-search layer audit reports canonical 1 and all other layers 0.
Credentials are test-only and are not included in this report.

## Real-browser observations

Chrome at the isolated URL renders the current Settings catalog, NumPy selections
and the floating query-memory tooltip without shifting form fields. No settings
were saved. A minor existing layering limitation remains: the main Menu Settings
modal can open behind an already-open full-screen KG overlay; closing KG reveals
it. This is recorded, not represented as a successful nested-modal interaction.

The spec's revision-two content rendered in the actual Specs list. KG subsequently
showed `SHOWING 500 / TOTAL 511`, with 510 Decisions and one Entity. Global Discovery
search for `FP8 durable vector recovery witness` completed and displayed **one
result**, `FP8 recovered decision 510`, **CANONICAL**, **100%**, and its mini-graph.
Accessibility state and screenshot were inspected; no saved screenshot is claimed.
Earlier real-browser load-more, Key Decisions ordering and node-detail evidence is
linked above; those package paths and the native candidate are unchanged.

## Actual post-barrier crash and automatic recovery

While the test Pulse process remained alive, a separate installed Grafx writer
changed Decision 510's title/content/vector. A child-only fault hook exited with
code **73** at `_apply_images` after the transaction reached `committed` following
the WAL barrier, but before applying its two page images. Recorded CSN is 6464.
No shared source, runtime setting or production file was patched.

The first subsequent Pulse read returned the last **published** snapshot, including
the old title. It did not force publication of the unacknowledged transaction.
The next ordinary Pulse write, triggered by creating a synthetic spec through its
API, recovered the pending durable COMMIT. The same Pulse PID 13808 and creation
time persisted, without a manual recovery call, native rescue opener or restart.
API/MCP and then the real UI found the new exact content and vector. Decision and
logical edge counts remained 510 and 20. This demonstrates normal-writer recovery,
not a guarantee that read-only access forces replay/publication.

After the live API had already proved recovery, a fresh **read-only installed
Grafx handle** reopened the same graph and checked the exact witness, 510 Decisions
and the original native endpoint-pair table's 20 edges. `verify("all")` reported
**no findings**, with 12,000 pages, 1,054 records and 4,172 index entries checked
(19.537487 s including open/queries). This is a post-recovery integrity check, not
the mechanism that recovered the earlier COMMIT.

## Evidence integrity and failed observers

All paths below are relative to `.grafx-tmp/`; receipts are retained, not overwritten.

| Receipt | SHA-256 |
| --- | --- |
| `fp-pulse-operations-corrective.xml` | `76ff6f5e0acb9d7d0e9110f66ae141dfb4c0dd98fd7ed0efa191de6269c1b1ef` |
| `fp-pulse-current/installed-operational-qualified.xml` | `ccf5a13c9d1ff84d46d40927c33318ab7049cd0b3d7d870efffd965de6c732a8` |
| `fp-pulse-current/installed-proof-operational-qualified.json` | `f30475126b226b6514d2758cd82f5f43b97cc262f0617837c0e12a6576f90c59` |
| `fp-pulse-current/operations-inventory-qualified.json` | `11f4fb63009cceee4be9163395b24714b44825de6e32e9162a86d276f6907352` |
| `fp-pulse-current/operations-source-live.json` | `04d7b0132353791d9cb3bbe3e4994aa5f0cc4bc3bc4d92de0af2ffbb1b76a0e7` |
| `fp-pulse-current/operations-source-tombstone.json` | `d6dfff05b76fecd550320dab5183a6d70005ff4cf2e7043a6ba3b44eb981a6b5` |
| `fp-pulse-current/live-recovery-qualified.json` | `229f8eceeffab9f6fc0215bf1c87fccddc8f62f2717b85cce972c7f4fedad3f7` |
| `fp-pulse-current/operations-recovery-visible.json` | `68dba42fb1cbc8b9419e2d4e332e5267ff071714ea7c866956eaea29c744618f` |
| `fp-pulse-current/mcp-operations.json` | `c7a9fa20f6ea7cefa5e25e25a3ddd0949b3fe22298d2cb1987371344367d3654` |
| `fp-pulse-current/recovered-readonly-verification-qualified.json` | `d78433d31699d466a3b10c8a71c64e7f2c9bb0aef29e388464ef41a242cd174f` |

The crash receipt deliberately retains outcome `failed`: its observer initially
expected immediate visibility after an unacknowledged, unpublished COMMIT. The
later live recovery/search receipt proves the actual sequence described above;
the first result is not relabeled as a pass. Earlier receipts also preserve the
missing-context 500 (corrected in Community), a nonexistent typed Entity property
requested by an observer, an incorrect physical-deletion expectation contrary to
the pre-existing tombstone contract, an initial crash-child dependency-import
failure and the installed Settings test's missing composition scope. Corrections
do not rewrite storage values, TCK oracles or required profile membership.
The first native reopen observer used the public logical `depends_on` name,
which only Community resolves; the native seed uses
`depends_on__Decision__Decision`. Its failed receipt is also preserved. Using
the original native fixture name verifies the same 20 edges without changing data
or native resolution. A fresh REST check independently still returned logical 20.

## Remaining delivery requirements

Full Grafx regression, any corrective reruns it requires, reconciliation of all
22 supplemental contracts and pinned competitor executions remain separate.
These operational results close the exercised Pulse flows only; they do not
authorize a final parity, release or production-certification claim.
