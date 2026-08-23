# Contributing to Okto Grafx

Thank you for helping improve Okto Grafx. Use a focused branch and a pull request for every change;
the `main` branch is protected.

Okto Grafx is a database. A defect here is not a wrong pixel — it is a row that is not there after a
commit said it was. The conventions below exist because of specific failures this project has
already paid for, and each one names the failure it prevents.

## Prerequisites

- Python 3.11, 3.12, or 3.13
- No other runtime dependency. The core is pure Python.

## Getting set up

```bash
git clone https://github.com/OktoLabsAI/okto-grafx.git
cd okto-grafx
pip install -e ".[dev,accel]"
pytest -q
```

The suite is around 7900 tests and takes roughly ten minutes on a free machine. Install `[accel]`
even for development: it makes a second implementation of the checksum port reachable, and a port
with one reachable implementation is an unmeasured surface however green the suite looks.

Run a slice while iterating:

```bash
pytest tests/query -q
pytest tests/txn/test_chain_relink_regressions.py -q
pytest -m "not slow" -q
```

## Where code goes

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
5. Record known gaps in `docs/architecture/PUNCHLIST.md` rather than leaving them implied.
6. Resolve review conversations and wait for the required checks and approvals before merging.

Never commit a database directory, a WAL, metrics output, a quarantine or ledger file, or private
data of any kind. Report security vulnerabilities using [SECURITY.md](SECURITY.md), not a public
issue.

## Licensing of contributions

Okto Grafx is distributed under the Elastic License 2.0 together with the project's SaaS,
competing-service, internal-use, and attribution addendum. See [LICENSE](LICENSE). By opening a pull
request you agree that your contribution is licensed under those same terms.

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
  in `PUNCHLIST.md` are not grounds for rejection.

A false claim in a comment on a safety argument **is** worth raising, because the next reader will
rely on it.
