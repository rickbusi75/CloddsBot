#!/usr/bin/env python3
"""VPS-side availability alert independent of the bot container and AI."""
import argparse
import datetime as dt
import json
from pathlib import Path
import subprocess
import urllib.request


def transition(state, failed, now):
    state = dict(state)
    state['failures'] = state.get('failures', 0) + 1 if failed else 0
    message = None
    if failed and state['failures'] >= 3 and not state.get('alerted'):
        message = 'CloddsBot : veille ou réseau VPN indisponible depuis plusieurs contrôles. Vérifier les services. Aucun ordre réel automatique.'
        state['alerted'] = True
    elif not failed and state.get('alerted'):
        message = 'CloddsBot : la veille VPS publie de nouveau son état.'
        state['alerted'] = False
    state['checkedAt'] = now.isoformat()
    return state, message


def recovery_allowed(state, mode, vpn, bot, service, now):
    if (mode != 'vpn' or not vpn.get('Running') or vpn.get('Health', {}).get('Status') != 'unhealthy'
            or bot.get('Status') not in ('running', 'created', 'restarting') or service not in ('active', 'activating')
            or state.get('failures', 0) < 3):
        return False
    try:
        last = dt.datetime.fromisoformat(state['lastRecoveryAttempt']) if state.get('lastRecoveryAttempt') else now - dt.timedelta(hours=2)
        return (now - last).total_seconds() >= 3600
    except (ValueError, TypeError):
        return False


def container_state(name):
    try:
        result = subprocess.run(['docker', 'inspect', '--format', '{{json .State}}', name],
                                capture_output=True, text=True, timeout=15)
        return json.loads(result.stdout) if result.returncode == 0 else {}
    except (ValueError, subprocess.TimeoutExpired, OSError):
        return {}


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
    try:
        mode = (root / 'config/cloddsbot/network-mode').read_text().strip()
    except OSError:
        mode = 'unknown'
    vpn, bot = container_state('cloddsbot-gluetun-1'), container_state('cloddsbot-bot-1')
    if mode == 'vpn' and vpn.get('Health', {}).get('Status') != 'healthy':
        failed = True
    try:
        service = subprocess.run(['systemctl', 'is-active', 'clodds-observer'],
                                 capture_output=True, text=True, timeout=15).stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        service, failed = 'unknown', True
    updated, message = transition(state, failed, now)
    if recovery_allowed(updated, mode, vpn, bot, service, now):
        updated['lastRecoveryAttempt'] = now.isoformat()
        # Persist before mutating: a crash cannot trigger a rapid recreation loop.
        data.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps({**updated, 'alerted': state.get('alerted', False)}))
        try:
            subprocess.run(['systemctl', 'stop', 'clodds-observer'], capture_output=True, timeout=20)
            result = subprocess.run(['/usr/local/bin/clodds-vpn', 'restart'], capture_output=True, timeout=190)
            updated['lastRecoveryResult'] = 'ok' if result.returncode == 0 else 'failed'
        except (subprocess.TimeoutExpired, OSError):
            updated['lastRecoveryResult'] = 'timeout'
        finally:
            try:
                subprocess.run(['systemctl', 'start', 'clodds-observer'], capture_output=True, timeout=30)
            except (subprocess.TimeoutExpired, OSError):
                updated['lastRecoveryResult'] = 'worker-start-failed'
        message = 'CloddsBot : panne VPN détectée ; remise en service ciblée tentée (' + updated['lastRecoveryResult'] + '). Mode VPN conservé, aucune bascule directe et aucun ordre réel.'
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
            # Retain cooldown even if Telegram fails, while retrying the notification.
            updated['alerted'] = state.get('alerted', False)
            file.write_text(json.dumps(updated))
            print('Watchdog notification failed')
            return
    data.mkdir(parents=True, exist_ok=True)
    temporary = data / 'watchdog.tmp'
    temporary.write_text(json.dumps(updated))
    temporary.replace(file)
    print('Observer stale' if failed else 'Observer fresh')


if __name__ == '__main__':
    main()
