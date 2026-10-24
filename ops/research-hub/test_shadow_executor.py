import copy
import datetime as dt
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from shadow_executor.policy import Blocked, config, plan
from shadow_executor.worker import paused, public_read


class ShadowPolicyTests(unittest.TestCase):
    def setUp(self):
        self.at = dt.datetime(2026, 10, 24, 0, 0, tzinfo=dt.timezone.utc)
        self.p = config({})
        self.row = {'finished_at': self.at, 'context': {'market': {
            'id': '1', 'event_id': 'event', 'condition_id': '0x' + 'a'*64,
            'outcomes': ['Yes', 'No'], 'tokens': ['123', '456'],
            'active': True, 'accepting_orders': True, 'book_enabled': True,
            'end': (self.at + dt.timedelta(days=2)).isoformat()}},
            'result': {'action': 'BUY', 'rules_checked': True, 'evidence_ids': ['proof'],
                       'probability': .9, 'lower_bound': .85, 'outcome': 'Yes'}}
        self.account = {'at': self.at, 'data': {'complete': True, 'cash': 18,
            'available_cash': 18, 'open_orders': 0, 'positions': []}}
        self.book = {'asset_id': '123', 'market': '0x'+'a'*64, 'timestamp': self.at.timestamp()*1000,
                     'tick_size': '.01', 'min_order_size': '1',
                     'bids': [{'price': '.39', 'size': '100'}],
                     'asks': [{'price': '.4', 'size': '100'}]}

    def propose(self, **kwargs):
        return plan(self.row, self.account, self.book, '.05', self.p, self.at, **kwargs)

    def test_price_and_maximum_fee_fit_the_entire_order_cap(self):
        x = self.propose()
        shares, limit = Decimal(x['shares']), Decimal(x['limit_price'])
        # Independent worst-case fee upper bound, not the current spread quote.
        self.assertLessEqual(shares * (limit + Decimal('.05')/4) + Decimal('.00001'), Decimal('2'))
        self.assertLessEqual(Decimal(x['reserved_usd']), Decimal('2'))
        self.assertEqual(x['transmitted'], 0)
        self.assertIs(x['live_eligible'], False)

    def test_minimum_share_count_never_increases_the_money_cap(self):
        self.book['min_order_size'] = 5
        with self.assertRaisesRegex(Blocked, 'minimum_shares'):
            self.propose()

    def test_daily_limit_and_open_reservations_survive_the_planner(self):
        with self.assertRaisesRegex(Blocked, 'budget_exhausted'):
            self.propose(reserved_day=4, reserved_open=4)
        x = self.propose(reserved_day='3.5', reserved_open='3.5')
        self.assertLessEqual(Decimal(x['reserved_usd']), Decimal('.5'))

    def test_real_position_uses_cost_or_mark_whichever_is_higher(self):
        self.account['data']['positions'] = [{'size': 10, 'avgPrice': '.55',
                                             'currentValue': 1, 'event_id': 'other'}]
        x = self.propose()
        self.assertLessEqual(Decimal(x['reserved_usd']), Decimal('.5'))

    def test_cash_and_unmapped_positions_are_not_assumed_available(self):
        for change in [{'available_cash': None}, {'available_cash': 19}, {'open_orders': 1},
                       {'open_orders': False}, {'complete': False}, {'positions': None},
                       {'positions': [{'size': 1, 'avgPrice': '.5', 'currentValue': '.5'}]}]:
            with self.subTest(change=change):
                account = copy.deepcopy(self.account)
                account['data'].update(change)
                with self.assertRaises(Blocked):
                    plan(self.row, account, self.book, 0, self.p, self.at)

    def test_duplicate_event_in_real_account_or_shadow_journal_blocks(self):
        with self.assertRaisesRegex(Blocked, 'event_already'):
            self.propose(reserved_events={'event'})
        self.account['data']['positions'] = [{'size': 1, 'avgPrice': '.5',
                                             'currentValue': '.4', 'event_id': 'event'}]
        with self.assertRaisesRegex(Blocked, 'event_already'):
            self.propose()

    def test_old_future_and_naive_timestamps_block(self):
        for key, offset in [('finished_at', -601), ('finished_at', 6), ('account', -361), ('book', -61)]:
            with self.subTest(key=key, offset=offset):
                row, account, book = copy.deepcopy((self.row, self.account, self.book))
                stamp = self.at + dt.timedelta(seconds=offset)
                if key == 'account': account['at'] = stamp
                elif key == 'book': book['timestamp'] = stamp.timestamp()*1000
                else: row['finished_at'] = stamp
                with self.assertRaises(Blocked):
                    plan(row, account, book, 0, self.p, self.at)
        self.account['at'] = self.at.replace(tzinfo=None)
        with self.assertRaises(Blocked): self.propose()

    def test_live_flags_invalid_caps_and_nonfinite_values_are_rejected(self):
        for values in [{'mode': 'live'}, {'live_dispatch': True}, {'live_dispatch': 'false'},
                       {'order_usd': 200}, {'daily_usd': 5}, {'exposure_usd': 7},
                       {'daily_usd': 'NaN'}, {'order_usd': True}]:
            with self.subTest(values=values), self.assertRaises(Blocked):
                config(values)
        self.row['result']['probability'] = 'Infinity'
        with self.assertRaises(Blocked): self.propose()

    def test_bad_asset_spread_depth_and_fee_are_explicit_failures(self):
        for changes in [{'asset_id': '456'}, {'market': 'wrong'}, {'bids': []}, {'tick_size': None},
                        {'asks': [{'price': '.99', 'size': '10'}]},
                        {'asks': [{'price': '.4', 'size': '-1'}]}]:
            with self.subTest(changes=changes), self.assertRaises(Blocked):
                plan(self.row, self.account, {**self.book, **changes}, 0, self.p, self.at)
        with self.assertRaises(Blocked):
            plan(self.row, self.account, self.book, '.5', self.p, self.at)

    def test_sales_are_explicitly_unimplemented_not_treated_as_buys(self):
        self.row['result']['action'] = 'SELL'
        with self.assertRaisesRegex(Blocked, 'sell_path_not_implemented'):
            self.propose()

    def test_control_missing_corrupt_and_ambiguous_all_pause(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'control.json'
            self.assertTrue(paused(path))
            for value in ['broken', '{}', '[]', '{"paused":"false"}', '{"paused":true}']:
                path.write_text(value)
                self.assertTrue(paused(path))
            path.write_text('{"paused":false}')
            self.assertFalse(paused(path))

    def test_public_reader_cannot_accept_arbitrary_paths_or_submission(self):
        with patch('shadow_executor.worker.urllib.request.build_opener') as opener:
            for kind, identity in [('order', '123'), ('book', '123&other=1'), ('fees', '../order'),
                                   ('book', 'https://example.com')]:
                with self.assertRaises(Blocked): public_read(kind, identity)
            opener.assert_not_called()


if __name__ == '__main__':
    unittest.main()
