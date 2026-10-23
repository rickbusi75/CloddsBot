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
from hub.accounting import cash_summary, positions_value, net_reconciliation, activity_flows
from hub.evidence import relevance
from hub.common import UpstreamError, now, iso
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
        historical = rss_items('<rss><channel><item><title>GDACS</title><link>http://www.gdacs.org/report.aspx?id=1</link></item></channel></rss>', 'disasters')
        self.assertEqual(historical[0]['url'], 'http://www.gdacs.org/report.aspx?id=1')

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

    def test_failed_call_settles_uncertain_at_reserved_amount(self):
        self.settings['model'] = 'fixture/model'
        def provider(url, **kwargs):
            if url.endswith('/models'):
                return {'data': [{'id': 'fixture/model', 'supported_parameters': [], 'pricing': {'prompt': '0.0000001', 'completion': '0.0000002'}}]}, {}
            if url.endswith('/key'):
                return {'data': {'usage_daily': 0, 'usage_monthly': 0}}, {}
            raise UpstreamError(500)
        budget = Budget(self.settings, {'OPENROUTER_API_KEY': 'fixture'}, self.path, transport=provider)
        with self.assertRaises(UpstreamError):
            budget.completion({'model': 'fixture/model', 'max_tokens': 1800, 'messages': [{'role': 'user', 'content': 'fixture'}]})
        with sqlite3.connect(self.path) as c:
            row = c.execute("SELECT state, actual, reserved FROM costs WHERE state LIKE 'hub_%'").fetchone()
        self.assertEqual(row[0], 'hub_uncertain')
        self.assertEqual(row[1], row[2])
        # The uncertain amount keeps counting conservatively in the budget.
        self.assertAlmostEqual(budget.usage()['day'], row[2])

    def test_settle_uncertain_ignores_unknown_reservation(self):
        self.budget.settle_uncertain(999)
        with sqlite3.connect(self.path) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM costs WHERE state='hub_uncertain'").fetchone()[0], 0)

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

    def test_provider_fallback_stays_on_model_and_price_cap_with_rate_backoff(self):
        self.settings['model'] = 'fixture/model'
        calls = []
        def provider(url, **kwargs):
            calls.append(url)
            if url.endswith('/models'):
                return {'data': [{'id': 'fixture/model', 'supported_parameters': ['reasoning'], 'pricing': {'prompt': '0.0000001', 'completion': '0.0000002'}}]}, {}
            if url.endswith('/key'):
                return {'data': {'usage_daily': 0, 'usage_monthly': 0}}, {}
            body = kwargs['body']
            self.assertEqual(body['model'], 'fixture/model')
            self.assertTrue(body['provider']['allow_fallbacks'])
            self.assertEqual(body['max_tokens'], 8192)
            self.assertEqual(body['reasoning'], {'effort': 'low'})
            self.assertAlmostEqual(body['provider']['max_price']['prompt'], .2)
            self.assertAlmostEqual(body['provider']['max_price']['completion'], .4)
            raise UpstreamError(429)
        budget = Budget(self.settings, {'OPENROUTER_API_KEY': 'fixture'}, self.path, transport=provider)
        with self.assertRaises(UpstreamError):
            budget.completion({'model': 'fixture/model', 'max_tokens': 8192, 'messages': [{'role': 'user', 'content': 'fixture'}]})
        self.assertIsNotNone(budget.usage()['provider_retry_at'])
        count = len(calls)
        with self.assertRaisesRegex(RuntimeError, 'model_provider_rate_limited'):
            Budget(self.settings, {'OPENROUTER_API_KEY': 'fixture'}, self.path, transport=provider).completion({'model': 'fixture/model'})
        self.assertEqual(len(calls), count)


class EvidenceAndAccountTests(unittest.TestCase):
    def test_different_subjects_do_not_match_generic_election_or_date_words(self):
        self.assertEqual(relevance('Will Putin be President in December 2026?', 'US midterm election in December 2026')['score'], 0)
        self.assertEqual(relevance('Will Anthropic reach a valuation in October?', 'Rocket Lab reaches new valuation in October')['score'], 0)
        self.assertGreater(relevance('Will Putin be President in December?', 'Putin discusses election outcome')['score'], 0)

    def test_subject_matching_handles_accents_and_generic_only_questions(self):
        self.assertGreater(relevance('Will Vučić become Prime Minister?', 'Vucic announces plan')['score'], 0)
        self.assertEqual(relevance('Will the next President win?', 'Next president wins')['score'], 0)
        self.assertGreater(relevance('Will inflation be above 3%?', 'Inflation rises')['score'], 0)

    def test_cash_excludes_remaining_buy_orders_and_deduplicates_pages(self):
        buy = {'id': 'buy', 'side': 'BUY', 'price': '.4', 'original_size': '10', 'size_matched': '3'}
        sell = {'id': 'sell', 'side': 'SELL', 'price': '.7', 'originalSize': '4', 'sizeMatched': '0'}
        value = cash_summary({'balance': '20000000'}, [buy, buy.copy(), sell])
        self.assertEqual(value['cash'], 20)
        self.assertEqual(value['reserved_buys'], 2.8)
        self.assertEqual(value['available_cash'], 17.2)
        self.assertEqual(value['open_orders'], 2)

    def test_unknown_balance_or_order_cannot_become_available_zero(self):
        for value in [None, True, 'NaN', 'Infinity', '-1', '.5', 'not-a-number']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                cash_summary({'balance': value}, [])
        order = {'id': 'buy', 'side': 'BUY', 'price': '.4', 'original_size': '10'}
        with self.assertRaisesRegex(ValueError, 'account_open_order_matched_unknown'):
            cash_summary({'balance': '20000000'}, [order])
        with self.assertRaisesRegex(ValueError, 'account_open_order_changed_during_read'):
            cash_summary({'balance': '20000000'}, [{**order, 'size_matched': '1'}, {**order, 'size_matched': '2'}])

    def test_position_value_requires_each_asset_and_numeric_valuation(self):
        self.assertEqual(positions_value([{'asset': 'a', 'size': '4.4', 'currentValue': '1.782'}]), 1.782)
        for rows in [[{'asset': 'a', 'size': 1}], [{'asset': 'a', 'size': 1, 'currentValue': 1}]*2]:
            with self.assertRaises(ValueError):
                positions_value(rows)

    def test_net_reconciliation_residual_math(self):
        # One BUY: deposits = cash + outflow.
        result = net_reconciliation('100.0', [{'side': 'BUY', 'usdcSize': '10'}])
        self.assertEqual(result['deposits'], 110.0)
        self.assertEqual(result['trade_outflows'], 10.0)
        self.assertEqual(result['trade_inflows'], 0.0)
        self.assertTrue(result['pnl_complete'])

        # Mixed flows: BUY and SELL, plus splits (out), merges and redeems (in).
        result = net_reconciliation(
            '100.0',
            [{'side': 'BUY', 'usdcSize': '10'}, {'side': 'SELL', 'usdcSize': '5'}],
            splits=['5'], merges=['3'], redeems=['4'])
        self.assertEqual(result['deposits'], 103.0)
        self.assertEqual(result['trade_outflows'], 10.0)
        self.assertEqual(result['event_outflows'], 5.0)
        self.assertEqual(result['trade_inflows'], 5.0)
        self.assertEqual(result['event_inflows'], 7.0)
        self.assertTrue(result['pnl_complete'])

        # Deposits negative (cash less than known outflows) still reported, not zeroed.
        result = net_reconciliation('2.0', [{'side': 'BUY', 'usdcSize': '10'}])
        self.assertEqual(result['deposits'], 12.0)
        self.assertTrue(result['pnl_complete'])

        # Invalid inputs must fail loudly, never silently zero.
        with self.assertRaisesRegex(ValueError, 'reconciliation_trades_shape'):
            net_reconciliation('100.0', 'not-a-list')
        with self.assertRaisesRegex(ValueError, 'reconciliation_trade_side_unknown'):
            net_reconciliation('100.0', [{'side': 'HOLD', 'usdcSize': '5'}])
        with self.assertRaisesRegex(ValueError, 'reconciliation_trade_amount_unknown'):
            net_reconciliation('100.0', [{'side': 'BUY'}])

    def test_activity_flows_grouping_and_validation(self):
        rows = [
            {'type': 'TRADE', 'side': 'BUY', 'usdcSize': '2.06'},
            {'type': 'SPLIT', 'usdcSize': '1.0'},
            {'type': 'MERGE', 'usdcSize': '0.5'},
            {'type': 'REDEEM', 'usdcSize': '4.0'},
        ]
        flows = activity_flows(rows)
        self.assertEqual(flows['trades'], [{'side': 'BUY', 'usdcSize': '2.06'}])
        self.assertEqual(flows['splits'], ['1.0'])
        self.assertEqual(flows['merges'], ['0.5'])
        self.assertEqual(flows['redeems'], ['4.0'])

        # Reconciliation on these flows: deposits = cash + out - in.
        recon = net_reconciliation('18.23', flows['trades'], flows['splits'], flows['merges'], flows['redeems'])
        self.assertAlmostEqual(recon['deposits'], 18.23 + 2.06 + 1.0 - 0.5 - 4.0, places=4)

        # Unknown activity type must fail loudly, never be ignored.
        with self.assertRaisesRegex(ValueError, 'reconciliation_activity_type_unknown'):
            activity_flows([{'type': 'REWARD', 'usdcSize': '1'}])

        # Unknown trade side must fail loudly.
        with self.assertRaisesRegex(ValueError, 'reconciliation_trade_side_unknown'):
            activity_flows([{'type': 'TRADE', 'side': 'HOLD', 'usdcSize': '1'}])

        # Rows without a numeric usdcSize are skipped, not converted to zero.
        self.assertEqual(activity_flows([{'type': 'SPLIT', 'usdcSize': None}]), {'trades': [], 'splits': [], 'merges': [], 'redeems': []})

        # Non-dict rows fail loudly.
        with self.assertRaisesRegex(ValueError, 'reconciliation_activity_shape'):
            activity_flows(['not-a-dict'])


class WorkflowTests(unittest.TestCase):
    def test_native_agents_and_connections(self):
        workflows = generate({'id': 'http-test', 'name': 'HTTP'}, {'id': 'model-test', 'name': 'Model'}, 'test/model')
        self.assertEqual(len(workflows), 10)
        agent = next(w for w in workflows if any(n['type'].endswith('.agent') for n in w['nodes']))
        self.assertEqual(sum(n['type'].endswith('.agentTool') for n in agent['nodes']), 2)
        decision = next(n for n in agent['nodes'] if n['name'] == 'Enregistrer la décision finale')
        targets = agent['connections'][decision['name']]['ai_tool'][0]
        self.assertEqual([t['node'] for t in targets], ['Orchestrateur'])
        self.assertIn('$fromAI', decision['parameters']['jsonBody'])
        self.assertIn('"decision_json"', decision['parameters']['jsonBody'])
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
