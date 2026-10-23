"""Account arithmetic from validated reads; no inference of deposits or profit."""
from decimal import Decimal, InvalidOperation


def amount(value, code):
    if value is None or isinstance(value, bool):
        raise ValueError(code)
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(code) from None
    if not result.is_finite() or result < 0:
        raise ValueError(code)
    return result


def cash_summary(balance, orders):
    units = amount(balance.get('balance'), 'account_balance_unknown')
    if units != units.to_integral_value():
        raise ValueError('account_balance_not_base_units')
    cash, reserved, seen = units / Decimal(1000000), Decimal(0), {}
    for order in orders:
        identifier, side = order.get('id'), order.get('side')
        if not identifier or side not in ('BUY', 'SELL'):
            raise ValueError('account_open_order_schema_changed')
        price = amount(order.get('price'), 'account_open_order_price_unknown')
        size = amount(order.get('original_size', order.get('originalSize')), 'account_open_order_size_unknown')
        matched = amount(order.get('size_matched', order.get('sizeMatched')), 'account_open_order_matched_unknown')
        if not 0 < price < 1 or size <= 0 or matched > size:
            raise ValueError('account_open_order_values_invalid')
        identity = (side, price, size, matched)
        if identifier in seen:
            if seen[identifier] != identity:
                raise ValueError('account_open_order_changed_during_read')
            continue
        seen[identifier] = identity
        if side == 'BUY':
            reserved += (size - matched) * price
    return {'cash': float(cash), 'available_cash': float(max(Decimal(0), cash - reserved)),
            'reserved_buys': float(reserved), 'open_orders': len(seen),
            'reservation_exceeds_balance': reserved > cash,
            'reservation_basis': 'remaining_buy_limit_notional_excluding_fees'}


def positions_value(positions):
    total, seen = Decimal(0), set()
    for position in positions:
        asset = position.get('asset')
        if not isinstance(asset, str) or not asset or asset in seen:
            raise ValueError('account_position_identity_invalid')
        seen.add(asset)
        amount(position.get('size'), 'account_position_size_unknown')
        total += amount(position.get('currentValue'), 'account_position_valuation_unknown')
    return float(total)
