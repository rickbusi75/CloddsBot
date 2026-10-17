#!/usr/bin/env python3
"""VPS-side availability alert independent of the bot container and AI."""
import argparse
import datetime as dt
import json
from pathlib import Path
import urllib.request


def transition(state, failed, now):
    state = dict(state)
    state['failures'] = state.get('failures', 0) + 1 if failed else 0
    message = None
    if failed and state['failures'] >= 3 and not state.get('alerted'):
        message = 'CloddsBot : la veille VPS ne publie plus son état depuis plus de dix minutes. Vérifier clodds-observer.service. Aucun ordre réel automatique.'
        state['alerted'] = True
    elif not failed and state.get('alerted'):
        message = 'CloddsBot : la veille VPS publie de nouveau son état.'
        state['alerted'] = False
    state['checkedAt'] = now.isoformat()
    return state, message


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--root', default='/srv/lab')
    args = p.parse_args()
    root = Path(args.root)
    data = root / 'data/cloddsbot/autonomy'
    file = data / 'watchdog.json'
    now = dt.datetime.now(dt.timezone.utc)
    try:
        status = json.loads((data / 'status.json').read_text())
        age = (now - dt.datetime.fromisoformat(status['at'])).total_seconds()
        failed = not 0 <= age <= 600
    except (OSError, ValueError, KeyError):
        failed = True
    try:
        state = json.loads(file.read_text())
    except (OSError, ValueError):
        state = {}
    updated, message = transition(state, failed, now)
    if message:
        env = {}
        for line in (root / 'config/cloddsbot/bot.env').read_text().splitlines():
            key, sep, value = line.partition('=')
            if key == 'TELEGRAM_BOT_TOKEN' and sep:
                env[key] = value.strip().strip('"\'')
        config = json.loads((root / 'config/cloddsbot/clodds.json').read_text())
        owners = config['channels']['telegram']['allowFrom']
        if len(owners) != 1 or not str(owners[0]).isdigit():
            raise RuntimeError('Explicit owner required')
        req = urllib.request.Request('https://api.telegram.org/bot' + env['TELEGRAM_BOT_TOKEN'] + '/sendMessage',
                                     data=json.dumps({'chat_id': str(owners[0]), 'text': message}).encode(),
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                if not json.load(response).get('ok'):
                    raise RuntimeError('Notification rejected')
        except Exception:
            # Retain the previous state so the next timer retries; no secret-bearing traceback.
            print('Watchdog notification failed')
            return
    data.mkdir(parents=True, exist_ok=True)
    temporary = data / 'watchdog.tmp'
    temporary.write_text(json.dumps(updated))
    temporary.replace(file)
    print('Observer stale' if failed else 'Observer fresh')


if __name__ == '__main__':
    main()
