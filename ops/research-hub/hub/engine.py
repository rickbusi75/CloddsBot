import base64
import datetime as dt
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import uuid
import urllib.parse
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from .budget import Budget
from .collectors import Collectors
from .common import digest, iso, now, number, query, request, stamp
from .markets import eligibility, final_result, simulate_fill
from .store import Store


def parse_decision(raw):
    if isinstance(raw, dict):
        return raw
    text = str(raw).strip()
    text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        # A model may prefix its final object with prose. Accept exactly one
        # decision object, never choose arbitrarily between competing decisions.
        choices = []
        for match in re.finditer(r'\{', text):
            try:
                candidate, _ = json.JSONDecoder().raw_decode(text[match.start():])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and 'action' in candidate and 'reason' in candidate:
                choices.append(candidate)
        if len(choices) != 1:
            raise ValueError('single_decision_object_required') from None
        value = choices[0]
    if not isinstance(value, dict):
        raise ValueError('decision_must_be_object')
    return value


class Engine:
    def __init__(self, store=None, settings=None, env=None, transport=request):
        self.env = env if env is not None else os.environ
        self.root = Path(__file__).resolve().parent.parent
        defaults = json.loads((self.root / 'settings.example.json').read_text())
        supplied = settings
        self.settings_path = Path(self.env.get('HUB_SETTINGS', '/config/settings.json'))
        if supplied is None and self.settings_path.exists():
            supplied = json.loads(self.settings_path.read_text())
        self.settings = {**defaults, **(supplied or {}), 'sources': {**defaults['sources'], **(supplied or {}).get('sources', {})}}
        self.validate_settings()
        self.registry = json.loads((self.root / 'registry.json').read_text())
        self.store = store or Store()
        self.store.init(self.registry)
        self.request = transport
        self.collectors = Collectors(self.store, self.settings, self.registry, self.env, transport)
        self.budget = Budget(self.settings, self.env, transport=transport)

    def validate_settings(self):
        if self.settings.get('execution_mode') != 'paper':
            raise ValueError('live_execution_requires_separate_reviewed_adapter')
        for key, low, high in [('daily_ai_usd', .001, .60), ('monthly_ai_usd', .001, 20),
                               ('max_ai_call_usd', .000001, .05), ('paper_order_usd', .1, 20),
                               ('paper_max_exposure_usd', 1, 200), ('paper_capital', 1, 100000),
                               ('minimum_net_edge', .001, .5), ('max_spread', .001, .5)]:
            if number(self.settings.get(key)) is None or not low <= self.settings[key] <= high:
                raise ValueError('invalid_setting_' + key)
        ZoneInfo(self.settings['timezone'])

    def run(self, kind):
        actions = {'universe': self.collectors.universe, 'collect': self.collectors.collect,
                   'scan': self.quantitative_scan, 'reconcile': self.reconcile,
                   'resolve': self.resolve, 'digest': self.daily_digest,
                   'health': self.monitor, 'registry': self.registry_review}
        if kind not in actions:
            raise ValueError('unknown_job')
        if not self.settings['enabled'] and kind not in ('health', 'digest', 'reconcile', 'registry'):
            return {'status': 'paused'}
        with self.store.job(kind) as result:
            if result is None:
                return {'status': 'already_running'}
            value = actions[kind]()
            result.update(value if isinstance(value, dict) else {'result': value})
            return {'status': 'ok', **result}

    def quantitative_scan(self):
        rows = self.store.rows("SELECT * FROM markets WHERE active AND updated_at>now()-interval '2 days'")
        counts, eligible = {}, []
        for row in rows:
            market = row['data']
            reason = eligibility(market, self.settings)
            counts[reason or 'eligible'] = counts.get(reason or 'eligible', 0) + 1
            if not reason:
                # Ranking is a research priority, not a claimed probability of profit.
                age = min(72, (now() - row['last_researched']).total_seconds() / 3600) if row['last_researched'] else 72
                evidence = self.store.one("SELECT count(*) n FROM evidence WHERE theme=%s AND observed_at>now()-interval '2 days'", (market['theme'],))['n']
                score = age + min(10, math.log10(max(1, market['volume24h']))) + min(5, evidence)
                eligible.append({'market': market['id'], 'theme': market['theme'], 'score': score})
        eligible.sort(key=lambda x: x['score'], reverse=True)
        self.store.put('candidates', {'at': iso(), 'counts': counts, 'items': eligible[:1000]})
        return {'coverage': len(rows), 'counts': counts, 'themes': sorted({x['theme'] for x in eligible})}

    def claim(self):
        if not self.settings['enabled']:
            return {'should_run': False, 'reason': 'paused'}
        owner = self.store.lease('research_cycle', 1200)
        if not owner:
            return {'should_run': False, 'reason': 'agent_already_running'}
        try:
            candidates = self.store.get('candidates', {}).get('items', [])
            last_theme = self.store.get('last_research_theme')
            candidates = sorted(candidates, key=lambda x: (x['theme'] == last_theme, -x['score']))
            for candidate in candidates:
                row = self.store.one('SELECT * FROM markets WHERE id=%s', (candidate['market'],))
                if not row or eligibility(row['data'], self.settings):
                    continue
                if row['last_researched'] and (now() - row['last_researched']).total_seconds() < 12 * 3600:
                    continue
                market = row['data']
                evidence = self.related_evidence(market)
                run_id = uuid.uuid4().hex
                context = {'market': market, 'evidence': evidence, 'created_at': iso(), 'lease_owner': owner,
                           'tools_used': [], 'mode': 'paper', 'observer_reference': self.observer_reference()}
                self.store.execute("INSERT INTO research(id,market_id,theme,status,context) VALUES(%s,%s,%s,'running',%s)",
                                   (run_id, market['id'], market['theme'], Jsonb(context)))
                self.store.execute('UPDATE markets SET last_researched=now() WHERE id=%s', (market['id'],))
                self.store.put('last_research_theme', market['theme'])
                # Never pass the lease owner to the model.
                safe = {k: v for k, v in context.items() if k != 'lease_owner'}
                return {'should_run': True, 'run_id': run_id, 'context': safe,
                        'prompt': json.dumps({'run_id': run_id, **safe}, ensure_ascii=False, default=str)}
            self.store.release('research_cycle', owner)
            return {'should_run': False, 'reason': 'no_new_eligible_candidate'}
        except Exception:
            self.store.release('research_cycle', owner)
            raise

    def related_evidence(self, market):
        words = {w for w in re.findall(r'\w+', market['question'].lower()) if len(w) > 3 and w not in ('will', 'have', 'than', 'before', 'after', 'what', 'this', '2026')}
        rows = self.store.rows("SELECT id,url,title,data,published_at,observed_at,source_id FROM evidence WHERE theme=%s AND observed_at>now()-interval '3 days' ORDER BY observed_at DESC LIMIT 250", (market['theme'],))
        scored = []
        for row in rows:
            overlap = len(words.intersection(re.findall(r'\w+', (row['title'] + ' ' + row['data'].get('text', '')).lower())))
            if overlap:
                scored.append((overlap, row))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [{'id': r['id'], 'url': r['url'], 'title': r['title'], 'text': r['data'].get('text', '')[:3500],
                 'source': r['source_id'], 'published_at': str(r['published_at']) if r['published_at'] else None,
                 'observed_at': str(r['observed_at'])} for _, r in scored[:8]]

    def tool(self, run_id, name):
        row = self.store.one("SELECT * FROM research WHERE id=%s AND status='running'", (run_id,))
        if not row:
            raise ValueError('research_not_running')
        context, market = row['context'], row['context']['market']
        if (now() - row['created_at']).total_seconds() > 1200:
            raise ValueError('research_expired')
        key = 'tool:' + run_id + ':' + name
        cached = self.store.get(key)
        if cached is not None:
            return cached
        if name == 'search_evidence':
            value = self.collectors.search(market['question'], market['theme'])
        elif name == 'specialist_data':
            value = self.collectors.specialized(market)
        elif name == 'compare_forecasts':
            value = self.other_forecasts(market)
        elif name == 'read_book':
            value = []
            for outcome, token in zip(market['outcomes'], market['tokens']):
                data, _ = self.request(query('https://clob.polymarket.com/book', {'token_id': token}))
                bids = sorted(data.get('bids', []), key=lambda x: number(x.get('price'), 0), reverse=True)[:5]
                asks = sorted(data.get('asks', []), key=lambda x: number(x.get('price'), 1))[:5]
                value.append({'outcome': outcome, 'bids': bids, 'asks': asks, 'timestamp': data.get('timestamp'), 'minimum_shares': data.get('min_order_size')})
        elif name == 'recall_decisions':
            value = self.store.rows('SELECT market_id,theme,status,result,finished_at FROM research WHERE market_id=%s AND id<>%s ORDER BY created_at DESC LIMIT 5', (market['id'], run_id))
        else:
            raise ValueError('unknown_research_tool')
        # Keep prompts bounded while preserving a clear truncation marker.
        serialized = json.dumps(value, ensure_ascii=False, default=str)
        if len(serialized) > 18000:
            value = {'truncated': True, 'excerpt': serialized[:18000]}
        self.store.put(key, value)
        self.store.execute("UPDATE research SET context=jsonb_set(context,'{tools_used}',COALESCE(context->'tools_used','[]'::jsonb)||%s) WHERE id=%s", (Jsonb([name]), run_id))
        return value

    def other_forecasts(self, market):
        result = {}
        if self.settings['sources'].get('manifold'):
            try:
                data, _ = self.request(query('https://api.manifold.markets/v0/search-markets', {'term': market['question'], 'limit': 3, 'filter': 'open'}))
                result['manifold'] = [{'question': x.get('question'), 'probability': x.get('probability'), 'url': x.get('url'), 'id': x.get('id'),
                                       'note': 'Correspondance et règles non garanties ; ce consensus est un contexte.'} for x in data]
            except Exception as error:
                result['manifold'] = {'error': type(error).__name__}
        if self.settings['sources'].get('metaculus') and self.env.get('METACULUS_TOKEN'):
            try:
                data, _ = self.request(query('https://www.metaculus.com/api/posts/', {'search': market['question'], 'limit': 3}), headers={'Authorization': 'Token ' + self.env['METACULUS_TOKEN']})
                result['metaculus'] = [{'id': x.get('id'), 'title': x.get('title'), 'question': x.get('question')} for x in data.get('results', [])][:3]
            except Exception as error:
                result['metaculus'] = {'error': type(error).__name__}
        return result

    def complete(self, run_id, raw):
        row = self.store.one('SELECT * FROM research WHERE id=%s', (run_id,))
        if not row:
            raise ValueError('unknown_research')
        if row['status'] != 'running':
            return {'status': row['status'], 'result': row['result'], 'duplicate': True}
        context = row['context']
        try:
            if (now() - row['created_at']).total_seconds() > 1200:
                raise ValueError('research_expired')
            result = parse_decision(raw)
            if result.get('analysis_error'):
                raise RuntimeError('agent_analysis_failed')
            market = context['market']
            probability, lower = number(result.get('probability')), number(result.get('lower_bound'))
            outcome = result.get('outcome')
            if result.get('action') not in ('BUY', 'SELL', 'WAIT') or not isinstance(result.get('reason'), str):
                raise ValueError('invalid_decision_schema')
            if probability is not None:
                if not 0 <= probability <= 1 or outcome not in market['outcomes']:
                    raise ValueError('invalid_forecast')
            known = {x['id'] for x in context['evidence']}
            searched = self.store.get('tool:' + run_id + ':search_evidence', [])
            if isinstance(searched, list):
                known.update(x['evidence_id'] for x in searched if 'evidence_id' in x)
            cited = result.get('evidence_ids', [])
            if not isinstance(cited, list) or any(not isinstance(x, str) or x not in known for x in cited):
                raise ValueError('unknown_evidence_reference')
            if result['action'] != 'WAIT':
                if (not cited or not result.get('counterargument') or probability is None or lower is None
                        or not 0 <= lower <= probability <= 1 or result.get('rules_checked') is not True):
                    # A rejected proposal is a normal business outcome, not a
                    # failed workflow. Preserve what the agent proposed for review.
                    proposal = dict(result)
                    result.update(action='WAIT', proposal=proposal,
                                  validation='trade_hypothesis_incomplete',
                                  reason='Proposition non exécutée : preuves, incertitude ou règles incomplètes. ' + result['reason'])
            if probability is not None:
                baseline = market['prices'][market['outcomes'].index(outcome)]
                self.store.execute('INSERT INTO forecasts(id,market_id,event_id,theme,probability,baseline,outcome,model,strategy) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                                   (run_id, market['id'], market['event_id'], market['theme'], probability, baseline, outcome, self.settings['model'], 'n8n-orchestrator-v1'))
            if result['action'] != 'WAIT':
                result['execution'] = self.paper_decision(run_id, market, result)
            self.store.execute("UPDATE research SET status='complete',finished_at=now(),result=%s WHERE id=%s", (Jsonb(result), run_id))
            return {'status': 'complete', 'result': result}
        except Exception as error:
            self.store.execute("UPDATE research SET status='error',finished_at=now(),error=%s WHERE id=%s", (type(error).__name__, run_id))
            raise
        finally:
            self.store.release('research_cycle', context['lease_owner'])

    def paper_portfolio(self, connection=None):
        if connection:
            rows = connection.execute('SELECT * FROM paper_positions').fetchall()
        else:
            rows = self.store.rows('SELECT * FROM paper_positions')
        cash = self.settings['paper_capital'] - sum(x['cost'] + x['fees'] for x in rows) + sum(x['payout'] or 0 for x in rows if x['closed_at'])
        opened = [x for x in rows if not x['closed_at']]
        realized = sum((x['payout'] or 0) - x['cost'] - x['fees'] for x in rows if x['closed_at'])
        market_value = sum(x['shares'] * x['mark'] for x in opened)
        return {'capital': self.settings['paper_capital'], 'cash': cash, 'exposure': sum(x['cost'] + x['fees'] for x in opened),
                'market_value': market_value, 'equity': cash + market_value, 'realized': realized,
                'unrealized': sum(x['shares'] * x['mark'] - x['cost'] - x['fees'] for x in opened), 'open': len(opened)}

    def fee_rate(self, market):
        if market.get('fees_enabled') is False:
            return 0.0
        condition = market.get('condition_id')
        if not condition:
            raise ValueError('fee_schedule_unknown')
        data, _ = self.request('https://clob.polymarket.com/clob-markets/' + urllib.parse.quote(condition, safe=''))
        fd = data.get('fd', {})
        rate = number(fd.get('r'))
        if rate is None or not 0 <= rate <= .1 or number(fd.get('e')) != 1:
            raise ValueError('fee_schedule_unsupported')
        return rate

    def paper_decision(self, research_id, market, decision):
        if self.settings['execution_mode'] != 'paper':
            raise ValueError('live_dispatch_unavailable')
        outcome = decision['outcome']
        token = market['tokens'][market['outcomes'].index(outcome)]
        intent = digest([research_id, market['id'], outcome, decision['action']])
        owner = self.store.lease('paper_account', 120)
        if not owner:
            return {'status': 'account_busy'}
        try:
            existing = self.store.one('SELECT * FROM order_intents WHERE research_id=%s', (research_id,))
            if existing:
                return {'status': existing['status'], 'intent_id': existing['id'], 'duplicate': True}
            book, _ = self.request(query('https://clob.polymarket.com/book', {'token_id': token}))
            if str(book.get('asset_id')) != token:
                raise ValueError('book_asset_mismatch')
            rate = self.fee_rate(market)
            portfolio = self.paper_portfolio()
            if decision['action'] == 'SELL':
                return self.paper_sell(intent, research_id, market, decision, book, rate)
            duplicate = self.store.one('SELECT id FROM paper_positions WHERE event_id=%s AND closed_at IS NULL', (market['event_id'],))
            if duplicate:
                return {'status': 'event_already_exposed'}
            dollars = min(self.settings['paper_order_usd'], self.settings['paper_max_exposure_usd'] - portfolio['exposure'], portfolio['cash']) / (1 + rate)
            limit = min(decision['lower_bound'], decision['probability'] - .03) - self.settings['minimum_net_edge']
            fill = simulate_fill(book, dollars, limit, self.settings['max_spread'])
            if not fill:
                return {'status': 'price_or_depth_not_suitable'}
            fees = fill['shares'] * rate * fill['price'] * (1 - fill['price'])
            edge = min(decision['lower_bound'], decision['probability'] - .03) - fill['price'] - fees / fill['shares']
            if edge < self.settings['minimum_net_edge']:
                return {'status': 'net_edge_insufficient'}
            value = {**fill, 'fees': fees, 'net_edge_estimate': edge, 'token': token, 'hypothetical': True}
            with self.store.connect() as connection:
                with connection.transaction():
                    connection.execute("INSERT INTO order_intents(id,research_id,market_id,event_id,outcome,side,mode,status,data) VALUES(%s,%s,%s,%s,%s,'BUY','paper','filled',%s)",
                                       (intent, research_id, market['id'], market['event_id'], outcome, Jsonb(value)))
                    connection.execute('INSERT INTO paper_positions(id,intent_id,market_id,event_id,outcome,token,shares,cost,fees,mark) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                                       (intent, intent, market['id'], market['event_id'], outcome, token, fill['shares'], fill['cost'], fees, fill['mark']))
            return {'status': 'paper_filled', 'intent_id': intent, **value}
        finally:
            self.store.release('paper_account', owner)

    def paper_sell(self, intent, research_id, market, decision, book, rate):
        position = self.store.one('SELECT * FROM paper_positions WHERE market_id=%s AND outcome=%s AND closed_at IS NULL', (market['id'], decision['outcome']))
        timestamp = number(book.get('timestamp'))
        if not position or timestamp is None or not -5 <= now().timestamp() - timestamp / 1000 <= 60:
            return {'status': 'owned_position_or_fresh_book_missing'}
        remaining, proceeds, fees = position['shares'], 0., 0.
        for level in sorted(book.get('bids', []), key=lambda x: number(x.get('price'), 0), reverse=True):
            price, size = number(level.get('price'), 0), number(level.get('size'), 0)
            if not 0 < price < 1 or size <= 0:
                continue
            quantity = min(remaining, size)
            proceeds += quantity * price
            fees += quantity * rate * price * (1 - price)
            remaining -= quantity
            if remaining < 1e-8:
                break
        if remaining > 1e-8:
            return {'status': 'sale_depth_insufficient'}
        value = {'proceeds': proceeds, 'fees': fees, 'shares': position['shares'], 'hypothetical': True}
        with self.store.connect() as connection:
            with connection.transaction():
                connection.execute("INSERT INTO order_intents(id,research_id,market_id,event_id,outcome,side,mode,status,data) VALUES(%s,%s,%s,%s,%s,'SELL','paper','filled',%s)",
                                   (intent, research_id, market['id'], market['event_id'], decision['outcome'], Jsonb(value)))
                connection.execute('UPDATE paper_positions SET payout=%s,fees=fees+%s,closed_at=now() WHERE id=%s', (proceeds, fees, position['id']))
        return {'status': 'paper_sold', **value}

    def resolve(self):
        forecasts = self.store.rows('SELECT * FROM forecasts WHERE result IS NULL ORDER BY at LIMIT 40')
        positions = self.store.rows('SELECT * FROM paper_positions WHERE closed_at IS NULL')
        ids = list(dict.fromkeys(x['market_id'] for x in forecasts + positions))[:30]
        resolved = 0
        for market_id in ids:
            try:
                market, _ = self.request('https://gamma-api.polymarket.com/markets/' + market_id)
                for f in [x for x in forecasts if x['market_id'] == market_id]:
                    result = final_result(market, f['outcome'], market_id)
                    if result is not None:
                        self.store.execute('UPDATE forecasts SET result=%s,resolved_at=now() WHERE id=%s', (result, f['id']))
                        resolved += 1
                for p in [x for x in positions if x['market_id'] == market_id]:
                    result = final_result(market, p['outcome'], market_id)
                    if result is not None:
                        self.store.execute('UPDATE paper_positions SET payout=%s,mark=%s,closed_at=now() WHERE id=%s', (p['shares'] * result, result, p['id']))
            except Exception:
                continue
        return {'markets_checked': len(ids), 'forecasts_resolved': resolved}

    def account_get(self, path):
        env = self.env
        if not all(env.get(k) for k in ('POLY_API_KEY', 'POLY_API_SECRET', 'POLY_API_PASSPHRASE', 'POLY_SIGNER_ADDRESS')):
            raise RuntimeError('account_read_credentials_missing')
        url = 'https://clob.polymarket.com' + path
        timestamp = str(int(now().timestamp()))
        secret = env['POLY_API_SECRET']
        signature = base64.urlsafe_b64encode(hmac.new(base64.urlsafe_b64decode(secret + '=' * (-len(secret) % 4)),
                                                     (timestamp + 'GET' + urllib.parse.urlsplit(url).path).encode(), hashlib.sha256).digest()).decode()
        return self.request(url, method='GET', headers={'POLY_ADDRESS': env['POLY_SIGNER_ADDRESS'], 'POLY_API_KEY': env['POLY_API_KEY'],
                           'POLY_PASSPHRASE': env['POLY_API_PASSPHRASE'], 'POLY_TIMESTAMP': timestamp, 'POLY_SIGNATURE': signature})[0]

    def account(self):
        balance = self.account_get('/balance-allowance?asset_type=COLLATERAL&signature_type=' + self.env.get('POLY_SIGNATURE_TYPE', '3'))
        orders, trades = [], []
        for path, target in [('/data/orders', orders), ('/data/trades', trades)]:
            cursor, seen = 'MA==', set()
            for _ in range(10):
                if cursor in seen:
                    raise ValueError('account_pagination_loop')
                seen.add(cursor)
                data = self.account_get(path + '?next_cursor=' + urllib.parse.quote(cursor))
                if not isinstance(data.get('data'), list):
                    raise ValueError('account_schema_changed')
                target.extend(data['data'])
                cursor = data.get('next_cursor')
                if cursor == 'LTE=':
                    break
            else:
                raise ValueError('account_pagination_incomplete')
        positions = []
        for offset in range(0, 5000, 500):
            page, _ = self.request(query('https://data-api.polymarket.com/positions', {'user': self.env['POLY_FUNDER_ADDRESS'], 'sizeThreshold': '0', 'limit': 500, 'offset': offset}))
            if not isinstance(page, list):
                raise ValueError('account_position_schema_changed')
            positions.extend(page)
            if len(page) < 500:
                break
        else:
            raise ValueError('account_positions_incomplete')
        normalized = [{k: p.get(k) for k in ['title', 'outcome', 'size', 'avgPrice', 'curPrice', 'currentValue', 'cashPnl', 'realizedPnl', 'redeemable', 'asset', 'conditionId']} for p in positions]
        return {'at': iso(), 'cash': number(balance.get('balance'), 0) / 1e6, 'positions': normalized,
                'open_orders': len(orders), 'trades': len(trades), 'valuation': 'API indicative, pas une liquidation garantie',
                'position_value': sum(number(x.get('currentValue'), 0) for x in positions), 'complete': True}

    def reconcile(self):
        errors = []
        for p in self.store.rows('SELECT * FROM paper_positions WHERE closed_at IS NULL'):
            try:
                book, _ = self.request(query('https://clob.polymarket.com/book', {'token_id': p['token']}))
                bids = [number(x.get('price')) for x in book.get('bids', [])]
                bids = [x for x in bids if x is not None and 0 <= x <= 1]
                if str(book.get('asset_id')) != p['token'] or not bids:
                    raise ValueError('invalid_mark')
                self.store.execute('UPDATE paper_positions SET mark=%s WHERE id=%s', (max(bids), p['id']))
            except Exception:
                errors.append(p['id'])
        try:
            account = self.account()
            self.store.execute('INSERT INTO account_snapshots(data) VALUES(%s)', (Jsonb(account),))
            self.store.put('account_error', None)
        except Exception as error:
            self.store.put('account_error', {'at': iso(), 'error': type(error).__name__})
        self.store.put('paper_mark_errors', errors)
        return {'paper_mark_errors': len(errors), 'account_error': self.store.get('account_error')}

    def observer_reference(self):
        path = Path(self.env.get('OBSERVER_LEDGER', '/observer/observer.sqlite')).parent / 'status.json'
        try:
            data = json.loads(path.read_text())
            return {k: data.get(k) for k in ('at', 'mode', 'portfolio', 'forecasts', 'lastRun')}
        except (OSError, ValueError):
            return {'status': 'unavailable'}

    def report(self):
        p = self.paper_portfolio()
        account = self.store.one('SELECT at,data FROM account_snapshots ORDER BY id DESC LIMIT 1')
        lines = ['CloddsBot + n8n — bilan quotidien', 'Recherche multi-thèmes • nouvelle chaîne en simulation']
        if account:
            a = account['data']
            lines += [f"Compte réel (lecture) : {a['cash']:.2f} pUSD disponibles ; positions {a['position_value']:.2f}",
                      f"Ordres ouverts : {a['open_orders']} ; positions : {len(a['positions'])}",
                      'État du compte daté : ' + str(account['at'])]
        else:
            lines += ['Compte réel : lecture non encore disponible.']
        if self.store.get('account_error'):
            lines += ['Lecture actuelle du compte indisponible : dernier état ci-dessus non confirmé.']
        lines += [f"Simulation n8n : {p['equity']:.2f}/{p['capital']:.2f} ; réalisé {p['realized']:+.2f} ; latent {p['unrealized']:+.2f}",
                  f"Engagé simulé : {p['exposure']:.2f} ; positions : {p['open']}"]
        counts = self.store.rows("SELECT theme,count(*) n FROM research WHERE created_at>now()-interval '1 day' GROUP BY theme ORDER BY n DESC")
        lines += ['Analyses du jour : ' + (', '.join(f"{x['theme']} {x['n']}" for x in counts) or 'aucune')]
        latest = self.store.one("SELECT result FROM research WHERE status='complete' ORDER BY finished_at DESC LIMIT 1")
        if latest:
            lines += ['Dernière décision : ' + str(latest['result'].get('reason', ''))[:450]]
        try:
            budget = self.budget.usage()
            lines += [f"IA partagée (journal) : {budget['day']:.4f}/{budget['daily_limit']:.2f} $ jour ; {budget['month']:.4f}/{budget['monthly_limit']:.2f} $ mois"]
        except Exception:
            lines += ['Budget IA partagé : lecture indisponible.']
        problems = self.store.rows("SELECT id,status FROM sources WHERE status IN ('error','configuration_required')")
        if problems:
            lines += ['Sources à vérifier/configurer : ' + ', '.join(x['id'] for x in problems)]
        lines += ['Aucun ordre réel soumis par cette chaîne. Le P&L net réel complet reste à rapprocher des frais et mouvements externes.']
        return '\n'.join(lines)[:3900]

    def daily_digest(self):
        local = now().astimezone(ZoneInfo(self.settings['timezone']))
        if not self.settings.get('digest_enabled') or local.hour != self.settings['digest_hour']:
            return {'delivery': 'not_scheduled'}
        key = 'daily:' + local.date().isoformat()
        self.store.execute('INSERT INTO notifications(key,text) VALUES(%s,%s) ON CONFLICT DO NOTHING', (key, self.report()))
        claimed = self.store.one("UPDATE notifications SET status='sending',attempts=attempts+1 WHERE key=%s AND status='pending' RETURNING text", (key,))
        if not claimed:
            return {'delivery': 'already_attempted'}
        if not self.env.get('TELEGRAM_BOT_TOKEN') or not self.settings.get('telegram_chat_id'):
            self.store.execute("UPDATE notifications SET status='pending',error='telegram_configuration_missing' WHERE key=%s", (key,))
            return {'delivery': 'configuration_required'}
        try:
            data, _ = self.request('https://api.telegram.org/bot' + self.env['TELEGRAM_BOT_TOKEN'] + '/sendMessage',
                                   body={'chat_id': self.settings['telegram_chat_id'], 'text': claimed['text'], 'disable_web_page_preview': True})
            if data.get('ok') is not True:
                raise RuntimeError('telegram_rejected')
            self.store.execute("UPDATE notifications SET status='sent',sent_at=now(),error=NULL WHERE key=%s", (key,))
            return {'delivery': 'sent'}
        except Exception:
            # Do not blindly retry an ambiguous send and duplicate the user's digest.
            self.store.execute("UPDATE notifications SET status='uncertain',error='delivery_not_confirmed' WHERE key=%s", (key,))
            raise

    def monitor(self):
        stale = self.store.rows("SELECT id,status FROM sources WHERE status='error' OR (last_success<now()-interval '2 days' AND status='ok')")
        expired = self.store.execute("UPDATE research SET status='expired',finished_at=now(),error='agent_timeout' WHERE status='running' AND created_at<now()-interval '25 minutes'")
        return {'source_issues': stale, 'expired_research': expired, 'at': iso()}

    def registry_review(self):
        return {'sources': len(self.registry), 'configuration_required': [x['id'] for x in self.registry if x.get('key') and not self.env.get(x['key'])],
                'note': 'Revue de configuration ; aucune nouvelle API installée automatiquement.'}

    def status(self):
        try:
            budget = self.budget.usage()
        except Exception:
            budget = {'status': 'unavailable'}
        sources = self.store.rows('SELECT id,data,status,last_success,last_error,checked_at FROM sources ORDER BY id')
        for s in sources:
            s['configuration_present'] = not s['data'].get('key') or bool(self.env.get(s['data']['key']))
            s['enabled'] = self.settings['sources'].get(s['id'], False)
        return {'at': iso(), 'mode': 'paper', 'enabled': self.settings['enabled'], 'budget': budget,
                'sources': sources, 'paper': self.paper_portfolio(), 'observer': self.observer_reference(),
                'universe': self.store.get('universe_last_complete'), 'universe_progress': self.store.get('universe_progress'),
                'themes': self.store.rows('SELECT theme,count(*) markets FROM markets WHERE active GROUP BY theme ORDER BY markets DESC'),
                'research': self.store.rows('SELECT id,market_id,theme,status,result,error,created_at FROM research ORDER BY created_at DESC LIMIT 10'),
                'jobs': self.store.rows('SELECT kind,status,started_at,finished_at,result,error FROM jobs ORDER BY started_at DESC LIMIT 12'),
                'account': self.store.one('SELECT at,data FROM account_snapshots ORDER BY id DESC LIMIT 1'),
                'account_error': self.store.get('account_error'),
                'evaluations': self.store.one('SELECT count(*) tracked,count(result) resolved,avg((probability-result)^2) brier,avg((baseline-result)^2) market_brier FROM forecasts'),
                'live_dispatch': False}
