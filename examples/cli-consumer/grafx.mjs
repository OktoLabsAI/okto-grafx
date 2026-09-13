// Local subprocess recipe, not a driver or transaction service. Node.js >= 20.
import { spawn } from 'node:child_process';

export function runGrafx(args, { executable = 'oktografx', prefixArgs = [],
    timeoutMs = 30000, maxBytes = 4 * 1024 * 1024 } = {}) {
  if (!Array.isArray(args) || args.some(a => typeof a !== 'string') ||
      !Array.isArray(prefixArgs) || prefixArgs.some(a => typeof a !== 'string') ||
      !Number.isSafeInteger(timeoutMs) || timeoutMs <= 0 || timeoutMs > 2147483647 ||
      !Number.isSafeInteger(maxBytes) || maxBytes <= 0) {
    return Promise.reject(new TypeError('Invalid subprocess arguments/bounds'));
  }
  return new Promise((resolve, reject) => {
    const child = spawn(executable, [...prefixArgs, ...args, '--json'], {
      shell: false, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe']
    });
    let failure, bytes = 0;
    const output = [];
    function stop(error) {
      failure ??= error;
      child.kill('SIGKILL');
    }
    const timer = setTimeout(() => stop(new Error('Grafx subprocess timeout')), timeoutMs);
    child.stdout.on('data', chunk => {
      bytes += chunk.length;
      if (bytes > maxBytes) stop(new Error('Grafx output limit exceeded'));
      else if (!failure) output.push(chunk);
    });
    child.stderr.on('data', chunk => {
      bytes += chunk.length;
      if (bytes > maxBytes) stop(new Error('Grafx output limit exceeded'));
    });
    child.on('error', error => { failure ??= error; });
    child.on('close', (code, signal) => {
      clearTimeout(timer);
      if (failure) return reject(failure);
      if (signal) return reject(new Error(`Grafx terminated: ${signal}`));
      try {
        const document = JSON.parse(Buffer.concat(output).toString('utf8'), (_key, value) => {
          if (typeof value === 'number' && Number.isInteger(value) && !Number.isSafeInteger(value))
            throw new Error('Unsafe JSON integer: use a lossless JSON consumer');
          return value;
        });
        if (!document || typeof document !== 'object' || Array.isArray(document) ||
            document.exit_code !== code || typeof document.result !== 'string')
          throw new Error('Invalid or inconsistent Grafx envelope');
        if (code !== 0) {
          const error = new Error(`Grafx refused operation (exit ${code})`);
          error.document = document;
          error.exitCode = code;
          throw error;
        }
        resolve(document);
      } catch (error) { reject(error); }
    });
  });
}
