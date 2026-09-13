import { runGrafx, type GrafxDocument, type GrafxOptions } from './grafx.mjs';

// Compile with TypeScript --noEmit --module nodenext --target es2022.
// Replace the example path before executing; this performs only schema inspection.
const options: GrafxOptions = { maxBytes: 1024 * 1024, timeoutMs: 10000 };
const document: GrafxDocument = await runGrafx(['schema', '/path/to/graph'], options);
if (document.truncated !== false || !Array.isArray(document.tables)) {
  throw new Error('Expected a complete table inventory');
}
for (const table of document.tables) {
  if (typeof table !== 'object' || table === null || typeof table.name !== 'string') {
    throw new Error('Invalid table entry');
  }
  console.log(table.name);
}
