"""Bounded read-only adapters. API secrets never enter evidence or tool outputs."""
import datetime as dt
import email.utils
import html
import json
import os
import re
import sqlite3
import urllib.parse
import xml.etree.ElementTree as ET

from psycopg.types.json import Jsonb

from .common import digest, iso, now, number, query, request, stamp, UpstreamError
from .markets import devig, normalize


def clean(text, limit=4500):
    text = re.sub(r'<(script|style)\b[^>]*>.*?</\1>', '', str(text), flags=re.I | re.S)
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', text)))[:limit]


def date(value):
    if stamp(value):
        return stamp(value).isoformat()
    try:
        return email.utils.parsedate_to_datetime(str(value)).isoformat()
    except (ValueError, TypeError):
        return None


def rss_items(raw, theme):
    root = ET.fromstring(raw)
    result = []
    for entry in list(root.findall('.//item')) + list(root.findall('.//{http://www.w3.org/2005/Atom}entry')):
        def field(name):
            return entry.findtext(name) or entry.findtext('{http://www.w3.org/2005/Atom}' + name) or ''
        link = field('link')
        if not link:
            node = entry.find('{http://www.w3.org/2005/Atom}link')
            link = node.get('href', '') if node is not None else ''
        # Preserve the URL published by the feed, including historical HTTP
        # provenance links. Fetching untrusted pages still requires public HTTPS.
        if link.startswith(('https://', 'http://')):
            result.append({'url': link, 'title': clean(field('title'), 500), 'text': clean(field('description') or field('summary')),
                           'published_at': date(field('pubDate') or field('published') or field('updated')), 'theme': theme})
    return result[:50]


class Collectors:
    def __init__(self, store, settings, registry, env=None, transport=request):
        self.store, self.settings, self.registry = store, settings, registry
        self.env = env if env is not None else os.environ
        self.request = transport

    def universe(self, max_pages=12):
        progress = self.store.get('universe_progress', {'cursor': None, 'count': 0, 'cycle': iso()})
        if progress.get('protocols') != ['v1', 'v2']:
            progress = {'cursor': None, 'count': 0, 'cycle': iso(), 'protocols': ['v1', 'v2']}
        cursor = progress.get('cursor')
        total, pages = progress.get('count', 0), 0
        seen = set()
        while pages < max_pages:
            if cursor in seen:
                raise RuntimeError('gamma_pagination_loop')
            seen.add(cursor)
            data, _ = self.request(query('https://gamma-api.polymarket.com/markets/keyset',
                                          {'closed': 'false', 'active': 'true', 'version': ['v1', 'v2'],
                                           'limit': 100, 'after_cursor': cursor}), max_bytes=20_000_000)
            rows = data.get('markets') if isinstance(data, dict) else None
            if not isinstance(rows, list):
                raise ValueError('gamma_keyset_schema_changed')
            for raw in rows:
                try:
                    market = normalize(raw)
                    version = digest(market)
                    self.store.execute('INSERT INTO markets(id,event_id,theme,question,data,active,version) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET event_id=excluded.event_id,theme=excluded.theme,question=excluded.question,data=excluded.data,active=excluded.active,version=excluded.version,updated_at=now()',
                                       (market['id'], market['event_id'], market['theme'], market['question'], Jsonb(market), market['active'], version))
                    self.store.execute('INSERT INTO market_snapshots(market_id,version,data) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',
                                       (market['id'], version, Jsonb(market)))
                except (KeyError, TypeError, ValueError):
                    self.store.put('universe_invalid_rows', self.store.get('universe_invalid_rows', 0) + 1)
            pages += 1
            total += len(rows)
            next_cursor = data.get('next_cursor')
            complete = not rows or not next_cursor or next_cursor == 'LTE='
            if next_cursor == cursor and not complete:
                raise ValueError('gamma_cursor_did_not_advance')
            progress.update(cursor=None if complete else next_cursor, count=0 if complete else total)
            self.store.put('universe_progress', progress)
            if complete:
                self.store.put('universe_last_complete', {'at': iso(), 'markets': total, 'cycle_started': progress.get('cycle')})
                self.store.put('universe_progress', {'cursor': None, 'count': 0, 'cycle': iso(), 'protocols': ['v1', 'v2']})
                break
            cursor = next_cursor
        self.store.source_status('gamma', 'ok')
        return {'pages': pages, 'cycle_markets_seen': total, 'complete': complete, 'next_page_pending': not complete}

    def collect(self, force=False):
        result = {}
        for source in self.registry:
            sid = source['id']
            if not source.get('interval') or sid in ('gamma', 'poly_account'):
                continue
            if not self.settings['sources'].get(sid, False):
                self.store.source_status(sid, 'disabled')
                continue
            if source.get('key') and not self.env.get(source['key']):
                self.store.source_status(sid, 'configuration_required')
                result[sid] = 'configuration_required'
                continue
            state = self.store.one('SELECT last_success,checked_at,last_error FROM sources WHERE id=%s', (sid,))
            if state and state['last_error'] == 'upstream_http_429' and state['checked_at'] and (now() - state['checked_at']).total_seconds() < 3600:
                result[sid] = {'deferred': 'provider_rate_limit'}
                continue
            if not force and state and state['last_success'] and (now() - state['last_success']).total_seconds() < source['interval']:
                continue
            try:
                items = self.feed(sid)
                for item in items:
                    self.store.evidence(sid, item)
                self.store.source_status(sid, 'ok')
                result[sid] = len(items)
            except Exception as error:
                self.store.source_status(sid, 'error', str(error) if isinstance(error, UpstreamError) or type(error) is RuntimeError else type(error).__name__)
                result[sid] = {'error': type(error).__name__}
        return result

    def feed(self, sid):
        if sid == 'usgs':
            data, _ = self.request('https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_week.geojson')
            return [{'url': x['properties']['url'], 'title': x['properties']['title'], 'theme': 'disasters',
                     'text': json.dumps({'properties': x['properties'], 'geometry': x['geometry']}),
                     'published_at': dt.datetime.fromtimestamp(x['properties']['time'] / 1000, dt.timezone.utc).isoformat()}
                    for x in data['features'][:50]]
        if sid == 'eonet':
            data, _ = self.request('https://eonet.gsfc.nasa.gov/api/v3/events?limit=30&status=open')
            return [{'url': x.get('link') or 'https://eonet.gsfc.nasa.gov/api/v3/events/' + x['id'], 'title': x['title'],
                     'theme': 'disasters', 'text': json.dumps(x)[:8000]} for x in data['events']]
        if sid in ('gdacs', 'fed', 'bea_rss'):
            url, theme = {'gdacs': ('https://www.gdacs.org/xml/rss.xml', 'disasters'),
                          'fed': ('https://www.federalreserve.gov/feeds/press_monetary.xml', 'economics'),
                          'bea_rss': ('https://apps.bea.gov/rss/rss.xml', 'economics')}[sid]
            raw, _ = self.request(url, raw=True)
            return rss_items(raw, theme)
        if sid == 'federal_register':
            data, _ = self.request('https://www.federalregister.gov/api/v1/documents.json?per_page=20&order=newest')
            return [{'url': x['html_url'], 'title': x['title'], 'theme': 'politics',
                     'text': clean(x.get('abstract') or '') + '\n' + json.dumps({'type': x.get('type'), 'agencies': x.get('agencies'), 'publication_date': x.get('publication_date')}),
                     'published_at': date(x.get('publication_date'))} for x in data['results'][:30]]
        if sid == 'defillama':
            data, _ = self.request('https://api.llama.fi/v2/chains')
            # Timestamp the observation; never label current TVL as a forecast.
            self.store.put('defillama_snapshot', {'at': iso(), 'chains': data})
            return [{'url': 'https://defillama.com/chains', 'title': 'TVL des principales chaînes — observation',
                     'theme': 'crypto', 'text': json.dumps(sorted(data, key=lambda x: number(x.get('tvl'), 0), reverse=True)[:30])}]
        if sid == 'crypto_prices':
            data, _ = self.request('https://api.coinbase.com/v2/exchange-rates?currency=USD')
            rates = data['data']['rates']
            return [{'url': 'https://www.coinbase.com/price/' + asset.lower(), 'title': 'Prix indicatif ' + asset,
                     'theme': 'crypto', 'text': json.dumps({'asset': asset, 'usd': 1 / float(rates[asset]), 'observed_at': iso()})}
                    for asset in ('BTC', 'ETH', 'SOL') if number(rates.get(asset), 0) > 0]
        if sid == 'gdelt':
            data, _ = self.request(query('https://api.gdeltproject.org/api/v2/doc/doc', {
                'query': '(ceasefire OR election OR inflation OR sanctions)', 'mode': 'artlist', 'format': 'json', 'timespan': '1h', 'maxrecords': 30}))
            return [{'url': x['url'], 'title': x.get('title', ''), 'text': x.get('title', ''), 'theme': 'geopolitics',
                     'published_at': date(x.get('seendate')), 'origin': x.get('domain')} for x in data.get('articles', [])]
        if sid == 'odds':
            return self.odds()
        if sid == 'reliefweb':
            data, _ = self.request(query('https://api.reliefweb.int/v2/reports', {'appname': self.env['RELIEFWEB_APPNAME'], 'limit': 20, 'profile': 'full', 'sort[]': 'date:desc'}))
            return [{'url': x['fields'].get('url', 'https://reliefweb.int'), 'title': x['fields']['title'], 'theme': 'geopolitics',
                     'text': clean(x['fields'].get('body', '')), 'published_at': date(x['fields'].get('date', {}).get('created'))} for x in data['data']]
        if sid == 'acled':
            data, _ = self.request('https://acleddata.com/api/acled/read?limit=50', headers={'Authorization': 'Bearer ' + self.env['ACLED_ACCESS_TOKEN']})
            return [{'url': 'https://acleddata.com', 'title': str(x.get('country')) + ' — ' + str(x.get('event_type')), 'theme': 'geopolitics',
                     'text': json.dumps(x)[:7000], 'published_at': date(x.get('event_date'))} for x in data.get('data', [])]
        if sid == 'ucdp':
            version = self.env.get('UCDP_DATASET_VERSION', '26.0.9')
            if not re.fullmatch(r'[0-9.]{4,20}', version):
                raise ValueError('invalid_ucdp_version')
            data, _ = self.request('https://ucdpapi.pcr.uu.se/api/gedevents/' + version + '?pagesize=30', headers={'x-ucdp-access-token': self.env['UCDP_ACCESS_TOKEN']})
            return [{'url': 'https://ucdp.uu.se', 'title': str(x.get('country')) + ' — ' + str(x.get('conflict_name')), 'theme': 'geopolitics',
                     'text': json.dumps(x)[:8000], 'published_at': date(x.get('date_end'))} for x in data.get('Result', [])]
        if sid == 'eia':
            data, _ = self.request(query('https://api.eia.gov/v2/petroleum/pri/spt/data/', {'api_key': self.env['EIA_API_KEY'], 'frequency': 'daily', 'data[0]': 'value', 'sort[0][column]': 'period', 'sort[0][direction]': 'desc', 'length': 20}))
            return [{'url': 'https://www.eia.gov/opendata/', 'title': 'EIA — prix pétroliers publiés', 'theme': 'economics', 'text': json.dumps(data.get('response', {}).get('data', []))[:12000]}]
        if sid == 'congress':
            data, _ = self.request(query('https://api.congress.gov/v3/bill', {'api_key': self.env['CONGRESS_API_KEY'], 'format': 'json', 'limit': 20}))
            return [{'url': 'https://www.congress.gov', 'title': x.get('title', ''), 'theme': 'politics', 'text': json.dumps(x)} for x in data.get('bills', [])]
        if sid == 'coingecko':
            data, _ = self.request('https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&per_page=20&page=1', headers={'x-cg-demo-api-key': self.env['COINGECKO_DEMO_API_KEY']})
            return [{'url': 'https://www.coingecko.com/en/coins/' + x['id'], 'title': x['name'], 'theme': 'crypto', 'text': json.dumps(x)[:5000]} for x in data]
        if sid == 'football_data':
            data, _ = self.request('https://api.football-data.org/v4/matches', headers={'X-Auth-Token': self.env['FOOTBALL_DATA_TOKEN']})
            return [{'url': 'https://www.football-data.org', 'title': x.get('homeTeam', {}).get('name', '') + ' / ' + x.get('awayTeam', {}).get('name', ''),
                     'theme': 'sports', 'text': json.dumps(x)[:5000], 'published_at': date(x.get('lastUpdated')), 'delayed': True} for x in data.get('matches', [])[:40]]
        if sid == 'launch_library':
            data, _ = self.request('https://ll.thespacedevs.com/2.3.0/launches/upcoming/?limit=10')
            return [{'url': x.get('url') or 'https://thespacedevs.com/llapi', 'title': x['name'], 'theme': 'science', 'text': json.dumps(x)[:7000]} for x in data.get('results', [])]
        if sid == 'clinicaltrials':
            data, _ = self.request('https://clinicaltrials.gov/api/v2/studies?pageSize=10&format=json&sort=LastUpdatePostDate:desc')
            return [{'url': 'https://clinicaltrials.gov/study/' + x['protocolSection']['identificationModule']['nctId'],
                     'title': x['protocolSection']['identificationModule'].get('briefTitle', ''), 'theme': 'science', 'text': json.dumps(x)[:7000]} for x in data.get('studies', [])]
        if sid == 'openfda':
            data, _ = self.request('https://api.fda.gov/drug/enforcement.json?limit=10&sort=report_date:desc')
            return [{'url': 'https://open.fda.gov/apis/drug/enforcement/', 'title': x.get('product_description', '')[:300], 'theme': 'science', 'text': json.dumps(x)[:6000]} for x in data.get('results', [])]
        raise ValueError('source_adapter_not_available')

    def odds(self):
        day = iso()[:10]
        used = self.store.one("SELECT COALESCE(sum(credits),0) total FROM api_usage WHERE source_id='odds' AND at>=date_trunc('day',now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'")['total']
        # Existing observer currently rotates two one-credit sports daily.
        # Include this documented consumption and preserve a monthly reserve.
        external_today = 0
        ledger = self.env.get('OBSERVER_LEDGER', '/observer/observer.sqlite')
        with sqlite3.connect('file:' + ledger + '?mode=ro', uri=True) as connection:
            row = connection.execute("SELECT value FROM kv WHERE key='oddsDay'").fetchone()
            if row and json.loads(row[0]) == day:
                external_today = 2
        items = []
        regions = self.settings['odds_regions']
        estimated = len(regions.split(','))
        for sport in self.settings['odds_sports'][:4]:
            if used + external_today + estimated > self.settings['odds_daily_credits']:
                break
            remaining = self.store.one("SELECT remaining FROM api_usage WHERE source_id='odds' ORDER BY id DESC LIMIT 1")
            if remaining and remaining['remaining'] is not None and remaining['remaining'] <= 20:
                break
            # Charge the reservation before a request; failure keeps the estimate.
            self.store.execute('INSERT INTO api_usage(source_id,credits,data) VALUES(%s,%s,%s)', ('odds', estimated, Jsonb({'state': 'reserved', 'sport': sport})))
            data, headers = self.request(query('https://api.the-odds-api.com/v4/sports/' + urllib.parse.quote(sport, safe='') + '/odds', {
                'apiKey': self.env['ODDS_API_KEY'], 'regions': regions, 'markets': 'h2h', 'oddsFormat': 'decimal'}))
            lower = {k.lower(): v for k, v in headers.items()}
            self.store.execute('INSERT INTO api_usage(source_id,credits,remaining,data) VALUES(%s,%s,%s,%s)',
                               ('odds', number(lower.get('x-requests-last'), estimated) - estimated, number(lower.get('x-requests-remaining')), Jsonb({'state': 'settled', 'sport': sport})))
            used += number(lower.get('x-requests-last'), estimated)
            for event in data[:30]:
                books = []
                for bookmaker in event.get('bookmakers', []):
                    market = next((x for x in bookmaker.get('markets', []) if x.get('key') == 'h2h'), {})
                    values = devig(market.get('outcomes', []))
                    if values:
                        books.append({'name': bookmaker.get('key'), 'updated': bookmaker.get('last_update'), 'probabilities': values})
                items.append({'url': 'https://the-odds-api.com/#event-' + event['id'], 'title': event['home_team'] + ' vs ' + event['away_team'],
                              'theme': 'sports', 'text': json.dumps({'start': event['commence_time'], 'sport': sport, 'books': books}),
                              'event_id': event['id'], 'structured': {'home': event['home_team'], 'away': event['away_team'], 'start': event['commence_time'], 'books': books}})
        return items

    def search(self, question, theme):
        key = 'search:' + digest([question, iso()[:13]])
        cached = self.store.get(key)
        if cached is not None:
            return cached
        items, source = [], None
        if self.env.get('BRAVE_SEARCH_API_KEY') and self.settings['sources'].get('brave'):
            used = self.store.one("SELECT count(*) n FROM api_usage WHERE source_id='brave' AND at>now()-interval '1 day'")['n']
            if used < 12:
                # Failed requests also remain counted; avoid an unbounded retry loop.
                self.store.execute('INSERT INTO api_usage(source_id,credits) VALUES(%s,1)', ('brave',))
                try:
                    data, _ = self.request(query('https://api.search.brave.com/res/v1/web/search', {'q': question, 'count': 5, 'freshness': 'pw'}), headers={'X-Subscription-Token': self.env['BRAVE_SEARCH_API_KEY']})
                    items = [{'url': x['url'], 'title': x.get('title', ''), 'text': x.get('description', ''), 'theme': theme,
                              'published_at': date(x.get('page_age'))} for x in data.get('web', {}).get('results', [])[:5]]
                    source = 'brave'
                    self.store.source_status('brave', 'ok')
                except Exception as error:
                    self.store.source_status('brave', 'error', str(error) if isinstance(error, UpstreamError) else type(error).__name__)
        if not items and self.env.get('SEARXNG_URL') and self.settings['sources'].get('searxng'):
            # This URL is private configuration, never supplied by an agent.
            try:
                data, _ = self.request(query(self.env['SEARXNG_URL'].rstrip('/') + '/search', {'q': question, 'format': 'json'}))
                items = [{'url': x['url'], 'title': x.get('title', ''), 'text': x.get('content', ''), 'theme': theme} for x in data.get('results', [])[:5]]
                source = 'searxng'
                self.store.source_status('searxng', 'ok')
            except Exception as error:
                self.store.source_status('searxng', 'error', str(error) if isinstance(error, UpstreamError) else type(error).__name__)
        result = []
        for item in items:
            if not str(item['url']).startswith('https://'):
                continue
            try:
                text, _ = self.request(item['url'], raw=True, untrusted=True, timeout=10, max_bytes=500000)
                item['text'] = clean(text)
            except Exception:
                item['excerpt_only'] = True
            item['evidence_id'] = self.store.evidence(source, item)
            result.append(item)
        self.store.put(key, result)
        return result

    def specialized(self, market):
        theme, question = market['theme'], market['question']
        if theme == 'weather':
            city = next((k for k in self.settings['weather_locations'] if k in question.lower()), None)
            if not city:
                return {'status': 'station_mapping_required'}
            latitude, longitude, zone = self.settings['weather_locations'][city]
            if self.settings['sources'].get('openmeteo'):
                data, _ = self.request(query('https://ensemble-api.open-meteo.com/v1/ensemble', {'latitude': latitude, 'longitude': longitude, 'hourly': 'temperature_2m', 'models': 'icon_seamless', 'timezone': zone, 'forecast_days': 7}))
                self.store.source_status('openmeteo', 'ok')
                return {'type': 'ensemble', 'city': city, 'station_rules_must_be_verified': True, 'data': data}
            if not self.settings['sources'].get('nws') or not zone.startswith('America/'):
                return {'status': 'weather_provider_mapping_required', 'city': city}
            data, _ = self.request('https://api.weather.gov/points/' + str(latitude) + ',' + str(longitude))
            forecast, _ = self.request(data['properties']['forecastHourly'])
            self.store.source_status('nws', 'ok')
            return {'type': 'nws_hourly', 'city': city, 'station_rules_must_be_verified': True, 'data': forecast['properties']['periods'][:72]}
        if theme == 'economics' and self.settings['sources'].get('bls'):
            data, _ = self.request('https://api.bls.gov/publicAPI/v2/timeseries/data/CUUR0000SA0')
            if data.get('status') != 'REQUEST_SUCCEEDED':
                self.store.source_status('bls', 'error', 'bls_request_not_succeeded')
                return {'status': 'bls_data_unavailable'}
            self.store.source_status('bls', 'ok')
            return {'type': 'historical_cpi_not_a_forecast', 'data': data}
        if theme == 'crypto':
            return {'type': 'current_chain_tvl_not_forecast', 'data': self.store.get('defillama_snapshot', {})}
        return {'type': 'stored_domain_evidence', 'note': 'Vérifier la définition précise de l’événement et la date de chaque source.'}
