import { test } from 'node:test';
import assert from 'node:assert/strict';
import { metaculusGet, metaculusSearch, metaculusPost, metaculusSummary } from '../../src/feeds/metaculus/api';

test('Metaculus authentication, post IDs, filters and restricted forecasts', async () => {
  const originalFetch = globalThis.fetch;
  const originalToken = process.env.METACULUS_TOKEN;
  try {
    delete process.env.METACULUS_TOKEN;
    await assert.rejects(metaculusGet('posts/'), /METACULUS_TOKEN/);
    process.env.METACULUS_TOKEN = 'test-only-token';
    let calls = 0;
    globalThis.fetch = (async (url: URL | string | Request, init?: RequestInit) => {
      calls++;
      const u = new URL(String(url));
      assert.equal(u.origin, 'https://www.metaculus.com');
      assert.equal((init?.headers as Record<string, string>).Authorization, 'Token test-only-token');
      assert.equal(init?.redirect, 'error');
      assert.equal(u.searchParams.get('search'), 'bitcoin & AI');
      assert.equal(u.searchParams.get('statuses'), 'open');
      assert.equal(u.searchParams.get('project'), '42');
      assert.equal(u.searchParams.get('limit'), '100');
      return new Response(JSON.stringify({ results: [
        { id: 1, title: 'Binary', status: 'open', created_at: '2026-01-01', question: { id: 99, type: 'binary', aggregations: { recency_weighted: { latest: { centers: [0.25] } } } } },
        { id: 2, title: 'Notebook' },
      ] }), { status: 200 });
    }) as typeof fetch;
    const posts = await metaculusSearch('bitcoin & AI', 'open', 1000, 42);
    assert.equal(posts.length, 1);
    assert.equal(metaculusSummary(posts[0]).probability, 0.25);
    assert.equal(metaculusSummary(posts[0]).question_id, 99);
    assert.equal(metaculusSummary({ ...posts[0], question: { id: 99, type: 'binary' } }).probability, null);
    await assert.rejects(metaculusGet('https://example.com/api/'), /Invalid/);
    await assert.rejects(metaculusGet('/not-api/'), /Invalid/);
    assert.throws(() => metaculusPost('../secrets'), /Invalid/);
    assert.equal(calls, 1);
    globalThis.fetch = (async () => new Response('', { status: 401 })) as typeof fetch;
    await assert.rejects(metaculusPost('1'), /401/);
  } finally {
    globalThis.fetch = originalFetch;
    if (originalToken === undefined) delete process.env.METACULUS_TOKEN;
    else process.env.METACULUS_TOKEN = originalToken;
  }
});
