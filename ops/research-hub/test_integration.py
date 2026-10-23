"""Real PostgreSQL transactions; external APIs mocked, no production data mounted.

Run inside the built image on the private database network. This suite creates
and drops only the explicitly named research_validation database.
"""
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit, urlunsplit

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from hub.common import iso, now
from hub.engine import Engine
from hub.markets import normalize
from hub.store import Store


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database = 'research_validation'
        parts = urlsplit(os.environ['DATABASE_URL'])
        cls.admin_dsn = os.environ['DATABASE_URL']
        cls.dsn = urlunsplit(parts._replace(path='/' + cls.database))
        with psycopg.connect(cls.admin_dsn, autocommit=True) as c:
            if c.execute('SELECT 1 FROM pg_database WHERE datname=%s', (cls.database,)).fetchone():
                raise RuntimeError('validation_database_already_exists_do_not_overwrite')
            c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(cls.database)))

    @classmethod
    def tearDownClass(cls):
        with psycopg.connect(cls.admin_dsn, autocommit=True) as c:
            c.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(cls.database)))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        ledger = str(Path(self.temp.name) / 'observer.sqlite')
        with sqlite3.connect(ledger) as c:
            c.executescript('CREATE TABLE costs(id INTEGER PRIMARY KEY,at TEXT,reserved REAL,actual REAL,state TEXT); CREATE TABLE kv(key TEXT PRIMARY KEY,value TEXT);')
        self.store = Store(self.dsn)
        self.store.init([])
        with self.store.connect() as c:
            tables = [r['tablename'] for r in c.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall()]
            for name in tables:
                c.execute(sql.SQL('TRUNCATE TABLE {} RESTART IDENTITY').format(sql.Identifier(name)))
        self.book = {'timestamp': int(now().timestamp()*1000), 'min_order_size': 1,
                     'asset_id': 'yes', 'bids': [{'price': '.39', 'size': '1000'}],
                     'asks': [{'price': '.4', 'size': '1000'}]}
        self.engine = Engine(self.store, settings={'paper_capital': 20, 'paper_max_exposure_usd': 10},
                             env={'OBSERVER_LEDGER': ledger}, transport=self.transport)
        self.market = normalize({'id': '100', 'active': True, 'acceptingOrders': True, 'enableOrderBook': True,
                                 'question': 'Will inflation be above 3%?', 'liquidityNum': 10000, 'volume24hr': 1000,
                                 'description': 'The official release for this exact period determines the result. Revisions are excluded.',
                                 'endDate': (now()+dt.timedelta(days=20)).isoformat(), 'conditionId': 'condition',
                                 'outcomes': ['Yes', 'No'], 'outcomePrices': [.4, .6], 'clobTokenIds': ['yes', 'no'],
                                 'feesEnabled': True, 'events': [{'id': 'event'}]})
        self.store.execute('INSERT INTO markets(id,event_id,theme,question,data,version) VALUES(%s,%s,%s,%s,%s,%s)',
                           ('100', 'event', 'economics', self.market['question'], Jsonb(self.market), 'v1'))
        self.evidence = self.store.evidence('bls', {'url': 'https://example.com/release', 'title': 'Inflation release',
                                                 'text': 'Inflation for the stated period, test fixture.', 'theme': 'economics'})

    def tearDown(self):
        self.temp.cleanup()

    def transport(self, url, **kwargs):
        if '/book?' in url:
            return self.book.copy(), {}
        if '/clob-markets/' in url:
            return {'fd': {'r': .05, 'e': 1}}, {}
        if '/markets/100' in url:
            return {'id': '100', 'umaResolutionStatus': 'resolved', 'outcomes': ['Yes', 'No'], 'outcomePrices': [1, 0]}, {}
        raise AssertionError('Unexpected external transport')

    def decision(self, action='BUY'):
        return {'action': action, 'outcome': 'Yes', 'probability': .9, 'lower_bound': .85,
                'rules_checked': True, 'reason': 'Test fixture', 'counterargument': 'Measurement may change',
                'invalidation': 'New official release', 'evidence_ids': [self.evidence]}

    def claim(self):
        self.engine.quantitative_scan()
        result = self.engine.claim()
        self.assertTrue(result['should_run'])
        return result

    def test_one_concurrent_claim_and_durable_decision(self):
        self.engine.quantitative_scan()
        claims = []
        threads = [threading.Thread(target=lambda: claims.append(self.engine.claim())) for _ in range(2)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(sum(x['should_run'] for x in claims), 1)
        run = next(x for x in claims if x['should_run'])
        self.assertNotIn('lease_owner', run['prompt'])
        self.engine.complete(run['run_id'], {**self.decision(), 'action': 'WAIT'})
        again = self.engine.complete(run['run_id'], self.decision())
        self.assertTrue(again['duplicate'])
        self.assertEqual(self.engine.paper_portfolio()['open'], 0)

    def test_buy_sell_fees_cash_and_duplicate(self):
        run = self.claim()
        result = self.engine.complete(run['run_id'], self.decision())
        self.assertEqual(result['result']['execution']['status'], 'paper_filled')
        position = self.store.one('SELECT * FROM paper_positions')
        self.assertGreater(position['fees'], 0)
        self.assertLessEqual(position['cost']+position['fees'], 5)
        self.engine.complete(run['run_id'], self.decision())
        self.assertEqual(self.store.one('SELECT count(*) n FROM paper_positions')['n'], 1)
        self.book.update(bids=[{'price': '.7', 'size': '1000'}], asks=[{'price': '.71', 'size': '1000'}])
        sold = self.engine.paper_decision('sale-fixture', self.market, self.decision('SELL'))
        self.assertEqual(sold['status'], 'paper_sold')
        self.assertTrue(self.engine.paper_decision('sale-fixture', self.market, self.decision('SELL'))['duplicate'])
        p = self.engine.paper_portfolio()
        self.assertEqual(p['open'], 0)
        self.assertGreater(p['realized'], 0)
        self.assertAlmostEqual(p['cash'], p['capital'] + p['realized'])

    def test_correlated_event_and_exposure_not_duplicated(self):
        run = self.claim()
        self.engine.complete(run['run_id'], self.decision())
        rejected = self.engine.paper_decision('second-research', self.market, self.decision())
        self.assertEqual(rejected['status'], 'event_already_exposed')
        self.assertLessEqual(self.engine.paper_portfolio()['exposure'], 10)

    def test_resolution_and_brier_use_actual_outcome(self):
        run = self.claim()
        self.engine.complete(run['run_id'], self.decision())
        result = self.engine.resolve()
        self.assertEqual(result['forecasts_resolved'], 1)
        self.assertEqual(self.engine.paper_portfolio()['open'], 0)
        score = self.engine.status()['evaluations']
        self.assertEqual(score['resolved'], 1)
        self.assertAlmostEqual(score['brier'], .01)

    def test_fabricated_evidence_cannot_open_position(self):
        run = self.claim()
        with self.assertRaisesRegex(ValueError, 'unknown_evidence_reference'):
            self.engine.complete(run['run_id'], {**self.decision(), 'evidence_ids': ['invented']})
        self.assertEqual(self.engine.paper_portfolio()['open'], 0)
        self.assertEqual(self.store.one('SELECT count(*) n FROM forecasts')['n'], 0)
        self.assertEqual(self.store.one('SELECT status FROM research')['status'], 'error')
        self.assertIsNone(self.store.one("SELECT * FROM leases WHERE name='research_cycle'"))

    def test_incomplete_buy_is_recorded_as_abstention_not_filled(self):
        run = self.claim()
        result = self.engine.complete(run['run_id'], {**self.decision(), 'lower_bound': None})
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['result']['action'], 'WAIT')
        self.assertEqual(result['result']['proposal']['action'], 'BUY')
        self.assertEqual(result['result']['validation'], 'trade_hypothesis_incomplete')
        self.assertEqual(self.engine.paper_portfolio()['open'], 0)

    def test_expired_lease_recovers_without_old_owner_unlocking_new(self):
        owner = self.store.lease('fixture', 60)
        self.store.execute("UPDATE leases SET expires_at=now()-interval '1 second' WHERE name='fixture'")
        replacement = self.store.lease('fixture', 60)
        self.assertNotEqual(owner, replacement)
        self.store.release('fixture', owner)
        self.assertEqual(self.store.one("SELECT owner FROM leases WHERE name='fixture'")['owner'], replacement)

    def test_gamma_continuation_uses_after_cursor_and_both_protocols(self):
        seen = []
        def pages(url, **kwargs):
            params = parse_qs(urlsplit(url).query)
            self.assertEqual(params['version'], ['v1', 'v2'])
            seen.append(params.get('after_cursor'))
            return {'markets': [], 'next_cursor': None} if len(seen) == 2 else {'markets': [{'id': '101', 'question': 'Test'}], 'next_cursor': 'second-page'}, {}
        self.engine.collectors.request = pages
        result = self.engine.collectors.universe()
        self.assertEqual(seen, [None, ['second-page']])
        self.assertTrue(result['complete'])
        self.assertEqual(self.store.get('universe_last_complete')['markets'], 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
