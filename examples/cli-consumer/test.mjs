import { test } from 'node:test';
import assert from 'node:assert/strict';
import { runGrafx } from './grafx.mjs';

const fake = source => ({ executable: process.execPath, prefixArgs: ['-e', source, '--'] });
test('argv never interpolates shell syntax', async () => {
  const text = 'hello; echo BAD & $(whoami)';
  const result = await runGrafx([text], fake('console.log(JSON.stringify({exit_code:0,result:"ok",argv:process.argv.slice(1)}))'));
  assert.deepEqual(result.argv, [text, '--json']);
});
test('bounded output and timeout close child before rejecting', async () => {
  await assert.rejects(runGrafx([], { timeoutMs: 2147483648 }), /Invalid/);
  await assert.rejects(runGrafx([], { ...fake('process.stdout.write("x".repeat(4096))'), maxBytes: 20 }), /limit/);
  await assert.rejects(runGrafx([], { ...fake('setInterval(()=>{},1000)'), timeoutMs: 100 }), /timeout/);
});
test('malformed, inconsistent, unsafe and failed envelopes', async () => {
  for (const source of ['console.log("no json")',
    'console.log(JSON.stringify({exit_code:4,result:"bad"}))',
    'console.log(\'{"exit_code":0,"result":"ok","id":18446744073709551614}\')',
    'console.log(JSON.stringify({exit_code:2,result:"bad"}));process.exitCode=2']) {
    await assert.rejects(runGrafx([], fake(source)));
  }
  await assert.rejects(runGrafx([], { executable: 'grafx-nonexistent-test-executable' }));
});
test('real Grafx envelope', { skip: !process.env.GRAFX_PYTHON }, async () => {
  const result = await runGrafx(['capabilities'], {
    executable: process.env.GRAFX_PYTHON, prefixArgs: ['-m', 'okto_grafx.cli']
  });
  assert.equal(result.command, 'capabilities');
});
