# Contributing to Okto Grafx

Thank you for helping improve Okto Grafx. Use a focused branch and a pull request for every change,
including administrator changes. [Repository governance](GOVERNANCE.md) specifies the required
protection and records the activation prerequisite; a CODEOWNERS file alone does not enforce it.

Okto Grafx is a database. A defect here is not a wrong pixel — it is a row that is not there after a
commit said it was. The conventions below exist because of specific failures this project has
already paid for, and each one names the failure it prevents.

## Prerequisites

- Python 3.11, 3.12, or 3.13
- The Python package installs NumPy, google-crc32c and tzdata by default. Optional integrations
  declare their own extras; install the development extras for the test suite.

## Getting set up

```bash
git clone https://github.com/OktoLabsAI/okto-grafx.git
cd okto-grafx
pip install -e ".[dev,accel]"
pytest -q
```

Suite size and duration change with the selected revision and machine. Install `[accel]`
even for development: it makes a second implementation of the checksum port reachable, and a port
with one reachable implementation is an unmeasured surface however green the suite looks.

Run a slice while iterating:

The full Pulse corpus check also needs local checkouts with HEAD exactly at Community commit
`befaf1e4f9da9d0cff7cfc0f4aee177ef0a3e595` and Core commit
`f602c7cc2f6a9f5ef446d4c991309196bd4667c7`. Set `PULSE_COMMUNITY_BASELINE` and
`PULSE_CORE_BASELINE` to those repositories if the documented sibling names are
not present. The freezer verifies HEAD and reads pinned Git objects; do not replace these references
with current source or skip the corpus to produce a green full regression.
CI checks out these public repositories at the exact revisions on every full-suite
leg; no Pulse application installation is needed.

Automatic CI has a 20-minute budget per job. The full four-leg regression matrix
exceeds that budget and runs only through **Actions > ci > Run workflow** on the
chosen branch. Its cross-family coverage check runs on the same manual invocation,
including after a failed suite leg. Select `recall_profile=full` there for the long
recall calibration; the default, push/PR and weekly runs use `smoke`. Lint, installed
consumer checks, compatibility and smoke recall remain automatic. A skipped manual
matrix is not a completed regression and does not replace release qualification.

Two optional audits read retained historical wheel receipts from
`.grafx-tmp/fp5-wheel-qualification/run-{4,5}/report.json`. Clean environments
attribute their absence as pending coverage debt (`CI-WHEEL-RECEIPTS` in
[ROADMAP.md](ROADMAP.md)), rather than claiming those audits ran. Supplying the
real receipts executes all their assertions. Neither outcome certifies a fresh
installed wheel of the current candidate.

```bash
pytest tests/query -q
pytest tests/txn/test_chain_relink_regressions.py -q
pytest -m "not slow" -q
```

## Documentation workflow

The [documentation index](docs/README.md) routes consumers; [ROADMAP.md](ROADMAP.md)
is the only active product backlog, including known limitations and corrections.
Do not create another next-steps/evolution/version-plan document. Dated experiments
belong in `docs/reports/`, indexed there and referenced by the corresponding roadmap
item; specs/ADRs describe acceptance/architecture, not competing execution queues.

For a public change, update the relevant guide, configuration/query/error contract,
roadmap status and changelog. Regenerate the signature appendix when declarations
change; do not hand-edit it. New runnable examples need a consumer regression.

```sh
python tools/generate_api_reference.py --check
python tools/check_documentation.py
python -m pytest tests/consumer/test_documentation.py tests/foundation/test_public_adapter_docs.py -q
```

The archive's content/hash manifest must stay intact. Original paths in historical
prose are provenance, not current instructions. Performance tables must name build,
workload and measurement boundary; unknown data stays unknown. Prefer proportional
focused/grouped checks for documentation changes; engine changes retain their
relevant quality/packaging/multi-process validation obligations.

## Code ownership

The layering is enforced by a test, not by convention. `tests/test_import_boundary.py` walks the
import graph and fails the build if `domain/**` or `engine/**` imports `os`, `time`, `socket`,
`threading`, `pathlib`, or an adapter module.

| Putting | Goes in |
|---|---|
| A format, a value type, the query AST, an error, a metric name, a port protocol | `src/okto_grafx/domain/` |
| Behaviour that `CONTRACT.md` describes | `src/okto_grafx/engine/` |
| Anything that touches the operating system | `src/okto_grafx/adapters/` |
| Choosing an adapter, validating configuration, wiring | `src/okto_grafx/runtime/` |
| A public door or a command | `src/okto_grafx/api/`, `src/okto_grafx/cli/` |

If a change to the engine seems to need `import time`, it needs a port instead. See
[docs/PORTS.md](docs/PORTS.md).

## What is frozen

`docs/architecture/CONTRACT.md` is the normative substrate and parts of it are **frozen**: the error
taxonomy, the on-disk formats, the commit protocol of §8.5, the metric catalogue, and the Definition
of Done in §14. Changing any of them is an amendment with its own discussion, not an implementation
detail. Open an issue with the "frozen surface" option selected before writing code against it.

## Tests

Every user-visible change needs a test. Two rules beyond that, and both come from defects that
shipped past a green suite:

**A test for a defect must fail before the fix and pass after it.** Verify that, do not assume it.
Revert the production change, run the test, watch it fail. A test that passes both ways proves
nothing about the change, and a test *believed* to pin a defect and which does not is worse than no
test — the next person reads a green run as proof of something it never proved. If you cannot make
it fail, say so in the test's own docstring rather than leaving it to be discovered.

**Name the regime you tested in, and the one you did not.** Most defects in this engine were
invisible outside one regime: a refusal landing on one half of validation rather than the other, a
buffer budget large enough never to evict, a reuse list handing out one page rather than two,
contention high enough that writers serialised and the window closed. A concurrency test that only
covers the contended case tests the case where the protocol is doing the least work. When a
parameter decides a *regime* rather than a speed — the frame budget, the process count, the page
size — parametrise over it.

`docs/architecture/LESSONS.md` records these in full. Reading L24, L28, L30, L31 and L32 before
writing a test for a concurrency or storage change is time well spent.

### Markers

```
platform_specific   exercises one OS family; must declare its counterpart (G4)
slow                runtime dominated by volume rather than logic
multiprocess        spawns more than one process against the same database
bench               needs the [bench] extra
pending             declared, registered debt
optional_dependency the test declares the dependency it needs
```

A skip must be **attributed** by one of those markers. An unattributed skip fails the run: a test
that quietly stops running is a test that stops protecting anything.

## Validation before you open a pull request

```bash
pytest -q                      # the whole suite: 0 failures, 0 errors, only attributed skips
python -m build                # package-facing changes
```

For a change to the storage, transaction, index or recovery components, also run the smoke tests a
few times — they are multi-process, they are slow on purpose, and a cheaper version of them did not
find the defects they exist for:

```bash
pytest tests/smoke -q
```

## Pull requests

1. Branch from the current `main`.
2. Keep commits scoped and use a short imperative subject.
3. Add or update tests and documentation for user-visible behavior.
4. Describe the validation you performed. For a correctness change, state the regime you measured in
   and the numbers you measured — a claim nobody can re-run is worse than no claim, and this project
   has a lesson about that (L31).
5. Record known gaps in `ROADMAP.md` (historical detail in `docs/archive/ROADMAP_SOURCES.md`) rather than leaving them implied.
6. Resolve review conversations, inspect the relevant CI results and obtain an approval from
   a designated administrator in [.github/CODEOWNERS](.github/CODEOWNERS) before merging.
   New reviewable commits invalidate stale approvals; the most recent reviewable push must be
   approved by someone other than its pusher. Do not use administrator bypass or direct pushes.

Never commit a database directory, a WAL, metrics output, a quarantine or ledger file, or private
data of any kind. Report security vulnerabilities using [SECURITY.md](SECURITY.md), not a public
issue.

## Licensing of contributions

Okto Grafx is distributed under the Elastic License 2.0 together with the project's SaaS,
competing-service, internal-use, and attribution addendum. See [LICENSE](LICENSE). By opening a pull
request you agree that your contribution is licensed under those same terms.

Public source access does not replace this custom license with unmodified ELv2 or an
OSI-approved license. Third-party code retains its applicable notices and licenses.

Do not add code carrying an incompatible license, and do not remove or alter the Okto Labs or
Okto Grafx notices: Section III of the addendum requires them to stay intact in the source
distribution, the package metadata, and the CLI's `--version` and help output.

## Reviewing

If you review a change here, the standard is `CONTRACT.md` §14, applied literally. In particular:

- A blocking defect is data loss or duplication an ordinary caller can reach, a wrong result, a
  hang, crash or non-`Grafx*` escape from a public door, non-determinism across five consecutive
  runs, or a frozen surface violated.
- Missing coverage on a *correct* guard is **not** grounds for rejection — unless the reverted guard
  is reachable by an ordinary caller and its absence produces one of the outcomes above, in which
  case it is blocking and needs a test.
- Naming, message wording, docstring accuracy, performance, design preference, and anything already
  recorded as nonblocking in [ROADMAP.md](ROADMAP.md) are not grounds for rejection.

A false claim in a comment on a safety argument **is** worth raising, because the next reader will
rely on it.
