import datetime as dt
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP
from zoneinfo import ZoneInfo


DEFAULTS = {'mode': 'shadow', 'live_dispatch': False, 'order_usd': 2,
            'daily_usd': 4, 'exposure_usd': 6, 'timezone': 'Europe/Paris',
            'account_max_age': 360, 'decision_max_age': 600,
            'book_max_age': 60, 'minimum_net_edge': .02, 'max_spread': .08}


class Blocked(ValueError):
    pass


def amount(value):
    if isinstance(value, bool):
        raise Blocked('number_invalid')
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise Blocked('number_invalid') from None
    if not result.is_finite():
        raise Blocked('number_invalid')
    return result


def fresh(value, at, seconds):
    try:
        stamp = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if stamp.tzinfo is None or not -5 <= (at - stamp).total_seconds() <= seconds:
            raise ValueError()
    except (TypeError, ValueError, OverflowError):
        raise Blocked('timestamp_stale_or_invalid') from None


def config(supplied):
    p = {**DEFAULTS, **supplied}
    if p['mode'] != 'shadow' or p['live_dispatch'] is not False:
        raise Blocked('live_dispatch_not_implemented')
    for key, low, high in [('order_usd', '.01', '2'), ('daily_usd', '.01', '4'),
                           ('exposure_usd', '.01', '6'), ('account_max_age', '1', '360'),
                           ('decision_max_age', '1', '600'), ('book_max_age', '1', '60'),
                           ('minimum_net_edge', '.02', '.5'), ('max_spread', '.001', '.08')]:
        if not Decimal(low) <= amount(p[key]) <= Decimal(high):
            raise Blocked('policy_outside_pilot_limits')
    if not amount(p['order_usd']) <= amount(p['daily_usd']) <= amount(p['exposure_usd']):
        raise Blocked('policy_limits_inconsistent')
    for key in ('account_max_age', 'decision_max_age', 'book_max_age'):
        value = amount(p[key])
        if value != value.to_integral_value():
            raise Blocked('policy_age_must_be_integer')
        p[key] = int(value)
    ZoneInfo(p['timezone'])
    return p


def precheck(row, account, p, at):
    fresh(row['finished_at'], at, p['decision_max_age'])
    fresh(account['at'], at, p['account_max_age'])
    decision, market, a = row['result'], row['context']['market'], account['data']
    if decision.get('action') != 'BUY':
        raise Blocked('sell_path_not_implemented')
    ids = decision.get('evidence_ids')
    if decision.get('rules_checked') is not True or not isinstance(ids, list) or not ids or any(not isinstance(x, str) or not x for x in ids):
        raise Blocked('decision_evidence_incomplete')
    probability, lower = amount(decision.get('probability')), amount(decision.get('lower_bound'))
    if not 0 < lower <= probability < 1:
        raise Blocked('probability_invalid')
    if a.get('complete') is not True or a.get('open_orders') != 0 or isinstance(a.get('open_orders'), bool):
        raise Blocked('account_incomplete_or_orders_open')
    if any(market.get(k) is not True for k in ('active', 'accepting_orders', 'book_enabled')):
        raise Blocked('market_not_tradable')
    end = market.get('end')
    try:
        end = dt.datetime.fromisoformat(str(end).replace('Z', '+00:00'))
        if end.tzinfo is None or end <= at:
            raise ValueError()
    except (TypeError, ValueError):
        raise Blocked('market_end_invalid') from None
    if not market.get('event_id') or not market.get('condition_id'):
        raise Blocked('market_identity_missing')
    outcomes, tokens = market.get('outcomes', []), market.get('tokens', [])
    if len(outcomes) != 2 or len(tokens) != 2 or len(set(tokens)) != 2 or decision.get('outcome') not in outcomes:
        raise Blocked('outcome_mapping_invalid')
    cash = amount(a.get('cash'))
    available = amount(a.get('available_cash'))
    if not 0 <= available <= cash:
        raise Blocked('cash_inconsistent')
    if not isinstance(a.get('positions'), list):
        raise Blocked('positions_unknown')
    exposure, events = Decimal(0), set()
    for pos in a['positions']:
        size, average, value = amount(pos.get('size')), amount(pos.get('avgPrice')), amount(pos.get('currentValue'))
        if size < 0 or not 0 <= average <= 1 or value < 0 or not pos.get('event_id'):
            raise Blocked('position_or_event_unknown')
        exposure += max(size * average, value)
        if size > 0:
            events.add(str(pos['event_id']))
    return market, decision, available, exposure, events


def plan(row, account, book, rate, p, at, reserved_day=0, reserved_open=0, reserved_events=()):
    market, decision, cash, real_exposure, real_events = precheck(row, account, p, at)
    event = str(market['event_id'])
    if event in real_events or event in reserved_events:
        raise Blocked('event_already_exposed')
    day, opened = amount(reserved_day), amount(reserved_open)
    if day < 0 or opened < day:
        raise Blocked('shadow_reservations_inconsistent')
    budget = min(amount(p['order_usd']), amount(p['daily_usd']) - day,
                 amount(p['exposure_usd']) - real_exposure - opened, cash - opened)
    if budget <= 0:
        raise Blocked('cash_or_pilot_budget_exhausted')
    token = str(market['tokens'][market['outcomes'].index(decision['outcome'])])
    if str(book.get('asset_id')) != token:
        raise Blocked('book_asset_mismatch')
    if book.get('market') != market['condition_id']:
        raise Blocked('book_condition_mismatch')
    try:
        timestamp = dt.datetime.fromtimestamp(float(amount(book.get('timestamp'))) / 1000, dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise Blocked('book_timestamp_invalid') from None
    fresh(timestamp, at, p['book_max_age'])
    rate = amount(rate)
    if not 0 <= rate <= Decimal('.1'):
        raise Blocked('fee_schedule_unsupported')
    tick, minimum = amount(book.get('tick_size')), amount(book.get('min_order_size'))
    if tick not in {Decimal('.1'), Decimal('.01'), Decimal('.001'), Decimal('.0001')} or minimum <= 0:
        raise Blocked('book_constraints_invalid')
    levels = {}
    for side in ('bids', 'asks'):
        values = [(amount(x.get('price')), amount(x.get('size'))) for x in book.get(side, [])]
        if not values or any(not 0 < price < 1 or size <= 0 for price, size in values):
            raise Blocked('book_depth_invalid')
        levels[side] = values
    bid = max(price for price, _ in levels['bids'])
    ask, depth = min(levels['asks'])
    if not 0 < ask - bid <= amount(p['max_spread']):
        raise Blocked('spread_invalid')
    limit = (ask / tick).to_integral_value(rounding=ROUND_UP) * tick
    belief = min(amount(decision['lower_bound']), amount(decision['probability']) - Decimal('.03'))
    if belief - limit - rate * limit * (1 - limit) < amount(p['minimum_net_edge']):
        raise Blocked('net_edge_insufficient')
    # Reserve the maximum price and fee (p*(1-p)<=1/4), including fee rounding.
    fee_rounding = Decimal('.00001') if rate else Decimal(0)
    shares = min(depth, (budget - fee_rounding) / (limit + rate / 4)).quantize(Decimal('.000001'), rounding=ROUND_DOWN)
    reserve = (shares * (limit + rate / 4) + fee_rounding).quantize(Decimal('.000001'), rounding=ROUND_UP)
    if shares <= 0 or shares < minimum:
        raise Blocked('minimum_shares_exceeds_pilot_budget')
    if reserve > budget or reserve <= 0:
        raise Blocked('reservation_outside_budget')
    return {'mode': 'shadow', 'live_eligible': False, 'transmitted': 0, 'action': 'BUY',
            'event_id': event, 'market_id': str(market['id']), 'token': token,
            'outcome': decision['outcome'], 'shares': str(shares), 'limit_price': str(limit),
            'reserved_usd': str(reserve), 'fee_rate': str(rate),
            'requires_review': ['citation_entity_precision', 'signing_adapter', 'live_reconciliation']}
