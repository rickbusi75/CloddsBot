#!/usr/bin/env python3
"""Run on the Oracle host as root. Never prints credentials or mutates trading."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

from workflows import generate

ROOT = Path('/srv/lab')
CONFIG = ROOT / 'config/research-hub'
PROJECT = ROOT / 'projects/research-hub'
PREFIX = 'Clodds Research • '


def run(args, **kwargs):
    result = subprocess.run(args, check=True, capture_output=True, **kwargs)
    return result.stdout


def read_env(path):
    values = {}
    for line in Path(path).read_text().splitlines():
        key, sep, value = line.partition('=')
        if sep:
            values[key] = value.strip().strip('"\'')
    return values


def private(path, text, group_read=False):
    path.write_text(text, encoding='utf-8')
    path.chmod(0o640 if group_read else 0o600)
    if group_read:
        os.chown(path, 0, 1000)


def initialize():
    CONFIG.mkdir(parents=True, exist_ok=True, mode=0o750)
    os.chown(CONFIG, 0, 1000)
    os.chmod(CONFIG, 0o750)
    (ROOT / 'data/research-hub').mkdir(parents=True, exist_ok=True, mode=0o750)
    (ROOT / 'dumps/research-hub').mkdir(parents=True, exist_ok=True, mode=0o700)
    bot = read_env(ROOT / 'config/cloddsbot/bot.env')
    if not (CONFIG / 'hub.env').exists():
        token, password = secrets.token_hex(32), secrets.token_hex(32)
        values = {'HUB_TOKEN': token, 'DATABASE_URL': 'postgresql://research:' + password + '@db:5432/research',
                  'OBSERVER_LEDGER': '/observer/observer.sqlite', 'HUB_SETTINGS': '/config/settings.json'}
        for key in ('OPENROUTER_API_KEY', 'BRAVE_SEARCH_API_KEY', 'ODDS_API_KEY', 'MANIFOLD_API_KEY', 'METACULUS_TOKEN',
                    'TELEGRAM_BOT_TOKEN', 'POLY_API_KEY', 'POLY_API_SECRET', 'POLY_API_PASSPHRASE', 'POLY_FUNDER_ADDRESS', 'POLY_SIGNATURE_TYPE'):
            if bot.get(key):
                values[key] = bot[key]
        # Derive the signer inside the existing bot without exporting its private key.
        signer = run(['docker', 'exec', 'cloddsbot-bot-1', 'node', '-e',
                      "process.stdout.write(require('ethers').computeAddress(process.env.POLY_PRIVATE_KEY))"]).decode().strip()
        values['POLY_SIGNER_ADDRESS'] = signer
        inspect = json.loads(run(['docker', 'inspect', 'n8n-searxng-1']))[0]
        if 'n8n_default' in inspect['NetworkSettings']['Networks']:
            values['SEARXNG_URL'] = 'http://n8n-searxng-1:8080'
        if any('\n' in value or '\r' in value for value in values.values()):
            raise RuntimeError('invalid_private_environment_value')
        private(CONFIG / 'hub.env', '\n'.join(k + '=' + v for k, v in values.items()) + '\n')
        private(CONFIG / 'db.env', 'POSTGRES_USER=research\nPOSTGRES_DB=research\nPOSTGRES_PASSWORD=' + password + '\n')
    if not (CONFIG / 'settings.json').exists():
        settings = json.loads((PROJECT / 'settings.example.json').read_text())
        settings['model'] = bot.get('CLODDS_MODEL') or settings['model']
        # Preserve the existing delivery until a consolidated report is verified.
        settings['digest_enabled'] = False
        private(CONFIG / 'settings.json', json.dumps(settings, ensure_ascii=False, indent=2) + '\n', group_read=True)
    print(json.dumps({'configuration': 'ready', 'existing_bot_modified': False, 'signing_key_copied': False}))


def n8n_api(method, path, body=None):
    bot = read_env(ROOT / 'config/cloddsbot/bot.env')
    base = bot.get('N8N_BASE_URL', 'http://127.0.0.1:5678/api/v1').rstrip('/')
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, method=method, data=data,
        headers={'X-N8N-API-KEY': bot['N8N_API_KEY'], 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError('n8n_http_' + str(error.code)) from None


def import_workflows(activate=False):
    env = read_env(CONFIG / 'hub.env')
    state_path = CONFIG / 'n8n-state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {'workflows': {}}
    if not state.get('http_credential'):
        c = n8n_api('POST', '/credentials', {'name': 'Research Hub API', 'type': 'httpHeaderAuth',
                     'data': {'name': 'Authorization', 'value': 'Bearer ' + env['HUB_TOKEN']}})
        state['http_credential'] = {'id': c['id'], 'name': c['name']}
        private(state_path, json.dumps(state, indent=2))
    if not state.get('model_credential'):
        c = n8n_api('POST', '/credentials', {'name': 'Research Hub — budget partagé', 'type': 'openAiApi',
                     'data': {'apiKey': env['HUB_TOKEN'], 'url': 'http://research-hub:8766/llm/v1'}})
        state['model_credential'] = {'id': c['id'], 'name': c['name']}
        private(state_path, json.dumps(state, indent=2))
    settings = json.loads((CONFIG / 'settings.json').read_text())
    definitions = generate(state['http_credential'], state['model_credential'], settings['model'])
    for workflow in definitions:
        previous = state['workflows'].get(workflow['name'])
        if previous:
            current = n8n_api('GET', '/workflows/' + previous)
            if current.get('active'):
                n8n_api('POST', '/workflows/' + previous + '/deactivate', {})
            result = n8n_api('PUT', '/workflows/' + previous, workflow)
        else:
            result = n8n_api('POST', '/workflows', workflow)
        state['workflows'][workflow['name']] = result['id']
        private(state_path, json.dumps(state, indent=2))
        if activate:
            n8n_api('POST', '/workflows/' + result['id'] + '/activate', {})
    print(json.dumps({'workflows': len(definitions), 'activated': activate,
                      'credentials_created_without_secret_output': True}))


def export_status():
    state = json.loads((CONFIG / 'n8n-state.json').read_text())
    rows = []
    for name, workflow_id in state['workflows'].items():
        value = n8n_api('GET', '/workflows/' + workflow_id)
        rows.append({'name': name, 'id': workflow_id, 'active': value.get('active'), 'nodes': len(value['nodes'])})
    print(json.dumps(rows, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['initialize', 'import', 'activate', 'status'])
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError('root_required_for_private_configuration')
    {'initialize': initialize, 'import': import_workflows, 'activate': lambda: import_workflows(True), 'status': export_status}[args.command]()
