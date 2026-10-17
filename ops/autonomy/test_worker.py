import datetime as dt
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
import worker
import watchdog


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.observer = worker.Observer(self.tmp.name)
        self.addCleanup(self.observer.db.close)

    def book(self):
        return {'asset_id': 'yes', 'timestamp': str(int(worker.time.time() * 1000)),
                'min_order_size': '5', 'asks': [{'price': '.5', 'size': '100'}],
                'bids': [{'price': '.49', 'size': '100'}]}

    def test_partial_depth_and_minimum_and_staleness(self):
        b = self.book()
        self.assertAlmostEqual(worker.fill(b, 3, .55)['shares'], 6)
        self.assertIsNone(worker.fill(b, 2, .55))
        b['asks'][0]['size'] = '5'
        self.assertEqual(worker.fill(b, 3, .55)['cost'], 2.5)
        self.assertIsNone(worker.fill(b, 3, .49))
        b['timestamp'] = '1'
        self.assertIsNone(worker.fill(b, 3, .55))
        b['timestamp'] = 'NaN'
        self.assertIsNone(worker.fill(b, 3, .55))

    def test_protocol_fees_and_unknown_schedules(self):
        self.assertEqual(worker.taker_fee_rate({'fd': {'r': .05, 'e': 1, 'to': True}}), .05)
        self.assertEqual(worker.taker_fee_rate({'fd': {'r': 0, 'e': 1}}), 0)
        for info in ({}, {'fd': {'r': .05, 'e': 2}}, {'fd': {'r': 'NaN', 'e': 1}}, {'fd': {'r': -.01, 'e': 1}}):
            with self.assertRaises(RuntimeError):
                worker.taker_fee_rate(info)

    def test_v2_outcome_identifiers_take_precedence_over_legacy_ctf(self):
        market = {'id': 'm', 'active': True, 'closed': False, 'acceptingOrders': True, 'enableOrderBook': True,
                  'endDate': (worker.utcnow() + dt.timedelta(days=2)).isoformat(), 'outcomePrices': '[\".5\",\".5\"]',
                  'outcomes': '[\"Yes\",\"No\"]', 'clobTokenIds': '[\"old-yes\",\"old-no\"]',
                  'positionIds': ['new-yes', 'new-no'], 'version': 'v2', 'liquidityNum': 2000, 'volume24hr': 2000, 'question': 'Example'}
        self.assertEqual(worker.normalize_market(market)['tokens'], ['new-yes', 'new-no'])
        market['version'] = 'v1'
        self.assertEqual(worker.normalize_market(market)['tokens'], ['old-yes', 'old-no'])
        market['version'] = 'unknown'
        self.assertIsNone(worker.normalize_market(market))

    def test_cumulative_reservation_survives_restart_timeout_and_shared_key_spend(self):
        info = {'usage_daily': 0, 'usage_monthly': 0}
        for _ in range(11):
            self.observer.reserve(.05, info)
        with self.assertRaises(RuntimeError):
            self.observer.reserve(.051, info)
        with self.assertRaises(RuntimeError):
            self.observer.reserve(.05, {'usage_daily': .59, 'usage_monthly': 0})
        with self.assertRaises(RuntimeError):
            self.observer.reserve(.01, {'usage_daily': .10, 'usage_monthly': 0})
        with self.assertRaises(RuntimeError):
            self.observer.reserve(.01, {'usage_daily': -1, 'usage_monthly': 0})
        with self.assertRaises(RuntimeError):
            self.observer.reserve(.05, {'usage_daily': 0, 'usage_monthly': 19.99})
        other = worker.Observer(self.tmp.name)
        self.addCleanup(other.db.close)
        self.assertAlmostEqual(other.ledger_cost(worker.utcnow().isoformat()[:10]), .55)

    def test_bad_controls_fail_closed(self):
        for invalid in ('{broken', '[]', 'null', 'true', '1', '{"paused":"false"}'):
            (Path(self.tmp.name) / 'control.json').write_text(invalid)
            self.assertTrue(self.observer.controls()['paused'])

    def test_hourly_single_run_and_two_daily_digests_follow_paris_including_dst(self):
        (Path(self.tmp.name) / 'control.json').write_text('{"paused":true}')
        for instant in ('2026-10-17T07:05:00+00:00', '2026-10-17T18:05:00+00:00', '2026-10-25T08:05:00+00:00'):
            with patch.object(worker, 'utcnow', return_value=dt.datetime.fromisoformat(instant)), \
                 patch.object(self.observer, 'reconcile'), patch.object(self.observer, 'deliver'):
                self.observer.tick()
                self.observer.tick()
        keys = [r[0] for r in self.observer.db.execute('SELECT key FROM outbox ORDER BY key')]
        self.assertEqual(keys, ['digest:2026-10-17:09', 'digest:2026-10-17:20', 'digest:2026-10-25:09'])
        self.assertEqual(self.observer.db.execute('SELECT COUNT(*) FROM runs').fetchone()[0], 3)
        self.assertEqual(self.observer.db.execute('SELECT COUNT(*) FROM costs').fetchone()[0], 0)

    def test_watchdog_alerts_after_three_failures_and_once_on_recovery(self):
        state = {}
        for i in range(3):
            state, message = watchdog.transition(state, True, worker.utcnow())
            self.assertEqual(bool(message), i == 2)
        state, message = watchdog.transition(state, True, worker.utcnow())
        self.assertIsNone(message)
        state, message = watchdog.transition(state, False, worker.utcnow())
        self.assertIsNotNone(message)
        state, message = watchdog.transition(state, False, worker.utcnow())
        self.assertIsNone(message)

    def test_vpn_recovery_respects_mode_manual_stop_and_hourly_cooldown(self):
        now = worker.utcnow()
        state = {'failures': 3}
        vpn = {'Running': True, 'Health': {'Status': 'unhealthy'}}
        bot = {'Status': 'running'}
        self.assertTrue(watchdog.recovery_allowed(state, 'vpn', vpn, bot, 'active', now))
        for mode, service in [('direct', 'active'), ('unknown', 'active'), ('vpn', 'inactive')]:
            self.assertFalse(watchdog.recovery_allowed(state, mode, vpn, bot, service, now))
        self.assertFalse(watchdog.recovery_allowed(state, 'vpn', vpn, {'Status': 'exited'}, 'activating', now))
        self.assertFalse(watchdog.recovery_allowed({'failures': 2}, 'vpn', vpn, bot, 'active', now))
        self.assertFalse(watchdog.recovery_allowed({'failures': 3, 'lastRecoveryAttempt': now.isoformat()}, 'vpn', vpn, bot, 'active', now))
        self.assertFalse(watchdog.recovery_allowed(state, 'vpn', {'Running': True, 'Health': {'Status': 'healthy'}}, bot, 'active', now))
        with patch.object(watchdog.subprocess, 'run', side_effect=subprocess.TimeoutExpired('docker', 15)):
            self.assertEqual(watchdog.container_state('cloddsbot-bot-1'), {})

    @unittest.skipUnless(sys.platform == 'linux', 'Linux daemon lifecycle')
    def test_daemon_singleton_stop_and_restart_retain_pause_and_ledger(self):
        root = Path(self.tmp.name)
        (root / 'control.json').write_text('{"paused":true}')
        env = root / 'empty.env'
        config = root / 'config.json'
        env.write_text('')
        config.write_text('{}')
        command = [sys.executable, str(Path(worker.__file__).resolve())]
        options = ['--data', str(root), '--env', str(env), '--config', str(config)]
        self.observer.reserve(.01, {'usage_daily': 0, 'usage_monthly': 0})
        for _ in range(2):
            child = subprocess.Popen(command + ['daemon'] + options, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                for _ in range(100):
                    pidfile = root / 'worker.pid'
                    if pidfile.exists() and pidfile.read_text() == str(child.pid):
                        break
                    time.sleep(.02)
                self.assertEqual(pidfile.read_text(), str(child.pid))
                subprocess.run(command + ['ready'] + options, check=True, timeout=15)
                duplicate = subprocess.run(command + ['daemon'] + options, capture_output=True, timeout=5)
                self.assertNotEqual(duplicate.returncode, 0)
                subprocess.run(command + ['stop'] + options, check=True, timeout=10)
                child.wait(timeout=5)
                self.assertTrue(self.observer.controls()['paused'])
                self.assertAlmostEqual(self.observer.ledger_cost(worker.utcnow().isoformat()[:10]), .01)
            finally:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=5)
    def test_sources_are_untrusted_unknown_or_duplicate_domains_abstain(self):
        market = {'id': 'm', 'event': 'event', 'tokens': ['yes', 'no'], 'question': 'Example'}
        evidence = [{'url': 'https://a.com/one'}, {'url': 'https://b.com/two'}]
        decision = {'decision': 'CANDIDATE', 'outcome': 'YES', 'probability': .9, 'lowerBound': .8,
                    'rulesClear': True, 'independentPrimarySources': True, 'counterargument': 'Could fail',
                    'sources': ['https://a.com/one', 'https://evil.example/fake']}
        with patch.object(worker, 'request', side_effect=AssertionError('No network permitted')):
            self.assertEqual(self.observer.paper_order(market, evidence, decision), 'ABSTAIN_EVIDENCE')

    def test_paper_accounting_deduplication_exposure_restart_and_pause(self):
        evidence = [{'url': 'https://a.com/one'}, {'url': 'https://b.com/two'}]
        decision = {'decision': 'CANDIDATE', 'outcome': 'YES', 'probability': .9, 'lowerBound': .8,
                    'rulesClear': True, 'independentPrimarySources': True, 'counterargument': 'Could fail',
                    'sources': [x['url'] for x in evidence]}
        with patch.object(worker, 'request', side_effect=lambda url, **kw: (self.book() if '/book?' in url else {'fd': {'r': .05, 'e': 1, 'to': True}}, {})):
            for i in range(6):
                market = {'id': str(i), 'conditionId': 'condition', 'event': str(i), 'tokens': ['yes', 'no'], 'question': 'Example'}
                self.assertEqual(self.observer.paper_order(market, evidence, decision), 'PAPER_FILLED')
                self.assertEqual(self.observer.paper_order(market, evidence, decision), 'ALREADY_EXPOSED')
            self.assertLessEqual(self.observer.portfolio()['exposure'], 30 + 1e-8)
            market = {'id': 'new', 'conditionId': 'condition', 'event': 'new', 'tokens': ['yes', 'no'], 'question': 'Example'}
            self.assertEqual(self.observer.paper_order(market, evidence, decision), 'ABSTAIN_PRICE_DEPTH')
        self.observer.snapshot()
        self.assertEqual(json.loads((Path(self.tmp.name) / 'status.json').read_text())['mode'], 'paper')
        (Path(self.tmp.name) / 'control.json').write_text('{"paused":true}')
        self.assertEqual(self.observer.paper_order(market, evidence, decision), 'PAUSED')

    def test_closed_is_not_settled_and_resolved_binary_payout_is(self):
        db = self.observer.db
        db.execute('INSERT INTO positions VALUES(1,"m","e","yes","YES","Example",10,5,.1,.49,NULL,?,NULL)', (worker.utcnow().isoformat(),))
        db.commit()
        with patch.object(worker, 'request', side_effect=lambda url, **kw: ({'id': 'm', 'outcomes': '["Yes","No"]', 'closed': True, 'outcomePrices': '[".99",".01"]'} if '/markets/' in url else self.book(), {})):
            self.observer.reconcile()
        self.assertEqual(self.observer.portfolio()['open'], 1)
        with patch.object(worker, 'request', return_value=({'id': 'm', 'outcomes': '["Yes","No"]', 'umaResolutionStatus': 'resolved', 'outcomePrices': '["1","0"]'}, {})):
            self.observer.reconcile()
        self.assertEqual(self.observer.portfolio()['open'], 0)
        self.assertAlmostEqual(self.observer.portfolio()['realized'], 4.9)
        self.assertAlmostEqual(self.observer.portfolio()['cash'], 204.9)

    def test_abstained_forecast_scan_restart_final_outcome_and_market_comparison(self):
        market = {'id': 'm', 'conditionId': 'c', 'active': True, 'closed': False,
                  'acceptingOrders': True, 'enableOrderBook': True, 'question': 'Example',
                  'endDate': (worker.utcnow() + dt.timedelta(days=2)).isoformat(),
                  'outcomes': '["Yes","No"]', 'outcomePrices': '[".4",".6"]',
                  'clobTokenIds': '["yes","no"]', 'liquidityNum': 2000, 'volume24hr': 2000}
        decision = {'decision': 'ABSTAIN', 'outcome': 'NO', 'probability': .8, 'lowerBound': .7,
                    'reason': 'Uncertain evidence', 'sources': []}
        with patch.object(worker, 'request', return_value=([market], {})), \
             patch.object(self.observer, 'evidence', return_value=[{'url': 'https://a.test'}, {'url': 'https://b.test'}]), \
             patch.object(self.observer, 'analyze', return_value=decision):
            self.assertEqual(self.observer.scan(), 'ok')
            self.observer.scan()  # Same forecast cannot be inserted twice in the daily interval.
        other = worker.Observer(self.tmp.name)
        self.addCleanup(other.db.close)
        self.assertEqual(other.forecast_metrics()['tracked'], 1)
        self.assertEqual(other.forecast_metrics()['resolved'], 0)
        self.assertEqual(other.portfolio()['open'], 0)
        final = {'id': 'm', 'conditionId': 'c', 'outcomes': '["No","Yes"]',
                 'outcomePrices': '["1","0"]', 'umaResolutionStatus': 'resolved'}
        with patch.object(worker, 'request', return_value=(final, {})) as mocked:
            other.evaluate_forecasts()
            other.evaluate_forecasts()
            self.assertEqual(mocked.call_count, 1)
        metrics = other.forecast_metrics()
        self.assertEqual((metrics['resolved'], metrics['markets']), (1, 1))
        self.assertAlmostEqual(metrics['brier'], .04)
        self.assertAlmostEqual(metrics['marketBrier'], .16)
        self.assertIn('Brier IA 0.0400', other.report())
        self.assertEqual(other.portfolio()['equity'], 200)

    def test_final_result_requires_matching_market_condition_labels_and_finality(self):
        market = {'id': 'm', 'conditionId': 'c', 'outcomes': '["No","Yes"]',
                  'outcomePrices': '["0","1"]', 'umaResolutionStatus': 'resolved'}
        self.assertEqual(worker.resolved_result(market, 'NO', 'm', 'c'), 0)
        for key, value in [('umaResolutionStatus', 'proposed'), ('outcomePrices', '[".01",".99"]'),
                           ('outcomes', '["Team A","Team B"]')]:
            self.assertIsNone(worker.resolved_result({**market, key: value}, 'YES', 'm', 'c'))
        for identity, condition in [('other', 'c'), ('m', 'other')]:
            with self.assertRaises(ValueError):
                worker.resolved_result(market, 'YES', identity, condition)

    def test_forecast_checks_are_bounded_fair_cached_and_before_cutoff_daily(self):
        decision = {'outcome': 'YES', 'probability': .6}
        for i in range(12):
            market = {'id': str(i // 2), 'end': (worker.utcnow() + dt.timedelta(days=2)).isoformat(), 'prices': [.5, .5]}
            self.observer.record_forecast(i, market, decision, 'ABSTAIN_EVIDENCE')
        self.observer.db.commit()
        def response(url, **kw):
            return {'id': url.rsplit('/', 1)[1], 'outcomes': '["Yes","No"]', 'outcomePrices': '[".5",".5"]'}, {}
        with patch.object(worker, 'request', side_effect=response) as mocked:
            self.observer.evaluate_forecasts()
            self.assertEqual(self.observer.get('forecastEvaluation')['checked'], 10)
            self.assertEqual(mocked.call_count, 5)  # Two forecasts per market share a public read.
            self.observer.evaluate_forecasts()
            self.assertEqual(self.observer.get('forecastEvaluation')['checked'], 2)
            self.assertEqual(mocked.call_count, 6)
            self.observer.evaluate_forecasts()
            self.assertEqual(mocked.call_count, 6)
        self.assertEqual(self.observer.forecast_metrics()['resolved'], 0)

    def test_unavailable_position_does_not_block_other_settlement_and_blocks_new_risk(self):
        db = self.observer.db
        for i in range(2):
            db.execute('INSERT INTO positions VALUES(?,?,?,?,?,?,?,?,?,?,NULL,?,NULL)',
                       (i, str(i), str(i), 'yes', 'YES', 'Example', 10, 5, .1, .49, worker.utcnow().isoformat()))
        db.commit()
        def response(url, **kw):
            identity = url.rsplit('/', 1)[1]
            if identity == '0':
                raise RuntimeError('HTTP 503')
            return {'id': identity, 'outcomes': '["Yes","No"]',
                    'outcomePrices': '["1","0"]', 'umaResolutionStatus': 'resolved'}, {}
        with patch.object(worker, 'request', side_effect=response):
            self.observer.reconcile()
        self.assertEqual(self.observer.portfolio()['open'], 1)
        self.assertAlmostEqual(self.observer.portfolio()['realized'], 4.9)
        self.assertEqual(self.observer.get('reconciliation')['unavailable'], 1)
        with patch.object(worker, 'request', side_effect=AssertionError('No new risk allowed')):
            self.assertEqual(self.observer.paper_order({}, [], {}), 'PAPER_MARKS_UNAVAILABLE')
        self.assertIn('valeur indicative', self.observer.report())

    def test_paper_fill_loss_and_daily_circuit_breaker_survive_restart(self):
        evidence = [{'url': 'https://a.com/one'}, {'url': 'https://b.com/two'}]
        decision = {'decision': 'CANDIDATE', 'outcome': 'NO', 'probability': .9, 'lowerBound': .8,
                    'rulesClear': True, 'independentPrimarySources': True, 'counterargument': 'Could fail',
                    'sources': [e['url'] for e in evidence]}
        with patch.object(worker, 'request', side_effect=lambda url, **kw: ({**self.book(), 'asset_id': 'no'} if '/book?' in url else {'fd': {'r': 0, 'e': 1}}, {})):
            for i in range(3):
                market = {'id': str(i), 'conditionId': 'c', 'event': str(i), 'tokens': ['yes', 'no'], 'question': 'Example'}
                self.assertEqual(self.observer.paper_order(market, evidence, decision), 'PAPER_FILLED')
        def final(url, **kw):
            return {'id': url.rsplit('/', 1)[1], 'outcomes': '["Yes","No"]',
                    'outcomePrices': '["1","0"]', 'umaResolutionStatus': 'resolved'}, {}
        with patch.object(worker, 'request', side_effect=final):
            self.observer.reconcile()
        other = worker.Observer(self.tmp.name)
        self.addCleanup(other.db.close)
        self.assertEqual(other.portfolio()['cash'], 185)
        self.assertEqual(other.portfolio()['dailyLoss'], 15)
        self.assertEqual(other.portfolio()['open'], 0)
        with patch.object(worker, 'request', side_effect=AssertionError('Daily loss stop')):
            self.assertEqual(other.paper_order({}, [], {}), 'PAPER_RISK_STOP')

    def test_malformed_source_lists_abstain_without_crashing(self):
        for sources in ('https://example.com', [{'url': 'https://example.com'}], None):
            with patch.object(worker, 'request', side_effect=AssertionError('No request')):
                self.assertEqual(self.observer.paper_order({}, [], {'sources': sources}), 'ABSTAIN_EVIDENCE')

    def test_outbox_retry_then_dedup(self):
        self.observer.env = {'TELEGRAM_BOT_TOKEN': 'test'}
        self.observer.config = {'channels': {'telegram': {'allowFrom': ['123']}}}
        self.observer.enqueue('report', 'Test')
        self.observer.enqueue('report', 'Duplicate')
        with patch.object(worker, 'request', side_effect=RuntimeError('Network failure')):
            with self.assertRaises(RuntimeError):
                self.observer.deliver()
        with patch.object(worker, 'request', return_value=({'ok': True}, {})) as mock:
            self.observer.deliver()
            self.observer.deliver()
            self.assertEqual(mock.call_count, 1)

    def test_private_sources_blocked(self):
        for url in ('http://example.com', 'https://localhost', 'https://127.0.0.1', 'https://example.com:8080'):
            with self.assertRaises(ValueError):
                worker.public_url(url)


if __name__ == '__main__':
    unittest.main()
