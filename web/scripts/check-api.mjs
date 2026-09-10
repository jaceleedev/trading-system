import { mkdtemp, readdir, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { createClient } from '@hey-api/openapi-ts';

const temporary = await mkdtemp(join(tmpdir(), 'trading-api-check-'));
const actual = resolve('src/lib/api');

async function files(directory, prefix = '') {
  const found = [];
  for (const entry of await readdir(join(directory, prefix), { withFileTypes: true })) {
    const name = join(prefix, entry.name);
    if (entry.isDirectory()) found.push(...(await files(directory, name)));
    else found.push(name);
  }
  return found.sort();
}

try {
  await createClient({
    input: resolve('openapi.json'),
    output: { path: temporary, postProcess: [] },
    plugins: ['@hey-api/typescript', '@hey-api/client-fetch', '@hey-api/sdk'],
  });
  const expectedFiles = await files(temporary);
  const actualFiles = await files(actual);
  if (JSON.stringify(expectedFiles) !== JSON.stringify(actualFiles)) {
    throw new Error('Generated API file list differs. Run pnpm api:generate.');
  }
  for (const file of expectedFiles) {
    const expected = await readFile(join(temporary, file));
    const current = await readFile(join(actual, file));
    if (!expected.equals(current)) {
      throw new Error(`Generated API differs at ${file}. Run pnpm api:generate.`);
    }
  }
  process.stdout.write(`API client matches OpenAPI (${expectedFiles.length} files).\n`);
} finally {
  await rm(temporary, { recursive: true, force: true });
}
