// Each browser-test server owns an isolated synthetic workspace outside the checkout.
import { spawn, spawnSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const repository = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const [mode, port] = process.argv.slice(2);
if (!['populated', 'empty'].includes(mode) || !['8871', '8872'].includes(port)) {
  throw new Error('Use an isolated populated or empty browser-test server.');
}
const owned = mkdtempSync(join(tmpdir(), 'trading-web-browser-'));
const workspace = join(owned, 'workspace');
const python = join(repository, '.venv', 'bin', 'python');
if (mode === 'populated') {
  const seeded = spawnSync(python, ['scripts/seed_web_demo.py', '--root', workspace], {
    cwd: repository,
    encoding: 'utf8',
  });
  if (seeded.status !== 0) {
    rmSync(owned, { recursive: true, force: true });
    throw new Error('Could not create the isolated synthetic browser fixture.');
  }
} else {
  mkdirSync(workspace, { mode: 0o700 });
}
const server = spawn(
  python,
  [
    '-m',
    'trading_research.web_api',
    '--workspace',
    workspace,
    '--static-dir',
    join(repository, 'web', 'build'),
    '--host',
    '127.0.0.1',
    '--port',
    port,
    '--synthetic',
  ],
  { cwd: repository, stdio: 'inherit' },
);
for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => server.kill('SIGTERM'));
}
server.on('error', () => {
  rmSync(owned, { recursive: true, force: true });
  process.exitCode = 1;
});
server.on('exit', (code) => {
  rmSync(owned, { recursive: true, force: true });
  process.exitCode = code ?? 0;
});
