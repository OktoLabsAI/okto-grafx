# Bounded Node.js / TypeScript consumption

Requires Node.js 20+ and an installed Grafx 0.0.6 CLI. No npm packages are required.
Copy `grafx.mjs` and its TypeScript declaration `grafx.d.mts` together.

```javascript
import { runGrafx } from './grafx.mjs';

const schema = await runGrafx(['schema', '/absolute/path/to/graph', '--limit', '100']);
if (schema.truncated) throw new Error('Full schema required');
console.log(schema.tables);
```

TypeScript uses the same import, with `GrafxDocument` exposing an intentionally
unknown extension payload; narrow/check `tables` or search DTOs before using them.
See `schema.mts`, validated with `tsc --noEmit --strict --module nodenext --target es2022`.
`GrafxOptions` supports `executable`, `prefixArgs`, `timeoutMs` (30000 default)
and combined stdout/stderr `maxBytes` (4 MiB default).
Bounds are positive safe integers; timeout is additionally <=2147483647 ms to
avoid Node's timer-overflow behavior.
To choose a Python environment, pass its absolute executable and
`prefixArgs: ['-m', 'okto_grafx.cli']`; never build a shell command string.

This is a **read-oriented CLI recipe**, not a driver: every invocation has its own
process/handle/snapshot; there is no shared transaction, pool, RPC or authentication.
Use Python for long-lived transactions. Do not use forced process timeouts as a
write rollback mechanism: an interrupted write may already be durably committed.
Never automatically retry a write without resolving its commit outcome.

The recipe rejects malformed JSON, mismatched exit codes, unsafe integer values,
nonzero CLI exits, excess output and timeouts. Grafx 64-bit IDs may exceed
JavaScript's safe integers: this consumer refuses them instead of silently rounding.
For such graphs use a separately validated lossless JSON consumer. Failure objects
from Grafx exits retain `document` and `exitCode`; stderr is drained and counted,
not retained. Limit and timeout errors terminate the direct child and reject only
after `close`. The actual CLI does not spawn a child process tree; this is not a
general-purpose tree-killing sandbox. Use a trusted executable.

Run `node --test examples/cli-consumer/test.mjs` from the repository root.
For the real CLI acceptance case set `GRAFX_PYTHON` to the desired Python executable
and (for a source checkout) `PYTHONPATH` to the repository `src` directory. Without
`GRAFX_PYTHON` the real-engine case is explicitly skipped, not counted as evidence.
