"""Shadow-only worker: public market GETs, read-only research, own journal writes."""
import datetime as dt
import json
import os
from pathlib import Path
import re
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .policy import Blocked, amount, config, plan, precheck

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS execution_shadow;
CREATE TABLE IF NOT EXISTS execution_shadow.intents(
 research_id text PRIMARY KEY, event_id text NOT NULL, at timestamptz NOT NULL,
 local_day date NOT NULL, status text NOT NULL CHECK(status IN ('blocked','shadow_candidate')),
 reason text NOT NULL, reserved_usd numeric NOT NULL DEFAULT 0 CHECK(reserved_usd>=0),
 data jsonb NOT NULL, CHECK(status='shadow_candidate' OR reserved_usd=0));
CREATE TABLE IF NOT EXISTS execution_shadow.state(
 key text PRIMARY KEY, value jsonb NOT NULL);
"""
LOCK = 763013241
FORBIDDEN_SECRETS = {'POLY_PRIVATE_KEY', 'PRIVATE_KEY', 'WALLET_PRIVATE_KEY',
                     'TELEGRAM_BOT_TOKEN', 'OPENROUTER_API_KEY', 'POLY_API_SECRET'}


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def paused(path):
    try:
        value = json.loads(Path(path).read_text())
        return not isinstance(value, dict) or value.get('paused') is not False
    except (OSError, ValueError):
        return True


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def public_read(kind, identity):
    # No caller-controlled URL/method/headers, credential or POST operation.
    if kind == 'book' and re.fullmatch(r'[0-9]{1,128}', str(identity)):
        path = '/book?' + urllib.parse.urlencode({'token_id': str(identity)})
    elif kind == 'fees' and re.fullmatch(r'0x[0-9a-fA-F]{64}', str(identity)):
        path = '/clob-markets/' + str(identity)
    else:
        raise Blocked('public_read_identity_invalid')
    req = urllib.request.Request('https://clob.polymarket.com' + path, method='GET',
                                 headers={'User-Agent': 'Clodds-Shadow/0.1'})
    with urllib.request.build_opener(NoRedirect).open(req, timeout=8) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise Blocked('public_read_too_large')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise Blocked('public_read_schema_invalid')
    return value


def account_with_events(c):
    row = c.execute('SELECT at,data FROM public.account_snapshots ORDER BY id DESC LIMIT 1').fetchone()
    if not row:
        raise Blocked('account_missing')
    if c.execute("SELECT value FROM public.hub_meta WHERE key='account_error'").fetchone() not in (None, {'value': None}):
        raise Blocked('account_read_not_confirmed')
    for pos in row['data'].get('positions', []):
        matches = c.execute("SELECT DISTINCT event_id FROM public.markets WHERE data->>'condition_id'=%s",
                            (pos.get('conditionId'),)).fetchall()
        if len(matches) != 1:
            raise Blocked('position_event_mapping_unknown')
        pos['event_id'] = matches[0]['event_id']
    return row


def run_once(c, p, control_path, read=public_read, clock=utcnow):
    p = config(p)
    # The transaction lock covers budget reads, checks, and journal writes.
    if not c.execute('SELECT pg_try_advisory_xact_lock(%s) locked', (LOCK,)).fetchone()['locked']:
        return {'mode': 'shadow', 'status': 'busy', 'transmitted': 0}
    at = clock()
    status = {'at': at.isoformat(), 'mode': 'shadow', 'live_dispatch': False,
              'transmitted': 0, 'examined': 0, 'shadow_candidates': 0, 'blocked': 0,
              'status': 'paused' if paused(control_path) else 'ok',
              'policy': {k: p[k] for k in ('order_usd', 'daily_usd', 'exposure_usd', 'timezone')}}
    started = c.execute("SELECT value FROM execution_shadow.state WHERE key='started_at'").fetchone()
    if not started:
        raise Blocked('shadow_start_watermark_missing')
    if status['status'] != 'paused':
        rows = c.execute("""SELECT r.id,r.finished_at,r.context,r.result,m.data current_market FROM public.research r
            LEFT JOIN public.markets m ON m.id=r.market_id
            LEFT JOIN execution_shadow.intents i ON i.research_id=r.id
            WHERE r.status='complete' AND r.result->>'action' IN ('BUY','SELL')
              AND r.finished_at>=%s AND r.finished_at>now()-interval '10 minutes'
              AND i.research_id IS NULL ORDER BY r.finished_at LIMIT 3""", (started['value'],)).fetchall()
        for row in rows:
            status['examined'] += 1
            proposal, reserve, reason, state = {}, amount(0), 'shadow_only', 'shadow_candidate'
            event = str(row['context'].get('market', {}).get('event_id') or 'unknown')
            try:
                account = account_with_events(c)
                market, decision, *_ = precheck(row, account, p, clock())
                current = row['current_market']
                if not isinstance(current, dict) or any(market.get(key) != current.get(key) for key in (
                        'id', 'event_id', 'condition_id', 'tokens', 'outcomes', 'description', 'resolution_source', 'end', 'version')):
                    raise Blocked('market_changed_since_research')
                if any(current.get(key) is not True for key in ('active', 'accepting_orders', 'book_enabled')):
                    raise Blocked('market_not_tradable')
                if paused(control_path):
                    raise Blocked('paused')
                token = market['tokens'][market['outcomes'].index(decision['outcome'])]
                book = read('book', token)
                if market.get('fees_enabled') is False:
                    rate = 0
                elif market.get('fees_enabled') is True:
                    fd = read('fees', market['condition_id']).get('fd', {})
                    if amount(fd.get('e')) != 1:
                        raise Blocked('fee_schedule_unsupported')
                    rate = amount(fd.get('r'))
                else:
                    raise Blocked('fee_schedule_unknown')
                at = clock()
                day = at.astimezone(ZoneInfo(p['timezone'])).date()
                reserved = c.execute("""SELECT coalesce(sum(reserved_usd),0) opened,
                    coalesce(sum(reserved_usd) FILTER(WHERE local_day=%s),0) today
                    FROM execution_shadow.intents WHERE status='shadow_candidate'""", (day,)).fetchone()
                events = {x['event_id'] for x in c.execute("SELECT event_id FROM execution_shadow.intents WHERE status='shadow_candidate'")}
                # Re-read account and control after external GET latency.
                account = account_with_events(c)
                if paused(control_path):
                    raise Blocked('paused')
                proposal = plan(row, account, book, rate, p, at, reserved['today'], reserved['opened'], events)
                reserve = amount(proposal['reserved_usd'])
            except Blocked as error:
                reason, state = str(error), 'blocked'
            except Exception:
                # No raw provider/SQL errors in logs or journal (DSNs may contain secrets).
                reason, state = 'input_or_public_read_failed', 'blocked'
            at = clock()
            day = at.astimezone(ZoneInfo(p['timezone'])).date()
            c.execute("""INSERT INTO execution_shadow.intents
                (research_id,event_id,at,local_day,status,reason,reserved_usd,data)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
                      (row['id'], event, at, day, state, reason, reserve,
                       Jsonb({'mode': 'shadow', 'live_dispatch': False, 'transmitted': 0,
                              'live_eligible': False, 'proposal': proposal})))
            status['blocked' if state == 'blocked' else 'shadow_candidates'] += 1
    status['at'] = clock().isoformat()
    c.execute("""INSERT INTO execution_shadow.state VALUES('last_pass',%s)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (Jsonb(status),))
    return status


def main():
    if any(os.environ.get(key) for key in FORBIDDEN_SECRETS):
        raise SystemExit('signing_or_service_secret_forbidden')
    p = config(json.loads(Path(os.environ.get('EXECUTOR_SETTINGS', '/config/settings.json')).read_text()))
    dsn = os.environ['DATABASE_URL']
    control = os.environ.get('CONTROL_PATH', '/observer/control.json')
    while True:
        try:
            with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=8) as c:
                c.execute("SET LOCAL statement_timeout='10s'")
                result = run_once(c, p, control)
            # DB commit is part of health; a rolled-back pass never appears healthy.
            if result['status'] != 'busy':
                Path('/tmp/shadow-health.json').write_text(json.dumps(result))
            print(json.dumps(result), flush=True)
        except Exception:
            print(json.dumps({'mode': 'shadow', 'status': 'failed', 'error': 'shadow_pass_failed', 'transmitted': 0}), flush=True)
        time.sleep(60)


if __name__ == '__main__':
    main()
