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


def activity_flows(rows):
    """Group validated activity rows into net_reconciliation inputs.

    Polymarket activity rows move USDC: a BUY trade and a SPLIT take USDC out
    of the wallet; a SELL trade, a MERGE and a REDEEM bring USDC in. A row with
    an unknown type or side fails loudly instead of being silently ignored.
    """
    out = {'trades': [], 'splits': [], 'merges': [], 'redeems': []}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('reconciliation_activity_shape')
        kind, usdc = row.get('type'), row.get('usdcSize')
        if kind == 'TRADE':
            side = row.get('side')
            if side not in ('BUY', 'SELL'):
                raise ValueError('reconciliation_trade_side_unknown')
            if usdc is not None:
                out['trades'].append({'side': side, 'usdcSize': usdc})
        elif kind == 'SPLIT':
            if usdc is not None:
                out['splits'].append(usdc)
        elif kind == 'MERGE':
            if usdc is not None:
                out['merges'].append(usdc)
        elif kind == 'REDEEM':
            if usdc is not None:
                out['redeems'].append(usdc)
        else:
            raise ValueError('reconciliation_activity_type_unknown')
    return out


def net_reconciliation(cash, trades, splits=(), merges=(), redeems=()):
    """Résiduel : dépôts = cash + sorties − entrées des mouvements validés.

    Chaque mouvement connu (trade, split, merge, redeem) déplace du USDC
    depuis ou vers le portefeuille. Les dépôts externes n'exposent aucun flux
    lisible ; ils sont déduits par l'équation du bilan. Un résultat négatif
    reste affiché tel quel, jamais converti en zéro ni en gain. Aucune valeur
    inconnue n'est acceptée silencieusement.
    """
    if not isinstance(trades, (list, tuple)):
        raise ValueError('reconciliation_trades_shape')
    cash_d = amount(cash, 'reconciliation_cash_unknown')
    trade_outflows, trade_inflows = Decimal(0), Decimal(0)
    event_outflows, event_inflows = Decimal(0), Decimal(0)

    for trade in trades:
        if not isinstance(trade, dict):
            raise ValueError('reconciliation_trade_shape')
        usdc = amount(trade.get('usdcSize'), 'reconciliation_trade_amount_unknown')
        if usdc <= 0:
            raise ValueError('reconciliation_trade_amount_invalid')
        side = trade.get('side')
        if side == 'BUY':
            trade_outflows += usdc
        elif side == 'SELL':
            trade_inflows += usdc
        else:
            raise ValueError('reconciliation_trade_side_unknown')

    for batch, direction in ((splits, 'OUT'), (merges, 'IN'), (redeems, 'IN')):
        for value in batch:
            usdc = amount(value, 'reconciliation_event_amount_unknown')
            if usdc <= 0:
                raise ValueError('reconciliation_event_amount_invalid')
            if direction == 'OUT':
                event_outflows += usdc
            else:
                event_inflows += usdc

    deposits = cash_d + (trade_outflows + event_outflows) - (trade_inflows + event_inflows)
    return {
        'deposits': float(deposits),
        'trade_inflows': float(trade_inflows),
        'trade_outflows': float(trade_outflows),
        'event_inflows': float(event_inflows),
        'event_outflows': float(event_outflows),
        'pnl_complete': deposits >= 0,
        'method': 'residual_from_validated_flows',
    }
