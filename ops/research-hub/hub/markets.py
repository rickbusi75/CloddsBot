import datetime as dt
import re

from .common import now, number, stamp

THEMES = {
    'sports': r'\b(nba|nfl|nhl|mlb|soccer|football|cricket|tennis|premier league|champions league|fifa|world cup|super bowl|vs\.?|playoffs|touchdown|bundesliga|liga mx|s[ée]rie a|nwsl|uefa|concacaf|olympics|olympiad|grand slam|trophy|medal|clean sheets?|goal differential|runner.up|championship|playoff|penalty|wicket|mma|ufc|boxing|formula 1|f1|nascar|golf|pga|esports|season record|points table|relegation|promotion|botola|liga)\b',
    'weather': r'\b(temperature|hottest|coldest|fahrenheit|celsius|snowfall|rainfall|weather|°[fc]|hurricane season|heatwave)\b',
    'crypto': r'\b(bitcoin|btc|ethereum|eth|solana|crypto|defi|stablecoin|token|xrp|usdc|usdt|binance|coinbase|onchain|blockchain|nft|mining rig)\b',
    'disasters': r'\b(earthquake|hurricane|wildfire|volcano|tsunami|cyclone|flood|typhoon|drought|landslide)\b',
    'economics': r'\b(inflation|cpi|pce|gdp|fomc|federal reserve|fed rate|federal funds|interest rate|target rate|unemployment|recession|oil price|brent|mortgage|tariff|trade deficit|budget deficit|depeg|treasury yield)\b',
    'geopolitics': r'\b(ceasefire|invasion|airstrike|war|nato|nuclear|peace deal|military|missile|conflict|sanctions|annex|occupy)\b',
    'politics': r'\b(election|senate|president|presidential|democrat|democratic|republican|congress|congressional|governor|minister|parliament|cabinet|vice.president|mayor|referendum|impeach|pardon|veto|treasury secretary|prime minister|chancellor|legislation)\b',
    'science': r'\b(launch|spacex|nasa|rocket|fda|clinical|vaccine|artificial intelligence|agi|openai|anthropic|nobel|climate model|fusion|particle)\b',
    'business': r'\b(earnings|revenue|ipo|merger|acquisition|tesla|nvidia|apple|microsoft|stock|market cap|layoff|bankrupt|subsidiary|valuation)\b'
}


def theme_of(question, tags=()):
    text = (question + ' ' + ' '.join(str(t) for t in tags)).lower()
    return next((theme for theme, pattern in THEMES.items() if re.search(pattern, text)), 'other')


def as_array(value):
    import json
    try:
        return json.loads(value) if isinstance(value, str) else value or []
    except ValueError:
        return []


def normalize(m):
    outcomes = as_array(m.get('outcomes'))
    prices = [number(x) for x in as_array(m.get('outcomePrices'))]
    tokens = as_array(m.get('positionIds') if m.get('version') == 'v2' else m.get('clobTokenIds'))
    event = (m.get('events') or [{}])[0]
    tags = [t.get('label', '') if isinstance(t, dict) else str(t) for t in m.get('tags', [])]
    return {'id': str(m['id']), 'event_id': str(event.get('id') or m['id']), 'question': str(m.get('question', '')),
            'theme': theme_of(str(m.get('question', '')), tags), 'description': str(m.get('description') or '')[:10000],
            'resolution_source': m.get('resolutionSource'), 'end': m.get('endDate'), 'outcomes': outcomes,
            'prices': prices, 'tokens': [str(x) for x in tokens], 'condition_id': m.get('conditionId'),
            'active': m.get('active') is True and m.get('closed') is not True,
            'accepting_orders': m.get('acceptingOrders') is True, 'book_enabled': m.get('enableOrderBook') is True,
            'liquidity': number(m.get('liquidityNum'), 0), 'volume24h': number(m.get('volume24hr'), 0),
            'fees_enabled': m.get('feesEnabled'), 'version': m.get('version', 'v1'),
            'url': 'https://polymarket.com/event/' + str(event.get('slug') or m.get('slug') or '')}


def eligibility(m, settings):
    end = stamp(m.get('end'))
    if not m['active'] or not m['accepting_orders'] or not m['book_enabled']:
        return 'market_not_tradable'
    if len(m['outcomes']) != 2 or len(m['tokens']) != 2 or len(m['prices']) != 2 or any(p is None or not 0 < p < 1 for p in m['prices']):
        return 'unsupported_outcomes'
    if not end or not now() < end < now() + dt.timedelta(days=settings['max_market_days']):
        return 'outside_research_horizon'
    if m['liquidity'] < settings['minimum_liquidity']:
        return 'low_liquidity'
    if len(m['description']) < 40:
        return 'resolution_rules_missing'
    return None


def devig(outcomes):
    values = {str(x['name']): 1 / float(x['price']) for x in outcomes
              if number(x.get('price')) is not None and float(x['price']) > 1}
    total = sum(values.values())
    return {name: value / total for name, value in values.items()} if len(values) >= 2 and total else {}


def simulate_fill(book, dollars, limit, max_spread=.08, at=None):
    at = (at or now()).timestamp()
    timestamp = number(book.get('timestamp'))
    if timestamp is None or not -5 <= at - timestamp / 1000 <= 60 or dollars <= 0 or not 0 < limit < 1:
        return None
    bids = [(number(x.get('price')), number(x.get('size'))) for x in book.get('bids', [])]
    asks = [(number(x.get('price')), number(x.get('size'))) for x in book.get('asks', [])]
    bids = [(p, q) for p, q in bids if p is not None and q is not None and 0 < p < 1 and q > 0]
    asks = sorted((p, q) for p, q in asks if p is not None and q is not None and 0 < p < 1 and q > 0)
    if not bids or not asks or not 0 < asks[0][0] - max(p for p, _ in bids) <= max_spread:
        return None
    shares = cost = 0.0
    for price, quantity in asks:
        if price > limit:
            break
        take = min(quantity, (dollars - cost) / price)
        shares += take
        cost += price * take
        if dollars - cost < 1e-8:
            break
    minimum = number(book.get('min_order_size'))
    if minimum is None or shares + 1e-9 < minimum or cost < dollars - 1e-5:
        return None
    return {'shares': shares, 'cost': cost, 'price': cost / shares, 'mark': max(p for p, _ in bids)}


def final_result(market, outcome, market_id):
    resolution = market.get('resolutionStatus') if market.get('version') == 'v2' else market.get('umaResolutionStatus')
    if str(market.get('id')) != str(market_id) or resolution != 'resolved':
        return None
    labels, prices = as_array(market.get('outcomes')), [number(x) for x in as_array(market.get('outcomePrices'))]
    if outcome not in labels or len(labels) != 2 or sorted(p for p in prices if p is not None) != [0, 1]:
        return None
    return prices[labels.index(outcome)]
