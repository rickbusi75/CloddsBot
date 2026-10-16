import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { getOrderbook } from '../../src/exchanges/hyperliquid';

const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });

test('Hyperliquid l2Book object levels preserve prices, sizes and order counts', async () => {
  globalThis.fetch = async (_url, init) => {
    assert.deepEqual(JSON.parse(String(init?.body)), { type: 'l2Book', coin: 'BTC' });
    return new Response(JSON.stringify({ levels: [
      [{ px: '68000.5', sz: '0.125', n: 3 }],
      [{ px: '68001', sz: '1.25', n: 7 }],
    ] }));
  };
  const book = await getOrderbook('BTC');
  assert.equal(book.coin, 'BTC');
  assert.deepEqual(book.levels, [
    [{ price: 68000.5, size: 0.125, numOrders: 3 }],
    [{ price: 68001, size: 1.25, numOrders: 7 }],
  ]);
});

test('Hyperliquid accepts empty sides without inventing liquidity', async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({ levels: [[], []] }));
  assert.deepEqual((await getOrderbook('BTC')).levels, [[], []]);
});
