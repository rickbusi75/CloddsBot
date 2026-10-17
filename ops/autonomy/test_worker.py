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

    def test_cumulative_reservation_survives_restart_timeout_and_shared_key_spend(self):
        info = {'usage_daily': 0, 'usage_monthly': 0}
        for _ in range(11):
            self.observer.reserve(.05, info)
        with self.assertRaises(RuntimeError):
            self.observer.reserve(.051, info)
        with self.assertRaises(RuntimeError):
            self.observer.reserve(.05, {'usage_daily': .59, 'usage_monthly': 0})
        with self.assertRaises(RuntimeError):
            self.observer.reserve(.05, {'usage_daily': 0, 'usage_monthly': 19.99})
        other = worker.Observer(self.tmp.name)
        self.addCleanup(other.db.close)
        self.assertAlmostEqual(other.ledger_cost(worker.utcnow().isoformat()[:10]), .55)

    def test_bad_controls_fail_closed(self):
        (Path(self.tmp.name) / 'control.json').write_text('{broken')
        self.assertTrue(self.observer.controls()['paused'])

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
        with patch.object(worker, 'request', side_effect=lambda url, **kw: (self.book() if '/book?' in url else {'base_fee': 0}, {})):
            for i in range(6):
                market = {'id': str(i), 'event': str(i), 'tokens': ['yes', 'no'], 'question': 'Example'}
                self.assertEqual(self.observer.paper_order(market, evidence, decision), 'PAPER_FILLED')
                self.assertEqual(self.observer.paper_order(market, evidence, decision), 'ALREADY_EXPOSED')
            self.assertLessEqual(self.observer.portfolio()['exposure'], 30 + 1e-8)
            market = {'id': 'new', 'event': 'new', 'tokens': ['yes', 'no'], 'question': 'Example'}
            self.assertEqual(self.observer.paper_order(market, evidence, decision), 'ABSTAIN_PRICE_DEPTH')
        self.observer.snapshot()
        self.assertEqual(json.loads((Path(self.tmp.name) / 'status.json').read_text())['mode'], 'paper')
        (Path(self.tmp.name) / 'control.json').write_text('{"paused":true}')
        self.assertEqual(self.observer.paper_order(market, evidence, decision), 'PAUSED')

    def test_closed_is_not_settled_and_resolved_binary_payout_is(self):
        db = self.observer.db
        db.execute('INSERT INTO positions VALUES(1,"m","e","yes","YES","Example",10,5,.1,.49,NULL,?,NULL)', (worker.utcnow().isoformat(),))
        db.commit()
        with patch.object(worker, 'request', side_effect=lambda url, **kw: ({'closed': True, 'outcomePrices': '[".99",".01"]'} if '/markets/' in url else self.book(), {})):
            self.observer.reconcile()
        self.assertEqual(self.observer.portfolio()['open'], 1)
        with patch.object(worker, 'request', return_value=({'umaResolutionStatus': 'resolved', 'outcomePrices': '["1","0"]'}, {})):
            self.observer.reconcile()
        self.assertEqual(self.observer.portfolio()['open'], 0)
        self.assertAlmostEqual(self.observer.portfolio()['realized'], 4.9)
        self.assertAlmostEqual(self.observer.portfolio()['cash'], 204.9)

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
