#!/usr/bin/env python3
"""Host-only, reversible transfer of scheduled digest ownership. No trading edits."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import urllib.request
from zoneinfo import ZoneInfo

from deploy import CONFIG, PROJECT, ROOT, private

LEDGER = ROOT / 'data/cloddsbot/autonomy/observer.sqlite'
STATE = CONFIG / 'digest-handoff.json'


def bot_api(method, path, data=None):
    token = Path('/run/lab-dashboard/clodds-token').read_text().strip()
    request = urllib.request.Request('http://127.0.0.1:18789' + path, method=method,
        data=json.dumps(data).encode() if data is not None else None,
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)
    if result.get('ok') is not True:
        raise RuntimeError('native_api_rejected')
    return result['data']


def read_owner():
    with sqlite3.connect('file:' + str(LEDGER) + '?mode=ro', uri=True, timeout=20) as c:
        row = c.execute("SELECT value FROM kv WHERE key='digestOwner'").fetchone()
        return json.loads(row[0]) if row else 'observer'


def write_owner(value):
    with sqlite3.connect(LEDGER, timeout=20) as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("INSERT INTO kv(key,value) VALUES('digestOwner',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(value),))


def recreate_hub():
    result = subprocess.run(['docker', 'compose', '-f', str(PROJECT / 'compose.yaml'), 'up', '-d', '--no-deps', 'hub'], capture_output=True)
    if result.returncode:
        raise RuntimeError('hub_recreate_failed')


def restore(state):
    settings = json.loads((CONFIG / 'settings.json').read_text())
    settings.update(state['settings'])
    private(CONFIG / 'settings.json', json.dumps(settings, ensure_ascii=False, indent=2) + '\n', group_read=True)
    write_owner(state['owner'])
    for job in state['native_jobs']:
        bot_api('PATCH', '/api/cron/jobs/' + job['id'], {'enabled': job['enabled']})
    recreate_hub()


def activate():
    script = "import hashlib,pathlib;p=pathlib.Path('/app/ops/autonomy/worker.py');b=p.read_bytes();assert b'digest-owner:v1' in b;print(hashlib.sha256(b).hexdigest())"
    result = subprocess.run(['docker', 'exec', 'cloddsbot-bot-1', 'python3', '-c', script], capture_output=True)
    if result.returncode:
        raise RuntimeError('observer_image_missing_digest_owner_support')
    expected = hashlib.sha256((PROJECT / 'observer-overlay/worker.py').read_bytes()).hexdigest()
    if result.stdout.decode().strip() != expected:
        raise RuntimeError('observer_worker_hash_mismatch')
    settings = json.loads((CONFIG / 'settings.json').read_text())
    config = json.loads((ROOT / 'config/cloddsbot/clodds.json').read_text())
    owners = config.get('channels', {}).get('telegram', {}).get('allowFrom', [])
    if len(owners) != 1 or not str(owners[0]).isdigit():
        raise RuntimeError('single_telegram_owner_required')
    jobs = bot_api('GET', '/api/cron/jobs?includeDisabled=true')['jobs']
    digests = [x for x in jobs if x.get('payload', {}).get('kind') == 'dailyDigest']
    if len(digests) != 1:
        raise RuntimeError('native_digest_inventory_changed')
    if read_owner() == 'research-hub' and settings.get('digest_enabled') and not digests[0]['enabled']:
        print(json.dumps({'owner': 'research-hub', 'already_configured': True}))
        return
    state = {'at': dt.datetime.now(dt.timezone.utc).isoformat(), 'owner': read_owner(),
             'settings': {k: settings.get(k) for k in ('digest_enabled', 'telegram_chat_id')},
             'native_jobs': [{'id': x['id'], 'enabled': x['enabled']} for x in digests]}
    if not STATE.exists():
        private(STATE, json.dumps(state, indent=2) + '\n')
    active = subprocess.run(['systemctl', 'is-active', '--quiet', 'clodds-observer.service']).returncode == 0
    if active:
        subprocess.run(['systemctl', 'stop', 'clodds-observer.service'], check=True)
    try:
        # The old sender is stopped during handoff. If its evening report was
        # already acknowledged, record that fact before enabling the new sender.
        today = dt.datetime.now(ZoneInfo(settings['timezone'])).date().isoformat()
        with sqlite3.connect('file:' + str(LEDGER) + '?mode=ro', uri=True) as c:
            old = c.execute('SELECT sent FROM outbox WHERE key=?', ('digest:' + today + ':20',)).fetchone()
        if old and old[0]:
            code = "from hub.store import Store;s=Store();s.execute(\"INSERT INTO notifications(key,text,status) VALUES(%s,%s,'legacy_sent') ON CONFLICT DO NOTHING\",(__import__('sys').argv[1],'Acknowledged by observer before handoff'))"
            subprocess.run(['docker', 'exec', 'research-hub', 'python3', '-c', code, 'daily:' + today], check=True, capture_output=True)
        for job in digests:
            bot_api('PATCH', '/api/cron/jobs/' + job['id'], {'enabled': False})
        write_owner('research-hub')
        settings.update(digest_enabled=True, telegram_chat_id=str(owners[0]))
        private(CONFIG / 'settings.json', json.dumps(settings, ensure_ascii=False, indent=2) + '\n', group_read=True)
        recreate_hub()
    except Exception:
        restore(state)
        raise
    finally:
        if active:
            subprocess.run(['systemctl', 'start', 'clodds-observer.service'], check=True)
    print(json.dumps({'owner': 'research-hub', 'hour': settings['digest_hour'], 'timezone': settings['timezone'],
                      'native_digest_enabled': False, 'trading_settings_modified': False}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['activate', 'rollback', 'status'])
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError('root_required')
    if args.command == 'activate':
        activate()
    elif args.command == 'rollback':
        restore(json.loads(STATE.read_text()))
        print(json.dumps({'restored': True, 'owner': read_owner()}))
    else:
        settings = json.loads((CONFIG / 'settings.json').read_text())
        print(json.dumps({'owner': read_owner(), 'digest_enabled': settings.get('digest_enabled'),
                          'hour': settings['digest_hour'], 'timezone': settings['timezone']}))
