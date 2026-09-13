# Final installed Pulse candidate qualification

September 13, 2026. This closes the exercised installed/API/UI/MCP consumer checks
with the final Grafx runtime payload. [Complete native qualification](FP_FINAL_NATIVE_QUALIFICATION.md)
and [the preceding operational/recovery report](FP_PULSE_OPERATIONAL_QUALIFICATION.md)
are separate evidence. Neo4j execution was explicitly deferred by the user;
it does not block this delivery and is not counted as passed. No release or global
installation is implied.

## Exact installed candidate

Grafx wheel `fp-final-wheel-qualification/dist/okto_grafx-0.0.6-py3-none-any.whl`:
SHA-256 `d666704a14492d737c43f0293e71605aeaf279aad65b4086d08bcb0160020b70`.
All 251 package payload files match the source and private installation before and
after the 158 installed Pulse tests. Community and Core use the unchanged paired
operational wheels from the preceding report. No global installation occurred.

The old-to-final wheel proof changes only seven files: documentation/comments,
postponed helper annotations, an annotation-only import with no remaining runtime
Name use, and sorting the same unique export set. Under those declared
normalizations, executable ASTs are identical. This justifies retaining unchanged
behavioral evidence, not claiming that the old full regression passed.

`fp-final-wheel-qualification/nonalgorithmic-delta.json`:
`8ba4470223ab7865e39642a12d8b3d50cc424974d5166c7c01a95ffe9390ec78`.

## Final installed qualification

- 158 passes, zero failures/errors/skips, JUnit 56.354 seconds.
- 16 of 16 bounded Grafx reference scenarios match, using the current shared
  harness and exactly the same queries/oracles used by the qualified Ladybug run.
- 12 HTTP checks pass, including runtime identity, 500 + 11 unique KG nodes,
  stable 511 total, 510 Decisions/20 logical edges, native values and typed refusals.
- Four separate final Settings/schema inventory checks pass.
- Six separate recovered-witness/count/edge/search/detail checks pass.
- Nine actual final MCP calls pass. Both similarity and natural search have
  positive exact-one-result, expected id/title/near-one-similarity assertions;
  natural search has canonical=1 and zero other layers.

The final installed Pulse server is the isolated PID 33712, started at local
01:47:32, ports 18100/18101, using the same disposable fixture home. Production
Pulse/data were not touched. This server restart occurred AFTER the earlier live
fault-to-next-writer recovery was proved in PID 13808. Do not claim that the whole
qualification ran without any restart; only the actual fault/recovery sequence did.

## Final real browser observations

After reloading the isolated UI against the final-wheel server, the observed
accessibility state showed 500/511 nodes (510 Decisions, one Entity). Clicking
load-more displayed all 511 and removed the paging button. Global Discovery search
for `FP8 durable vector recovery witness` returned exactly the recovered Decision
510, canonical, 100%. Key Decisions rendered 100 actual rows. Opening the first
decision displayed id `fp-ui-10`, title `Parity decision 10`, synthetic content,
90% confidence, 43% relevance and Decision type. This is actual browser evidence,
not a React mock or HTTP-only inference.

No final screenshot is claimed: the last screenshot tool output was truncated.
The earlier operational report's screenshot inspection remains its own evidence.
The pre-existing Settings-behind-full-screen-KG layering limitation remains recorded
in that report; no new layering fix was included in this candidate.

## Receipts

All paths below are relative to `.grafx-tmp/`.

| Receipt | SHA-256 |
| --- | --- |
| `fp-final-wheel-qualification/installed-final-qualified.xml` | `372f7b24c05d69f58db3b1506117752435d3c68dac95ff37949104c05830840d` |
| `fp-final-wheel-qualification/installed-proof-final-qualified.json` | `e4963c78948e9a16b3afc81caf4248ec72959d3663037f5b1353f510779bbb3e` |
| `fp-final-wheel-qualification/references-grafx/report.json` | `24e9ce376cdb1f56ea9ad77c65421ea9e4b3bff1cbad3d4655d263c534199084` |
| `fp-final-wheel-qualification/http-final.json` | `29d67468a653cf053dac16a13ce8ace547fa9e7545a63f2e4aa8643ec5d6dc75` |
| `fp-final-wheel-qualification/mcp-final.json` | `4eefe2ef5bf25a433eed824b0d9de244408d7bbfb61bfa140c5f41a0b4ada5cc` |
| `fp-pulse-current/operations-final-inventory.json` | `9c1dd8094197525e6f9c811bd1e5c087467f1e1dcd9b206ff0279316733e4f8d` |
| `fp-pulse-current/operations-final-recovered.json` | `38896ab3d646b6bb9ea3eee105bf3df10114b15c089a167fc848c531e7d17d7c` |

## Neo4j preparation (NOT engine execution)

The user subsequently deferred this execution on September 13, 2026
(`FP-NEO4J-DEFERRED-20260913`). No Docker startup is authorized by this report.
The preparation evidence below is retained for future use, not an active gate.

The exact 5.26.0 Community linux/amd64 digest remains pinned. Docker was confirmed
unavailable: `docker version` returned Server=null and no DockerDesktopLinuxEngine
pipe. A nonblocking request to start Docker Desktop plus an isolated test container
was sent; no response or service startup is claimed here.

Private driver environment: `fp-competitor-qualification/neo4j-driver`.
Driver 5.26.0 wheel SHA-256
`511a6a9468ca89b521bf686f885a2070acc462b1d09821d43710bd477acdf11e`;
pytz 2024.2 wheel SHA-256
`31c7c1817eb7fae7ca4b8c7ee50c72f93aa2dd863de768e1ef4245d426aa0725`.
Both were verified against the exact PyPI version JSON before use.

Prepared 16-case schema/dialect manifest:
`fp-competitor-qualification/neo4j-prepared.json`, SHA-256
`d11c4f888f22637c08f30c6c8ecc15c42ae0fbd0f23a092e7c7d1604e8b57739`.
Queries, expected values, multiplicity and effect oracles are unchanged. Typed
tables become labels with UNIQUE(id), explicitly NOT equivalent to mandatory typed
primary keys. Decimal/nested CREATEs remain native and are not string/DOUBLE
substitutions. A semantic setup refusal is a recorded difference, not an unavailable
service and not a Grafx waiver.

[Tracked Neo4j runner](../../tools/fp_neo4j_reference.py) SHA-256
`f2de391619a979bebf547bf61e0b82929f93ad2875490ece058698e42a0a7e81`;
[Offline safety/oracle tests](../../tools/test_fp_neo4j_reference.py) SHA-256
`eca9a57baed584ce69bc9998c4ca3ab68cd682e6e4cc9385b2c2a4661c2d1694`.
Eleven offline harness tests pass (0.315 s), including untouched oracles, exact
DATE/type/duplicate handling, effects checks, semantic-vs-transport failures and
refusal of foreign/bind-mounted/remotely-exposed/unpinned/stopped containers.
No engine result follows from those mocked safety/observer tests.

The scripts are now tracked. The runner's only promotion change removed an unused
`sys` import during lint; its earlier scratch hash was
`9d8b8d2326a34906dbc9f5f0f998fa2a1c2dcc223ab510e8a7bb01ad1194544d`.
The test file is byte-identical. Their offline tests do not run Neo4j.
Execute them with the private driver interpreter and `-I`; do not use `-O`
(the test-only observers deliberately use assertions). The run command requires
an explicit full container ID, per-run ownership label and prepared manifest.
The tool does not start Docker or create containers. Its data cleanup is confined
to the validated disposable container; user databases and host bind mounts refuse.
