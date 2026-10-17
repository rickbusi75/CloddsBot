import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, writeFile, mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { observerCommand } from '../../src/channels/telegram/observer-control';

test('only the explicit operator in a private chat can pause or resume', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'observer-control-'));
  try {
    assert.match((await observerCommand('/veille pause', false, true, dir))!, /réservée/);
    assert.match((await observerCommand('/veille pause', true, false, dir))!, /réservée/);
    assert.match((await observerCommand('/veille pause', true, true, dir))!, /suspendues/);
    assert.equal(JSON.parse(await readFile(join(dir, 'autonomy/control.json'), 'utf8')).paused, true);
    await observerCommand('/veille reprise', true, true, dir);
    assert.equal(JSON.parse(await readFile(join(dir, 'autonomy/control.json'), 'utf8')).paused, false);
    assert.equal(await observerCommand('/status', true, true, dir), null);
  } finally { await rm(dir, { recursive: true, force: true }); }
});
test('reports warn when the worker is stale or unavailable', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'observer-stale-'));
  try {
    assert.match((await observerCommand('/veille', true, true, dir))!, /pas encore/);
    await mkdir(join(dir, 'autonomy'));
    await writeFile(join(dir, 'autonomy/status.json'), JSON.stringify({ at: new Date(0).toISOString(), report: 'paper', lastRun: null }));
    assert.match((await observerCommand('/veille bilan', true, true, dir))!, /État ancien/);
  } finally { await rm(dir, { recursive: true, force: true }); }
});
