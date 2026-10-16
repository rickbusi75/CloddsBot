import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { getPolymarketBalance, createExecutionService } from '../../src/execution';
import { Wallet } from 'ethers';
import { buildPolymarketHmacSignature } from '../../src/utils/polymarket-auth';
const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });
const auth = { address: '0x2222222222222222222222222222222222222222', apiKey: 'test', apiSecret: Buffer.from('test').toString('base64'), apiPassphrase: 'test', signatureType: 3 };
test('Deposit Wallet collateral uses type 3 and reads plural allowances in six-decimal units', async () => {
  globalThis.fetch = async input => {
    const url = new URL(String(input));
    assert.equal(url.searchParams.get('asset_type'), 'COLLATERAL');
    assert.equal(url.searchParams.get('signature_type'), '3');
    return new Response(JSON.stringify({ balance: '20284834', allowances: { exchange: '25000000' } }));
  };
  assert.deepEqual(await getPolymarketBalance(auth), { balance: 20.284834, allowance: 25 });
});
test('an authenticated zero collateral balance is authoritative', async () => {
  let requests = 0;
  globalThis.fetch = async () => { requests++; return new Response(JSON.stringify({ balance: '0', allowances: {} })); };
  assert.deepEqual(await getPolymarketBalance(auth), { balance: 0, allowance: 0 });
  assert.equal(requests, 1);
});
test('fallback reads pUSD for type 3 and does not infer allowance from balance', async () => {
  globalThis.fetch = async (_input, init) => {
    if (init?.method !== 'POST') return new Response('', { status: 503 });
    const body = JSON.parse(String(init.body));
    assert.equal(body.params[0].to, '0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB');
    return new Response(JSON.stringify({ result: '0x1318682' }));
  };
  const result = await getPolymarketBalance(auth);
  assert.equal(result.balance, Number(BigInt('0x1318682')) / 1e6);
  assert.equal(result.allowance, 0);
});

const privateKey = '0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80';
test('execution builds a Deposit Wallet order and authenticates creation, cancel and heartbeat bodies', async () => {
  const creds = { ...auth, privateKey, funderAddress: auth.address };
  const service = createExecutionService({ polymarket: creds, dryRun: false, maxOrderSize: 1 });
  let creates = 0, cancellations = 0, heartbeats = 0;
  globalThis.fetch = async (input, init) => {
    const url = new URL(String(input));
    const method = init?.method || 'GET';
    const response = (data: unknown) => new Response(JSON.stringify(data));
    if (method === 'GET') {
      if (url.pathname === '/version') return response({ version: 2 });
      if (url.pathname === '/book') return response({ version: 'v2', tick_size: '0.01', neg_risk: false, bids: [], asks: [] });
      if (url.pathname === '/neg-risk') return response({ neg_risk: false });
      if (url.pathname === '/fee-rate') return response({ base_fee: 0 });
    }
    const headers = init?.headers as Record<string,string>;
    assert.equal(headers.POLY_ADDRESS, new Wallet(privateKey).address);
    assert.equal(headers.POLY_SIGNATURE, buildPolymarketHmacSignature(auth.apiSecret, headers.POLY_TIMESTAMP, method, url.pathname, String(init?.body)));
    const body = JSON.parse(String(init?.body));
    if (url.pathname === '/order' && method === 'POST') {
      creates++;
      assert.equal(body.order.signatureType, 3);
      assert.equal(body.order.signer, auth.address);
      assert.equal(body.order.maker, auth.address);
      assert.ok(body.order.signature.length > 132);
      assert.equal(body.owner, auth.apiKey);
      assert.equal(body.order.makerAmount, '1000000');
      return response({ success: true, orderID: 'mock-order', status: 'matched' });
    }
    if (url.pathname === '/order' && method === 'DELETE') {
      cancellations++; assert.deepEqual(body, { orderID: 'mock-order' });
      return response({ canceled: ['mock-order'], not_canceled: {} });
    }
    if (url.pathname === '/v1/heartbeats' && method === 'POST') {
      heartbeats++; assert.deepEqual(body, { heartbeat_id: '' });
      return response({ heartbeat_id: 'mock-heartbeat' });
    }
    throw new Error(`Unexpected test request ${method} ${url.pathname}`);
  };
  try {
    const result = await service.buyLimit({ platform: 'polymarket', marketId: 'test', tokenId: '12345678901234567890', price: .5, size: 2, orderType: 'FOK' });
    assert.equal(result.success, true);
    assert.equal(result.status, 'filled');
    assert.equal(await service.cancelOrder('polymarket', 'mock-order'), true);
    assert.equal(await service.startHeartbeat(), 'mock-heartbeat');
    assert.deepEqual([creates,cancellations,heartbeats], [1,1,1]);
    const capped = await service.buyLimit({ platform: 'polymarket', marketId: 'test', tokenId: '12345678901234567890', price: .5, size: 3, orderType: 'FOK' });
    assert.equal(capped.success, false);
    assert.equal(creates, 1);
  } finally { service.stopHeartbeat(); }
});

test('open orders use authenticated paginated data routes, and market cancellation stays scoped', async () => {
  const service = createExecutionService({ polymarket: { ...auth, privateKey, funderAddress: auth.address }, dryRun: false });
  let pages = 0;
  globalThis.fetch = async (input, init) => {
    const url = new URL(String(input));
    if (init?.method === 'DELETE') {
      assert.equal(url.pathname, '/cancel-market-orders');
      assert.deepEqual(JSON.parse(String(init.body)), { market: 'condition' });
      return new Response(JSON.stringify({ canceled: ['one','two'] }));
    }
    assert.equal(url.pathname, '/data/orders');
    pages++;
    return new Response(JSON.stringify({ data: [], next_cursor: pages === 1 ? 'next-page' : 'LTE=' }));
  };
  try {
    assert.deepEqual(await service.getOpenOrders('polymarket'), []);
    assert.equal(pages, 2);
    assert.equal(await service.cancelAllOrders('polymarket','condition'), 2);
  } finally { service.stop(); }
});
