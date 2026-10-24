import datetime as dt
from zoneinfo import ZoneInfo


def summary(store, at=None):
    at = at or dt.datetime.now(dt.timezone.utc)
    if not store.one("SELECT to_regclass('execution_shadow.state') present")['present']:
        return {'installed': False}
    row = store.one("SELECT value FROM execution_shadow.state WHERE key='last_pass'")
    if not row:
        return {'installed': True, 'status': 'initializing', 'fresh': False}
    last = row['value']
    try:
        stamp = dt.datetime.fromisoformat(last['at'])
        fresh = -5 <= (at-stamp).total_seconds() <= 180
    except (KeyError, TypeError, ValueError):
        fresh = False
    totals = store.one("""SELECT count(*) FILTER(WHERE status='blocked') blocked,
        count(*) FILTER(WHERE status='shadow_candidate') candidates,
        coalesce(sum(reserved_usd) FILTER(WHERE status='shadow_candidate' AND local_day=%s),0) reserved_day
        FROM execution_shadow.intents""", (at.astimezone(ZoneInfo('Europe/Paris')).date(),))
    return {'installed': True, 'mode': 'shadow', 'live_dispatch': False,
            'transmission_capability': 'absent', 'transmitted': 0,
            'status': last.get('status', 'unknown'), 'fresh': fresh,
            'last_at': last.get('at'), 'policy': last.get('policy'),
            'blocked': totals['blocked'], 'candidates': totals['candidates'],
            'reserved_day': str(totals['reserved_day'])}
