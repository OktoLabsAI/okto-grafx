# Logical transfer: installed historical/current wheel qualification

September 12, 2026, `feature/v0.0.6`. This supplies the older-artifact-importer
evidence required by [independent namespaces](../specs/GRAPH_NAMESPACES_V1.md) and
the [functional-parity plan](../specs/FUNCTIONAL_PARITY_PLAN.md). It is not release,
global installation, installed-Pulse acceptance or a claim about all old binaries.

## Fixed matrix and outcome

`tools/qualify_transfer_wheels.py` completed **42/42 worker checks**, terminal exit
0: **10 artifact-producing fixtures, 16 successful imports and 16 exact format
refusals**. These are not 42 independent import scenarios. All workers run with
`python -I` and verify that Grafx comes from the selected isolated installation.

| Direction / artifact | Qualification |
| --- | --- |
| Each archived writer → current importer, format 1 | Exact rows, parallel edges, self-loop, fresh destination UUID, mapping count, verification and two read-only reopens; current importer uses one-row batches |
| Current writer → each archived importer, format 1 | Same oracle and reopen checks, using the archived importer's default 256-row batch setting; the tiny-batch limitation below remains explicit |
| Current writer → current importer, formats 2 and 3 | Ordinary and resumable import, one-row batches, repeated completed-resume acknowledgment, exact flexible nested values or kind-qualified mappings, verification and two reopens |
| Current format 2/3 → each archived importer | Ordinary and resumable APIs refuse with exactly `recovery_refused`, operation `logical_transfer`, reason `unsupported_format`; all artifact bytes and destination-parent files/directories remain unchanged |

The matrix uses pure and accelerated **source/reopen** profiles: pure
codec/vector/checksum versus NumPy codec/vector math and native checksum. Import
itself retains the public API's default physical configuration; there is no
per-import codec option and no test-only override pretending there is one.

Format 2 uses native flexible nodes/relationships and a property changing type
between a nested map/list and an integer across nodes. Format 3 uses an ordinary
typed node and relationship both named N. All fixtures have two nodes and three
edges, including parallel edges and a loop; endpoints, values and multiplicity
are checked independently. Format 1 uses unique typed names. A successful source
export and each import must match the explicitly specified oracle.

No runtime fix was required in the current importer: its earlier per-batch
durable-ID-floor handling already passed these installed tests. This increment
adds a reproducible verifier, adversarial verifier tests and corrects stale API
docstrings to name all three accepted formats. It does not add a format/config bit.

## Historical limitation, not a passing compatibility claim

The first matrix failed on a fixture expectation: native returned nested lists
are immutable tuples, both before and after transfer. The expectation was fixed
at both ends, with no value transformation added to transfer.

The next run exposed a real **old-importer** defect: `batch_rows=1` eventually
attempts to insert an explicit identity below the durable reservation floor and
correctly trips `GrafxTransactionStateError`. Separate invocations reproduced it
with each archived importer's **own** format-1 export, not only current exports.
The failed targets were not promoted. The old packages were not patched and their
identity protection was not disabled. Those old versions are **not qualified for
arbitrary batch sizes** by the passing default-batch matrix. Use the current
importer for reliable bounded multi-batch imports; its one-row cases pass.

Retained failed matrices: `.grafx-tmp/transfer-compatibility/run-first/report.json`
(nested tuple expectation) and `run-qualified/report.json` (old tiny-batch defect).
The separate old-own-artifact probes exited 1 at `old005-self-batch1` and
`old006-self-batch1`. Their source artifacts remain in the respective recorded
run directories. This is disclosed legacy behavior, not a required-current-profile
failure waived to declare parity.

## Exact environments and receipts

Windows, CPython **3.13.1** for all qualified workers. Isolated dependencies:
NumPy 2.5.3 and google-crc32c 1.8.0; the current wheel also uses tzdata 2026.3.
The selected historical 0.0.5 package contains the transfer API. Another earlier
0.0.5 archive examined during discovery lacked it and was not counted as a format
refusal. An unused initial Python-3.14 test environment is not part of this matrix.

Wheel paths relative to `.grafx-tmp/`:

| Package | Wheel | SHA-256 |
| --- | --- | --- |
| Archived 0.0.5 | `accel-default/dist/okto_grafx-0.0.5-py3-none-any.whl` | `4f09d7e3ba1b8c7b716aa0b278bea2f39428db68fea71b27f56521f3b4bffcc5` |
| Archived early 0.0.6 | `language-wheel/okto_grafx-0.0.6-py3-none-any.whl` | `74728ca0dcc93956b84717b46388fac9dad7a48a04a46cc0892fd9b027120f57` |
| Current candidate 0.0.6 | `transfer-compatibility/candidate-final/okto_grafx-0.0.6-py3-none-any.whl` | `9b3a9e5158e467b27ff9884edd982552e1b82fe6bb67f88b958460af5123e165` |

All installed Python payloads matched their selected wheel: **185 / 208 / 239**
files respectively. All 239 current wheel Python files also matched the source
tree at qualification. Version strings alone do not distinguish these 0.0.6 builds.

| Receipt under `.grafx-tmp/` | Result | SHA-256 |
| --- | --- | --- |
| `transfer-compatibility/run-formats/report.json` | 42 worker checks passed, terminal exit 0 | `50da08a400fcf3e4060c8037c72c7a6ee93f065e6f16bc93d169a767324e09f7` |
| `transfer-compatibility-regression.xml` | 67 passed, no failures/errors/skips, 122.957 s | `81cb9d9524a2e48d3fd36743447428ce4190f55d8a5067ee00c2896a7227f4e2` |
| `transfer-wheel-verifier.xml` | 8 passed, no failures/errors/skips, 0.135 s; overlaps the 67 | `95f04bd889aacb476d8d7f1af07f2c702396f37b1b7e5688dfe217d8b5c41990` |

The 67-case selection includes the verifier's adversarial checks and the complete
ordinary, flexible, namespace and resume transfer files. Verifier tests reject
unrelated lease/identity/path errors as proof of format incompatibility, and
demonstrate that empty workspaces/control-directory changes are not excluded from
the audit. Refused operations have **no** file/directory exclusions.
After final tool changes, the eight verifier tests passed again. Generated API
documentation, links/anchors, all 39 configuration fields, 11 preserved source
plans, changed/new Python lint and whitespace checks passed.

Reproduce after installing the selected wheels in private environments:

```powershell
python tools/qualify_transfer_wheels.py `
  --old-python <old005-python.exe> --old-python <old006-python.exe> `
  --current-python <candidate-python.exe> --output <fresh-directory>
```

The tool refuses an existing output root, installs nothing, never deletes an
existing database and retains per-worker output, import origins and payload hashes.
For the disclosed legacy limit, invoke that old interpreter with `-I`, this tool,
`--worker import`, its own recorded artifact, a fresh `--path` and `--batch-rows 1`.

## Remaining acceptance

This closes the specified older logical-artifact-importer qualification for these
two actual binaries and the fixed fixtures. It does not qualify every historical
binary, every payload/type/index combination, physical mixed-writer admission or
an installed Pulse. Remaining consumer audits, broader Pulse execution and the
FP-4–8 requirements retain their original scope. No global/runtime deployment,
production data change, commit, push or release occurred.
