import type { Market } from '../../types';

const GAMMA_URL = 'https://gamma-api.polymarket.com';
type RecordValue = Record<string, unknown>;

function record(value: unknown): RecordValue {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('Polymarket Gamma returned an invalid object');
  }
  return value as RecordValue;
}

function requiredString(value: unknown, field: string): string {
  if (typeof value !== 'string' || !value.trim()) {
    throw new Error(`Polymarket Gamma returned invalid ${field}`);
  }
  return value;
}

function array(value: unknown, field: string): unknown[] {
  let parsed = value;
  if (typeof value === 'string') {
    try { parsed = JSON.parse(value); } catch {
      throw new Error(`Polymarket Gamma returned invalid JSON for ${field}`);
    }
  }
  if (!Array.isArray(parsed)) throw new Error(`Polymarket Gamma returned invalid ${field}`);
  return parsed;
}

function number(value: unknown, field: string): number {
  if ((typeof value !== 'number' && typeof value !== 'string') || value === '' ||
      (typeof value === 'string' && !value.trim()) || !Number.isFinite(Number(value))) {
    throw new Error(`Polymarket Gamma returned invalid ${field}`);
  }
  return Number(value);
}

/** Gamma uses JSON-encoded parallel arrays, unlike CLOB's tokens objects. */
export function convertGammaMarket(value: unknown, eventSlug?: string): Market {
  const data = record(value);
  const names = array(data.outcomes, 'outcomes');
  const prices = array(data.outcomePrices, 'outcomePrices');
  const tokens = array(data.clobTokenIds, 'clobTokenIds');
  if (!names.length || names.length !== prices.length || names.length !== tokens.length) {
    throw new Error('Polymarket Gamma returned mismatched outcome arrays');
  }
  const slug = requiredString(data.slug, 'slug');
  const parentSlug = eventSlug || (Array.isArray(data.events) && data.events[0]?.slug) || slug;
  const endDate = data.endDate ? new Date(requiredString(data.endDate, 'endDate')) : undefined;
  if (endDate && !Number.isFinite(endDate.getTime())) throw new Error('Polymarket Gamma returned invalid endDate');
  return {
    id: requiredString(data.conditionId, 'conditionId'),
    platform: 'polymarket',
    slug,
    question: requiredString(data.question, 'question'),
    description: typeof data.description === 'string' ? data.description : '',
    outcomes: names.map((name, i) => {
      const price = number(prices[i], 'outcome price');
      if (price < 0 || price > 1) throw new Error('Polymarket Gamma returned an out-of-range probability');
      const tokenId = requiredString(tokens[i], 'token ID');
      return { id: tokenId, tokenId, name: requiredString(name, 'outcome'), price, volume24h: 0 };
    }),
    volume24h: data.volume24hr == null ? 0 : number(data.volume24hr, 'volume24hr'),
    liquidity: data.liquidity == null ? 0 : number(data.liquidity, 'liquidity'),
    endDate,
    resolved: data.closed === true,
    tags: [],
    url: `https://polymarket.com/event/${encodeURIComponent(parentSlug)}/${encodeURIComponent(slug)}`,
    createdAt: new Date(),
    updatedAt: new Date(),
  };
}

async function request(path: string): Promise<unknown> {
  const response = await fetch(`${GAMMA_URL}${path}`, { signal: AbortSignal.timeout(15000) });
  if (!response.ok) throw new Error(`Polymarket Gamma request failed (HTTP ${response.status})`);
  return response.json();
}

export async function searchGammaMarkets(query: string): Promise<Market[]> {
  if (!query.trim()) return [];
  // /markets ignores the old _q parameter; public-search returns matching events.
  const params = new URLSearchParams({ q: query, events_status: 'active', limit_per_type: '20',
    keep_closed_markets: '0', search_tags: 'false', search_profiles: 'false' });
  const data = record(await request(`/public-search?${params}`));
  if (data.events == null) {
    if ('pagination' in data || 'events' in data) return [];
    throw new Error('Polymarket Gamma returned an invalid search response');
  }
  const markets = new Map<string, Market>();
  for (const value of array(data.events, 'events')) {
    const event = record(value);
    if (event.closed === true || event.active === false || event.archived === true) continue;
    for (const entry of array(event.markets ?? [], 'event markets')) {
      const row = record(entry);
      if (row.closed === true || row.active === false || row.archived === true) continue;
      const market = convertGammaMarket(row, typeof event.slug === 'string' ? event.slug : undefined);
      markets.set(market.id, market);
    }
  }
  return [...markets.values()];
}

/** Keep condition IDs as Market.id for CLOB consumers, but use Gamma's lookup filters. */
export async function fetchGammaMarket(marketId: string): Promise<Market | null> {
  if (/^\d+$/.test(marketId)) {
    const response = await fetch(`${GAMMA_URL}/markets/${encodeURIComponent(marketId)}`, {
      signal: AbortSignal.timeout(15000),
    });
    if (response.status === 404) return null;
    if (!response.ok) throw new Error(`Polymarket Gamma request failed (HTTP ${response.status})`);
    return convertGammaMarket(await response.json());
  }
  const field = /^0x[\da-f]{64}$/i.test(marketId) ? 'condition_ids' : 'slug';
  const data = array(await request(`/markets?${new URLSearchParams({ [field]: marketId })}`), 'markets');
  return data.length ? convertGammaMarket(data[0]) : null;
}
