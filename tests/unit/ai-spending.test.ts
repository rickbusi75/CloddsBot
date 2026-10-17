import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { reserveAiSpend, createBudgetFetch } from '../../src/providers/spending';

test('chat reserves against the same persistent ledger and provider totals', () => {
  const dir = mkdtempSync(join(tmpdir(), 'ai-budget-'));
  try {
    const path = join(dir, 'observer.sqlite');
    const provider = { usage_daily: 0, usage_monthly: 0 };
    for (let i = 0; i < 11; i++) reserveAiSpend(path, .05, provider);
    assert.throws(() => reserveAiSpend(path, .051, provider), /ceiling/);
    assert.throws(() => reserveAiSpend(path, .05, { usage_daily: .59, usage_monthly: 0 }), /exhausted/);
    assert.throws(() => reserveAiSpend(path, .01, { usage_daily: .10, usage_monthly: 0 }), /exhausted/);
    assert.throws(() => reserveAiSpend(path, .05, { usage_daily: 0, usage_monthly: 19.99 }), /exhausted/);
    assert.throws(() => reserveAiSpend(path, .01, {}), /unavailable/);
    assert.throws(() => reserveAiSpend(path, .01, { usage_daily: -1, usage_monthly: 0 }), /unavailable/);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
test('budgeted streaming request is sent once only after successful pricing and reservation', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'ai-fetch-'));
  try {
    let sent = 0;
    const base: typeof fetch = async (input, init) => {
      const url = String(input);
      if (url.endsWith('/key')) return Response.json({ data: { usage_daily: 0, usage_monthly: 0 } });
      if (url.endsWith('/models')) return Response.json({ data: [{ id: 'test/model', pricing: { prompt: '0.0000001', completion: '0.0000002' } }] });
      const body = JSON.parse(init!.body as string);
      assert.equal(body.provider.allow_fallbacks, false); sent++;
      return new Response('stream');
    };
    const guarded = createBudgetFetch({ CLODDS_AI_BUDGET_ENABLED: 'true', CLODDS_STATE_DIR: dir }, base);
    const response = await guarded('https://openrouter.ai/api/v1/messages', { body: JSON.stringify({ model: 'test/model', max_tokens: 1024, stream: true }) });
    assert.equal(await response.text(), 'stream'); assert.equal(sent, 1);
    await assert.rejects(guarded('https://openrouter.ai/api/v1/messages', { body: JSON.stringify({ model: 'missing', max_tokens: 1 }) }), /unavailable/);
    assert.equal(sent, 1);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
