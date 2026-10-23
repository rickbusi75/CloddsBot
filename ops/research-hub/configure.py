#!/usr/bin/env python3
"""Private host-side API configuration. Input is JSON on stdin, no key output."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from deploy import CONFIG, PROJECT, read_env

KEYS = {'EIA_API_KEY': 'eia', 'CONGRESS_API_KEY': 'congress', 'ACLED_ACCESS_TOKEN': 'acled',
        'UCDP_ACCESS_TOKEN': 'ucdp', 'RELIEFWEB_APPNAME': 'reliefweb',
        'COINGECKO_DEMO_API_KEY': 'coingecko', 'FOOTBALL_DATA_TOKEN': 'football_data'}


def atomic(path, value, mode, group=0):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(value)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(name, mode)
        os.chown(name, 0, group)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def main():
    if os.geteuid() != 0:
        raise RuntimeError('root_required')
    if sys.argv[1:] == ['status']:
        env = read_env(CONFIG / 'hub.env')
        print(json.dumps({k: bool(env.get(k)) for k in KEYS}))
        return
    payload = json.loads(sys.stdin.read(16384))
    key, value = payload.get('key'), payload.get('value')
    if key not in KEYS or not isinstance(value, str) or not 1 <= len(value) <= 12000:
        raise ValueError('unsupported_configuration')
    # Docker env-file values are deliberately restricted to single-line secrets.
    if any(c.isspace() for c in value) or any(c in value for c in '\x00\r\n#$\'"'):
        raise ValueError('unsupported_secret_format')
    env = read_env(CONFIG / 'hub.env')
    settings = json.loads((CONFIG / 'settings.json').read_text())
    env[key] = value
    settings['sources'][KEYS[key]] = True
    atomic(CONFIG / 'hub.env', ''.join(k + '=' + v + '\n' for k, v in env.items()), 0o600)
    atomic(CONFIG / 'settings.json', json.dumps(settings, ensure_ascii=False, indent=2) + '\n', 0o640, 1000)
    result = subprocess.run(['docker', 'compose', '-f', str(PROJECT / 'compose.yaml'), 'up', '-d', '--no-deps', 'hub'],
                            capture_output=True)
    if result.returncode:
        raise RuntimeError('configuration_saved_service_recreate_failed')
    print(json.dumps({'source': KEYS[key], 'configuration_saved': True, 'service_recreated': True,
                      'external_access_validated': False}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Never let a parser error display an input secret in a traceback.
        print(json.dumps({'error': type(error).__name__, 'configuration_applied': False}))
        sys.exit(1)
