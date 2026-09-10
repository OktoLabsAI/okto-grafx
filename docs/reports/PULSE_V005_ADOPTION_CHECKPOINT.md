# Pulse 0.3.3 / Grafx 0.0.5 adoption checkpoint

2026-09-09. First adoption batch, not completion of the entire opportunity list.

[Assessment](PULSE_GRAFX_CAPABILITY_ADOPTION_ASSESSMENT.md) ·
[Community implementation/configuration contract](../../../okto-pulse-v003-kg-load-codex/docs/GRAFX_V005_ADOPTION.md)

## Delivered

- Installed the exact validated Grafx `8c3f6f2` wheel into both local Pulse
  environments (Python 3.13 user-site and uv global tool), keeping `[accel]`
  dependencies present. All 186 packaged files matched the wheel byte-for-byte.
- Added the four 0.0.5 memory/cache constructor options to the Community Settings
  inventory with tooltips, validation and constructor-forwarding coverage.
- Added `kg_grafx_read_participants`, default 2, strict bounded integer 1–8,
  environment/API/persistence/UI support and restart-required semantics.
- Vector reads now charge their selected lane through indexed search, exact
  fallback, shaping and cleanup; standalone resolver-only consumers still work.
- Built Pulse/Core 0.3.3 wheels, rebuilt/verified the packaged frontend and installed
  the matching pair into both local environments. Core source was not modified:
  the existing `okto-pulse-core-kg5-codex@9303f98` supplies the neutral contracts
  required by this Community worktree. The earlier pagination baseline was not a
  sufficient full-package match.

No default-data-home access, rebuild, backfill, cognitive consolidation, publication,
commit or push was performed. Pulse remains stopped. Builds include the existing
Community worktree changes and this batch, not merely its HEAD commit; other dirty
changes were preserved. Grafx implementation remains the exact committed candidate.

## Artifact receipt

Artifacts are local candidates under `.grafx-tmp/pulse-adoption-20260909/dist/`,
except Grafx under `.grafx-tmp/pre-pulse-8c3f6f2/dist/`.

| Wheel | SHA-256 |
| --- | --- |
| `okto_grafx-0.0.5-py3-none-any.whl` | `5395fdd8d9619342c0c35e952b5b1422f810ef18c978a0d39ad3614d6718f529` |
| `okto_pulse_core-0.3.3-py3-none-any.whl` | `13330f2b68211bc3eb28bff3a259ad6e80b957c57ee8adc6b3be4319b3e56572` |
| `okto_pulse-0.3.3-py3-none-any.whl` | `00d5e15d4e4c49509b7067040e65c64954635a696bfbd81ecbedfbbae6f744f4` |

The Community lockfile was resolved offline against these local candidate wheel
directories. It is not a public-PyPI release lock and requires those artifacts;
replace local sources with published artifacts as part of a future release.

## Evidence and limits

- 265 adapter/settings regression tests passed; 30 routed Global/settings tests
  passed; 55 Core contracts/coordination/Discovery tests passed: **350 backend tests**.
- 20 frontend tests passed. TypeScript/Vite build and 78-file frontend sync passed.
  Existing bundle-size/Browserslist warnings are not claimed resolved.
- Isolated installed-wheel smoke: 396 Community and 825 Core packaged files match;
  application construction and complete Settings catalog pass; a disposable native
  graph proves uncommitted writes are invisible to the Community reader and become
  visible after a durable commit. No application lifespan/workers are started.
- The same package-parity/application/native-consumer smoke passed again in both
  actual local installations after installation, with no source checkout on the
  import path. No listener remained on ports 8100/8101.
- The uv tool environment passed dependency validation (121 installed packages).
  The shared Python user-site has existing conflicts involving unrelated
  LangChain/Omni/Streamlit/OpenTelemetry packages; these were not modified by the
  no-dependency installs. Prefer `C:/Users/jpamb/.local/bin/okto-pulse.exe` for its
  isolated uv environment rather than depending on PATH precedence.
- Test artifacts: `C:/Users/jpamb/AppData/Local/Temp/pulse-grafx-adoption-{regression,routing,core}-20260909.xml`.
  Reusable local smoke: `.grafx-tmp/pulse-adoption-20260909/installed_smoke.py`.
- This is not a full Pulse regression, real-data UI test or measured speedup.
  Earlier 102-test evidence overlaps these slices and is not counted again.

## Remaining scope

Global read/write separation with lifecycle drain, remaining blocking-I/O paths,
native full-text/hybrid retrieval, exact-vector fallback optimization, homogeneous
bulk writes, proven same-board saga scheduling, bounded analytics and operational
provenance remain pending. The existing per-board coordinator and Global lock
remain intact. Optional capabilities must have meaningful neutral contracts,
explicit support/readiness and faithful errors, not mandatory Grafx-shaped ports.
