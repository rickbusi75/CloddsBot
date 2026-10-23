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
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit, urlunsplit

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from hub.common import iso, now, UpstreamError
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

    def test_staged_decision_survives_truncated_final_prose_without_early_fill(self):
        run = self.claim()
        staged = self.engine.stage_decision(run['run_id'], self.decision())
        self.assertEqual(staged['execution'], 'not_submitted')
        self.assertEqual(self.engine.paper_portfolio()['open'], 0)
        self.assertEqual(self.store.one('SELECT count(*) n FROM forecasts')['n'], 0)
        result = self.engine.complete(run['run_id'], 'Analyse terminée mais texte tronqué...')
        self.assertEqual(result['result']['decision_origin'], 'agent_decision_tool')
        self.assertEqual(result['result']['execution']['status'], 'paper_filled')
        self.assertTrue(self.engine.complete(run['run_id'], {'analysis_error': True})['duplicate'])
        self.assertEqual(self.engine.paper_portfolio()['open'], 1)

    def test_staged_decision_is_immutable_and_invalid_evidence_can_be_corrected(self):
        run = self.claim()
        with self.assertRaisesRegex(ValueError, 'unknown_evidence_reference'):
            self.engine.stage_decision(run['run_id'], {**self.decision(), 'evidence_ids': ['invented']})
        self.assertIsNone(self.store.get('decision:' + run['run_id']))
        self.engine.stage_decision(run['run_id'], self.decision('WAIT'))
        self.engine.stage_decision(run['run_id'], self.decision('WAIT'))
        with self.assertRaisesRegex(ValueError, 'decision_already_staged'):
            self.engine.stage_decision(run['run_id'], self.decision())
        self.assertEqual(self.engine.complete(run['run_id'], {'analysis_error': True})['result']['action'], 'WAIT')
        self.assertEqual(self.engine.paper_portfolio()['open'], 0)

    def test_expiration_and_pause_still_apply_to_staged_buy(self):
        run = self.claim()
        self.engine.stage_decision(run['run_id'], self.decision())
        control = Path(self.engine.env['OBSERVER_LEDGER']).parent / 'control.json'
        control.write_text('{"paused":true}')
        self.assertEqual(self.engine.complete(run['run_id'], 'Done')['result']['execution']['status'], 'paused')
        self.assertEqual(self.engine.paper_portfolio()['open'], 0)
        control.write_text('{"paused":false}')
        self.store.execute('UPDATE markets SET last_researched=NULL')
        run = self.claim()
        self.store.execute("UPDATE research SET created_at=now()-interval '21 minutes' WHERE id=%s", (run['run_id'],))
        with self.assertRaisesRegex(ValueError, 'research_not_running_or_expired'):
            self.engine.stage_decision(run['run_id'], self.decision())

    def test_failure_keeps_specific_validation_and_excerpt_without_forecast(self):
        run = self.claim()
        with self.assertRaisesRegex(ValueError, 'single_decision_object_required'):
            self.engine.complete(run['run_id'], 'No structured decision available')
        row = self.store.one('SELECT status,error,result FROM research WHERE id=%s', (run['run_id'],))
        self.assertEqual(row['error'], 'single_decision_object_required')
        self.assertEqual(row['result']['output_excerpt'], 'No structured decision available')
        self.assertEqual(self.store.one('SELECT count(*) n FROM forecasts')['n'], 0)

    def test_persisted_provider_backoff_skips_claim_before_call_or_lease(self):
        with sqlite3.connect(self.engine.env['OBSERVER_LEDGER']) as c:
            c.execute("INSERT INTO kv VALUES('hubProviderRetryAt',?)", (json.dumps(iso(now()+dt.timedelta(minutes=5))),))
        self.assertEqual(self.engine.claim()['reason'], 'model_provider_rate_limited')
        self.assertEqual(self.store.one('SELECT count(*) n FROM research')['n'], 0)
        self.assertEqual(self.store.one('SELECT count(*) n FROM leases')['n'], 0)

    def test_related_evidence_excludes_other_subjects_in_same_theme(self):
        good = self.store.evidence('fixture', {'url': 'https://example.com/putin', 'title': 'Putin election update', 'text': 'Putin speaks', 'theme': 'politics'})
        self.store.evidence('fixture', {'url': 'https://example.com/us', 'title': 'US president in December 2026', 'text': 'Election plans', 'theme': 'politics'})
        evidence = self.engine.related_evidence({'question': 'Will Putin be President in December 2026?', 'theme': 'politics'})
        self.assertEqual([x['id'] for x in evidence], [good])
        self.assertEqual(evidence[0]['matched_terms'], ['putin'])

    def test_empty_search_is_cached_and_does_not_repeat_requests(self):
        self.engine.env['SEARXNG_URL'] = 'http://searxng:8080'
        calls = []
        def empty(url, **kwargs):
            calls.append(url)
            return {'results': []}, {}
        self.engine.collectors.request = empty
        self.assertEqual(self.engine.collectors.search('Empty fixture', 'politics'), [])
        self.assertEqual(self.engine.collectors.search('Empty fixture', 'politics'), [])
        self.assertEqual(len(calls), 1)

    def test_stale_book_does_not_replace_mark_and_bad_balance_keeps_last_snapshot(self):
        run = self.claim()
        self.engine.complete(run['run_id'], self.decision())
        before = self.store.one('SELECT mark FROM paper_positions')['mark']
        self.book.update(timestamp=1000, bids=[{'price': '.9', 'size': 1000}])
        self.store.execute('INSERT INTO account_snapshots(data) VALUES(%s)', (Jsonb({'fixture': True}),))
        self.engine.env['POLY_FUNDER_ADDRESS'] = 'fixture'
        def account(path):
            return {} if path.startswith('/balance-') else {'data': [], 'next_cursor': 'LTE='}
        def reads(url, **kwargs):
            return (self.book.copy() if '/book?' in url else []), {}
        with patch.object(self.engine, 'account_get', side_effect=account), patch.object(self.engine, 'request', side_effect=reads):
            result = self.engine.reconcile()
        self.assertIsNotNone(result['account_error'])
        self.assertEqual(result['paper_mark_errors'], 1)
        self.assertEqual(self.store.one('SELECT count(*) n FROM account_snapshots')['n'], 1)
        self.assertEqual(self.store.one('SELECT mark FROM paper_positions')['mark'], before)

    def test_position_review_gets_original_thesis_and_hourly_cooldown(self):
        run = self.claim()
        self.engine.complete(run['run_id'], self.decision())
        review = self.engine.claim('positions')
        self.assertTrue(review['should_run'])
        self.assertEqual(review['context']['position']['thesis']['action'], 'BUY')
        self.assertEqual(review['context']['purpose'], 'positions')
        result = self.engine.complete(review['run_id'], {**self.decision(), 'action': 'WAIT'})
        self.assertEqual(result['result']['action'], 'WAIT')
        self.assertFalse(self.engine.claim('positions')['should_run'])

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

    def prepare_digest(self):
        self.engine.settings.update(digest_enabled=True, telegram_chat_id='123')
        self.engine.env['TELEGRAM_BOT_TOKEN'] = 'test'
        with sqlite3.connect(self.engine.env['OBSERVER_LEDGER']) as c:
            c.execute("INSERT OR REPLACE INTO kv VALUES('digestOwner','\"research-hub\"')")

    def test_digest_owner_and_dst_deduplicate_across_restart(self):
        self.prepare_digest()
        instant = dt.datetime.fromisoformat('2026-10-25T19:05:00+00:00')
        with patch('hub.reporting.now', return_value=instant), patch.object(self.engine, 'request', return_value=({'ok': True, 'result': {'message_id': 123}}, {})) as send:
            self.assertEqual(self.engine.daily_digest()['delivery'], 'sent')
            self.assertEqual(self.engine.daily_digest()['delivery'], 'already_attempted')
            restarted = Engine(self.store, settings=self.engine.settings, env=self.engine.env, transport=self.transport)
            self.assertEqual(restarted.daily_digest()['delivery'], 'already_attempted')
            self.assertEqual(send.call_count, 1)
        row = self.store.one('SELECT * FROM notifications')
        self.assertEqual(row['key'], 'daily:2026-10-25')
        self.assertEqual(row['provider_message_id'], '123')

    def test_digest_does_not_send_without_ownership_or_outside_hour(self):
        self.prepare_digest()
        with sqlite3.connect(self.engine.env['OBSERVER_LEDGER']) as c:
            c.execute("UPDATE kv SET value='\"observer\"' WHERE key='digestOwner'")
        with patch('hub.reporting.now', return_value=dt.datetime.fromisoformat('2026-10-23T18:10:00+00:00')):
            self.assertEqual(self.engine.daily_digest()['delivery'], 'not_owner')
        with patch('hub.reporting.now', return_value=dt.datetime.fromisoformat('2026-10-23T17:10:00+00:00')):
            self.assertEqual(self.engine.daily_digest()['delivery'], 'not_scheduled')
        self.assertEqual(self.store.one('SELECT count(*) n FROM notifications')['n'], 0)

    def test_uncertain_digest_is_not_blindly_retried(self):
        self.prepare_digest()
        instant = dt.datetime.fromisoformat('2026-10-23T18:05:00+00:00')
        with patch('hub.reporting.now', return_value=instant), patch.object(self.engine, 'request', side_effect=RuntimeError('response_lost')) as send:
            with self.assertRaises(RuntimeError):
                self.engine.daily_digest()
            self.assertEqual(self.engine.daily_digest()['delivery'], 'already_attempted')
            self.assertEqual(send.call_count, 1)
        self.assertEqual(self.store.one('SELECT status FROM notifications')['status'], 'uncertain')

    def test_interrupted_sender_becomes_uncertain(self):
        self.prepare_digest()
        self.store.execute("INSERT INTO notifications(key,text,status,attempted_at) VALUES('fixture','pending','sending',now()-interval '20 minutes')")
        self.assertEqual(self.engine.monitor()['uncertain_notifications'], 1)
        self.assertEqual(self.store.one('SELECT status FROM notifications')['status'], 'uncertain')

    def test_rate_limited_source_waits_before_requesting_again(self):
        self.engine.settings['sources'] = {'gdelt': True}
        self.store.source_status('gdelt', 'error', 'upstream_http_429')
        with patch.object(self.engine.collectors, 'request', side_effect=AssertionError('No retry during backoff')):
            result = self.engine.collectors.collect(force=True)
        self.assertEqual(result['gdelt']['deferred'], 'provider_rate_limit')

    def test_brave_failure_falls_back_with_correct_provenance(self):
        self.engine.env.update(BRAVE_SEARCH_API_KEY='test', SEARXNG_URL='http://searxng:8080')
        def search(url, **kwargs):
            if 'api.search.brave.com' in url:
                raise UpstreamError(429)
            if 'searxng:8080' in url:
                return {'results': [{'url': 'https://example.com/news', 'title': 'Primary report', 'content': 'Relevant news'}]}, {}
            return '<html>Primary report contents</html>', {}
        self.engine.collectors.request = search
        result = self.engine.collectors.search('Fixture question', 'politics')
        self.assertEqual(len(result), 1)
        self.assertEqual(self.store.one('SELECT source_id FROM evidence WHERE id=%s', (result[0]['evidence_id'],))['source_id'], 'searxng')
        self.assertEqual(self.store.one("SELECT last_error FROM sources WHERE id='brave'")['last_error'], 'upstream_http_429')
        self.assertEqual(self.store.one("SELECT status FROM sources WHERE id='searxng'")['status'], 'ok')

    def test_shared_pause_blocks_inflight_buys_but_keeps_settlement_and_sales(self):
        run = self.claim()
        self.engine.complete(run['run_id'], self.decision())
        control = Path(self.engine.env['OBSERVER_LEDGER']).parent / 'control.json'
        control.write_text('{"paused":true}')
        self.assertFalse(self.engine.claim()['should_run'])
        self.assertEqual(self.engine.run('scan')['status'], 'paused')
        with patch.object(self.engine, 'request', side_effect=AssertionError('No entry after pause')):
            self.assertEqual(self.engine.paper_decision('inflight-buy', self.market, self.decision())['status'], 'paused')
        self.book.update(bids=[{'price': '.7', 'size': '1000'}], asks=[{'price': '.71', 'size': '1000'}])
        self.assertEqual(self.engine.paper_decision('exit-while-paused', self.market, self.decision('SELL'))['status'], 'paper_sold')
        self.assertEqual(self.engine.run('resolve')['status'], 'ok')

    def test_bad_shared_control_does_not_resume_research(self):
        control = Path(self.engine.env['OBSERVER_LEDGER']).parent / 'control.json'
        for value in ['{broken', '{"paused":"false"}', '[]']:
            control.write_text(value)
            self.assertTrue(self.engine.paused())
        control.write_text('{"paused":false}')
        self.assertFalse(self.engine.paused())


if __name__ == '__main__':
    unittest.main(verbosity=2)
