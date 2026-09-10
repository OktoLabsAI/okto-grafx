# 0.0.6 compatibility and upgrade matrix

Source/development validation, **not a published release or Pulse installation**.
The reproducible command installs both supplied wheels into separate temporary
targets, invokes isolated interpreters and checks imported package origins:

```sh
python tools/check_v006_upgrade.py --legacy-wheel legacy/okto_grafx-0.0.5-py3-none-any.whl --current-wheel current/okto_grafx-0.0.6-py3-none-any.whl
```

Install NumPy and google-crc32c in the invoking Python environment first. Isolated
workers ignore user-site/PYTHONPATH; a venv avoids accidentally relying on user
packages. The script never installs globally or touches production stores.

Ten cells cover pure and native-CRC/NumPy writers for each boundary:

| Store case | New wheel reads/appends to 0.0.5 | Old wheel after opt-in | Opposite selector readback |
| --- | --- | --- | --- |
| Default, no new capability | Required | Opens | Required |
| `posting_hash`, bit 14 | Required | Refuses | Required |
| Nullable column layouts, bit 13 | Required | Refuses | Required |
| System-time history, bit 15 | Required | Refuses | Required |
| Positional FTS, bit 16 | Required | Refuses | Required |

Each cell verifies native rows, full verification and checkpoint/reopen. Temporal
and positional cells also exercise their public APIs; old-reader refusal must
leave payload hashes unchanged. Wheel SHA-256, platform, Python and results are
emitted as JSON. Both package versions are asserted, not inferred from filenames.

The GitHub workflow `v006-compatibility.yml` defines Windows/Ubuntu and Python
3.11/3.12/3.13 coverage, with the focused feature/crash tests. A configured workflow
is not evidence of a remote run. Actual local regression, artifact hashes and
matrix outcomes are recorded in the [round acceptance report](reports/V006_NATIVE_HISTORY_ROUND.md).
No format downgrade is provided; install compatible binaries on all participants
before opting into new capabilities. Native exact-source tests remain separate
from wheel consumption checks.
