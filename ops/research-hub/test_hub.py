import datetime as dt
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from hub.budget import Budget
from hub.collectors import rss_items
from hub.engine import parse_decision
from hub.markets import devig, final_result, normalize, simulate_fill, theme_of
from workflows import generate


class MarketTests(unittest.TestCase):
    def test_version_two_uses_position_ids(self):
        m = normalize({'id': 1, 'version': 'v2', 'outcomes': '["Yes","No"]', 'outcomePrices': '["0.4","0.6"]', 'positionIds': ['11', '22'], 'clobTokenIds': '["old","bad"]', 'question': 'Bitcoin above 100k?'})
        self.assertEqual(m['tokens'], ['11', '22'])
        self.assertEqual(m['theme'], 'crypto')

    def test_theme_diversity(self):
        examples = {'sports': 'NBA playoffs winner?', 'weather': 'Highest temperature in Chicago?', 'geopolitics': 'Ceasefire by December?',
                    'politics': 'Republican governor in Ohio?', 'economics': 'CPI inflation above 3%?', 'disasters': 'Magnitude 8 earthquake?', 'science': 'NASA rocket launch?', 'business': 'Nvidia earnings beat?'}
        for expected, question in examples.items():
            with self.subTest(question=question):
                self.assertEqual(theme_of(question), expected)

    def test_devig_removes_margin(self):
        p = devig([{'name': 'home', 'price': 1.8}, {'name': 'away', 'price': 1.8}])
        self.assertEqual(p, {'home': .5, 'away': .5})

    def test_insufficient_depth_is_not_full_fill(self):
        book = {'timestamp': 1000000, 'min_order_size': 1, 'bids': [{'price': '.49', 'size': '100'}], 'asks': [{'price': '.5', 'size': '1'}]}
        self.assertIsNone(simulate_fill(book, 5, .6, at=dt.datetime.fromtimestamp(1000, dt.timezone.utc)))

    def test_stale_book_rejected(self):
        book = {'timestamp': 1000, 'min_order_size': 1, 'bids': [{'price': '.49', 'size': '100'}], 'asks': [{'price': '.5', 'size': '100'}]}
        self.assertIsNone(simulate_fill(book, 5, .6))

    def test_fill_uses_executable_asks(self):
        at = dt.datetime.fromtimestamp(1000, dt.timezone.utc)
        book = {'timestamp': 1000000, 'min_order_size': 1, 'bids': [{'price': '.49', 'size': '100'}], 'asks': [{'price': '.5', 'size': '4'}, {'price': '.51', 'size': '100'}]}
        result = simulate_fill(book, 5, .6, at=at)
        self.assertAlmostEqual(result['cost'], 5)
        self.assertGreater(result['price'], .5)

    def test_resolution_requires_correct_market_and_final_status(self):
        m = {'id': '1', 'umaResolutionStatus': 'resolved', 'outcomes': '["Yes","No"]', 'outcomePrices': '["1","0"]'}
        self.assertEqual(final_result(m, 'Yes', '1'), 1)
        self.assertIsNone(final_result(m, 'Yes', '2'))
        self.assertIsNone(final_result({**m, 'umaResolutionStatus': 'proposed'}, 'Yes', '1'))
        self.assertEqual(final_result({**m, 'version': 'v2', 'umaResolutionStatus': None, 'resolutionStatus': 'resolved'}, 'Yes', '1'), 1)

    def test_rss_preserves_publication_date(self):
        data = rss_items('<rss><channel><item><title>A</title><link>https://example.com/a</link><description>x</description><pubDate>Thu, 22 Oct 2026 18:06:17 -0400</pubDate></item></channel></rss>', 'politics')
        self.assertTrue(data[0]['published_at'].startswith('2026-10-22'))

    def test_decision_requires_object(self):
        self.assertEqual(parse_decision('```json\n{"action":"WAIT"}\n```')['action'], 'WAIT')
        with self.assertRaises(ValueError):
            parse_decision('[]')

    def test_decision_accepts_one_final_object_but_rejects_ambiguity(self):
        self.assertEqual(parse_decision('Analysis\n{"action":"WAIT","reason":"Missing data"}')['action'], 'WAIT')
        with self.assertRaisesRegex(ValueError, 'single_decision_object_required'):
            parse_decision('{"action":"WAIT","reason":"A"}\n{"action":"BUY","reason":"B"}')


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'observer.sqlite')
        with sqlite3.connect(self.path) as c:
            c.execute('CREATE TABLE costs(id INTEGER PRIMARY KEY,at TEXT,reserved REAL,actual REAL,state TEXT)')
            c.execute('CREATE TABLE kv(key TEXT PRIMARY KEY,value TEXT)')
        self.settings = {'daily_ai_usd': .06, 'monthly_ai_usd': 1, 'max_ai_call_usd': .05}
        self.budget = Budget(self.settings, {}, self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_atomic_shared_reservations(self):
        accepted = []
        def reserve():
            try:
                self.budget.reserve(.04, {'usage_daily': 0, 'usage_monthly': 0})
                accepted.append(True)
            except RuntimeError:
                accepted.append(False)
        threads = [threading.Thread(target=reserve) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(accepted), [False, True])

    def test_unsettled_call_stays_reserved(self):
        self.budget.reserve(.04, {'usage_daily': 0, 'usage_monthly': 0})
        self.assertAlmostEqual(self.budget.usage()['day'], .04)

    def test_real_cost_settlement(self):
        rid = self.budget.reserve(.04, {'usage_daily': 0, 'usage_monthly': 0})
        self.budget.settle(rid, .001)
        self.assertAlmostEqual(self.budget.usage()['day'], .001)

    def test_missing_shared_ledger_not_replaced(self):
        budget = Budget(self.settings, {}, str(Path(self.temp.name) / 'missing.sqlite'))
        with self.assertRaises(RuntimeError):
            budget.usage()

    def test_provider_usage_included(self):
        with self.assertRaises(RuntimeError):
            self.budget.reserve(.04, {'usage_daily': .03, 'usage_monthly': 0})

    def test_overrun_stops_future_shared_calls(self):
        rid = self.budget.reserve(.01, {'usage_daily': 0, 'usage_monthly': 0})
        self.budget.settle(rid, .02)
        with self.assertRaisesRegex(RuntimeError, 'shared_ai_budget_fault'):
            self.budget.reserve(.001, {'usage_daily': 0, 'usage_monthly': 0})


class WorkflowTests(unittest.TestCase):
    def test_native_agents_and_connections(self):
        workflows = generate({'id': 'http-test', 'name': 'HTTP'}, {'id': 'model-test', 'name': 'Model'}, 'test/model')
        self.assertEqual(len(workflows), 9)
        agent = next(w for w in workflows if any(n['type'].endswith('.agent') for n in w['nodes']))
        self.assertEqual(sum(n['type'].endswith('.agentTool') for n in agent['nodes']), 2)
        for workflow in workflows:
            names = {n['name'] for n in workflow['nodes']}
            for source, groups in workflow['connections'].items():
                self.assertIn(source, names)
                for outputs in groups.values():
                    for output in outputs:
                        for connection in output:
                            self.assertIn(connection['node'], names)
        self.assertNotIn('apiKey', json.dumps(workflows))


if __name__ == '__main__':
    unittest.main(verbosity=2)
