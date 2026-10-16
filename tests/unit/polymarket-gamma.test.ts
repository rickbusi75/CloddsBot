import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { convertGammaMarket, searchGammaMarkets, fetchGammaMarket } from '../../src/feeds/polymarket/gamma';

const conditionId = '0x' + 'a'.repeat(64);
const fixture = () => ({
  id: '562793', conditionId, slug: 'democratic-senate-2026', question: 'Democratic Senate in 2026?',
  outcomes: '["Yes","No"]', outcomePrices: '["0.625","0.375"]',
  clobTokenIds: '["113287701564209339913693347405685749986285999146352375265161592243948562084773","2"]',
  volume: '3622759', volume24hr: 108391.1, liquidity: '2456208.6',
  endDate: '2026-11-04T04:59:00Z', active: true, closed: false,
});
const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });
function respond(body: unknown, check?: (url: URL) => void, status = 200) {
  globalThis.fetch = async (input) => {
    check?.(new URL(String(input)));
    return new Response(JSON.stringify(body), { status });
  };
}

test('Gamma strings preserve outcome/token alignment, condition ID, daily volume and event URL', () => {
  const market = convertGammaMarket(fixture(), 'senate-2026');
  assert.equal(market.id, conditionId);
  assert.deepEqual(market.outcomes.map(o => [o.name, o.price]), [['Yes', .625], ['No', .375]]);
  assert.equal(market.outcomes[0].tokenId, JSON.parse(fixture().clobTokenIds)[0]);
  assert.equal(market.volume24h, 108391.1);
  assert.equal(market.liquidity, 2456208.6);
  assert.equal(market.endDate?.toISOString(), '2026-11-04T04:59:00.000Z');
  assert.equal(market.url, 'https://polymarket.com/event/senate-2026/democratic-senate-2026');
});

test('accepts decoded arrays, zero probabilities and nested event metadata', () => {
  const market = convertGammaMarket({ ...fixture(), outcomes: ['Yes', 'No'], outcomePrices: [0, 1],
    clobTokenIds: ['1', '2'], events: [{ slug: 'parent' }], volume24hr: undefined });
  assert.equal(market.outcomes[0].price, 0);
  assert.equal(market.volume24h, 0);
  assert.match(market.url!, /event\/parent\//);
});

test('malformed and mismatched arrays and invalid probabilities are explicit errors', () => {
  for (const bad of [{ outcomes: 'broken' }, { clobTokenIds: '["1"]' },
    { outcomePrices: '[null, "0.5"]' }, { outcomePrices: '["", "0.5"]' },
    { outcomePrices: '["NaN", "0.5"]' }, { outcomePrices: '["1.2", "0.5"]' },
    { conditionId: undefined }, { endDate: 'invalid' }]) {
    assert.throws(() => convertGammaMarket({ ...fixture(), ...bad }), /Polymarket Gamma/);
  }
});

test('search uses supported query parameters, flattens events, excludes closed rows and deduplicates', async () => {
  respond({ events: [{ slug: 'senate-2026', markets: [fixture(), fixture(), { ...fixture(), closed: true }] },
    { closed: true, markets: [{ ...fixture(), conditionId: 'closed-event' }] },
    { markets: [{ ...fixture(), active: false }] }] }, url => {
    assert.equal(url.pathname, '/public-search');
    assert.equal(url.searchParams.get('q'), 'Senate & House 2026');
    assert.equal(url.searchParams.get('events_status'), 'active');
    assert.equal(url.searchParams.get('keep_closed_markets'), '0');
    assert.equal(url.searchParams.has('_q'), false);
  });
  const result = await searchGammaMarkets('Senate & House 2026');
  assert.equal(result.length, 1);
  assert.equal(result[0].id, conditionId);
});

test('empty search differs from HTTP, transport and schema failures', async () => {
  for (const body of [{ events: [] }, { events: null }, { pagination: { totalResults: 0 } }]) {
    respond(body); assert.deepEqual(await searchGammaMarkets('nothing'), []);
  }
  respond({ error: 'unavailable' }, undefined, 503);
  await assert.rejects(searchGammaMarkets('Senate'), /HTTP 503/);
  respond({ error: 'unexpected schema' });
  await assert.rejects(searchGammaMarkets('Senate'), /invalid search response/);
  respond({ events: [{ markets: [{ ...fixture(), outcomePrices: 'bad' }] }] });
  await assert.rejects(searchGammaMarkets('Senate'), /invalid JSON/);
  globalThis.fetch = async () => { throw new Error('network unavailable'); };
  await assert.rejects(searchGammaMarkets('Senate'), /network unavailable/);
});

test('lookup supports numeric Gamma ID, condition ID and slug', async () => {
  respond(fixture(), url => assert.equal(url.pathname, '/markets/562793'));
  assert.equal((await fetchGammaMarket('562793'))?.id, conditionId);
  respond([fixture()], url => assert.equal(url.searchParams.get('condition_ids'), conditionId));
  assert.equal((await fetchGammaMarket(conditionId))?.id, conditionId);
  respond([fixture()], url => assert.equal(url.searchParams.get('slug'), 'democratic-senate-2026'));
  assert.equal((await fetchGammaMarket('democratic-senate-2026'))?.id, conditionId);
  respond([]); assert.equal(await fetchGammaMarket('missing'), null);
  respond({}, undefined, 404); assert.equal(await fetchGammaMarket('9999999'), null);
  respond({}, undefined, 429); await assert.rejects(fetchGammaMarket('562793'), /HTTP 429/);
});


test('feed adapter accepts the manager single-ID call and legacy platform/ID call', async () => {
  const { createPolymarketFeed } = await import('../../src/feeds/polymarket');
  const feed = await createPolymarketFeed();
  let lookups = 0;
  respond([fixture()], url => {
    assert.equal(url.searchParams.get('condition_ids'), conditionId);
    lookups++;
  });
  assert.equal((await feed.getMarket(conditionId))?.id, conditionId);
  assert.equal((await feed.getMarket('polymarket', conditionId))?.id, conditionId);
  assert.equal(lookups, 1, 'both forms share the same market cache');
  respond({}, undefined, 503);
  await assert.rejects(feed.searchMarkets('Senate'), /HTTP 503/);
  await feed.stop();
});
