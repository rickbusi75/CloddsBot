import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { getEnvironmentTradingCredentials } from '../../src/credentials/environment';
import { createCredentialsManager } from '../../src/credentials';
import type { Database } from '../../src/db';

const fixture: NodeJS.ProcessEnv = {
  POLY_API_KEY: 'api', POLY_API_SECRET: 'secret', POLY_API_PASSPHRASE: 'pass', POLY_PRIVATE_KEY: 'test-key',
  POLY_FUNDER_ADDRESS: 'test-funder', POLY_SIGNATURE_TYPE: '3', HYPERLIQUID_WALLET: 'test-owner', HYPERLIQUID_PRIVATE_KEY: 'test-agent',
  CLODDS_MAX_ORDER_USD: '1', DRY_RUN: 'true', CLODDS_CREDENTIAL_KEY: 'test-encryption-only',
};
const old = Object.fromEntries(Object.keys(fixture).map(k => [k, process.env[k]]));
afterEach(() => { for (const [k,v] of Object.entries(old)) { if (v === undefined) delete process.env[k]; else process.env[k] = v; } });

test('environment resolves complete platform fields; Hyperliquid does not need a nonexistent API_KEY', () => {
  assert.deepEqual([...getEnvironmentTradingCredentials(fixture).keys()], ['polymarket','hyperliquid']);
  assert.equal(getEnvironmentTradingCredentials({ ...fixture, POLY_API_SECRET: '' }).has('polymarket'), false);
  assert.equal(getEnvironmentTradingCredentials({ ...fixture, POLY_SIGNATURE_TYPE: '99' }).has('polymarket'), false);
  assert.equal(getEnvironmentTradingCredentials({ ...fixture, HYPERLIQUID_WALLET: '' }).has('hyperliquid'), false);
});

test('settings are opt-in per context and explicit disabled user rows prevent fallback', async () => {
  Object.assign(process.env, fixture);
  let blocked = false;
  const db = {
    getTradingCredentials: (_user: string, platform: string) => blocked && platform === 'polymarket' ? { enabled: false } : null,
    getUser: () => ({ settings: { maxOrderSize: 50 } }),
  } as unknown as Database;
  const manager = createCredentialsManager(db);
  assert.equal((await manager.buildTradingContext('test-user','test-session')).credentials.size, 0);
  const allowed = await manager.buildTradingContext('test-user','test-session',true);
  assert.deepEqual([...allowed.credentials.keys()], ['polymarket','hyperliquid']);
  assert.equal(allowed.maxOrderSize, 1);
  assert.equal(allowed.dryRun, true);
  blocked = true;
  assert.equal((await manager.buildTradingContext('test-user','test-session',true)).credentials.has('polymarket'), false);
  blocked = false;
  process.env.POLY_API_SECRET = '';
  assert.equal((await manager.buildTradingContext('test-user','test-session',true)).credentials.has('polymarket'), false);
});
