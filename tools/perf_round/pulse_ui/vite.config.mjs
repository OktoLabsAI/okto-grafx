// Uses the consumer's existing dependencies. No npm install or live Pulse routing.
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const frontend = path.join(process.env.OKTO_PULSE_COMMUNITY_REPO, 'frontend');
const require = createRequire(path.join(frontend, 'package.json'));
const root = path.dirname(fileURLToPath(import.meta.url));
export default {
  root,
  cacheDir: path.resolve(root, '../../../.grafx-tmp/v005-vite-cache'),
  esbuild: { jsx: 'automatic' },
  resolve: { alias: {
    '@': path.join(frontend, 'src'),
    'react-dom': path.join(frontend, 'node_modules/react-dom'),
    'react': path.join(frontend, 'node_modules/react'),
  } },
  css: { postcss: { plugins: [require('tailwindcss')({
    content: [path.join(frontend, 'src/**/*.{ts,tsx}'), path.join(root, '*.tsx')],
  }), require('autoprefixer')()] } },
  server: { host: '127.0.0.1', port: 18106, strictPort: true,
    fs: { allow: [root, frontend] },
    proxy: { '/api/v1': { target: 'http://127.0.0.1:18105', rewrite: p => p.replace('/api/v1', '') } },
  },
};
