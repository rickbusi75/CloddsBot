#!/usr/bin/env python3
"""Private host-side API configuration. Input is JSON on stdin, no key output."""
import json
import os
import fcntl
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

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


class ConfigurationFailure(RuntimeError):
    def __init__(self, code, **state):
        self.result = {'error': code, **state}
        super().__init__(code)


def validate(payload):
    if not isinstance(payload, dict):
        raise ValueError('unsupported_configuration')
    key, value = payload.get('key'), payload.get('value')
    if key not in KEYS or not isinstance(value, str) or not 1 <= len(value) <= 12000:
        raise ValueError('unsupported_configuration')
    # Docker env-file values are deliberately restricted to single-line secrets.
    if any(c.isspace() for c in value) or any(c in value for c in '\x00\r\n#$\'"'):
        raise ValueError('unsupported_secret_format')
    return key, value


def env_update(raw, key, value):
    # Preserve every unrelated line verbatim: parsing/re-emitting the complete
    # env file can change quoted credentials belonging to other integrations.
    lines = raw.splitlines(keepends=True)
    found = [i for i, line in enumerate(lines) if line.partition('=')[0] == key]
    if len(found) > 1:
        raise ValueError('duplicate_configuration_key')
    if found:
        lines[found[0]] = key + '=' + value + '\n'
        return ''.join(lines)
    return raw + ('' if not raw or raw.endswith('\n') else '\n') + key + '=' + value + '\n'


def ensure_idle():
    # Read only. Refuse a known active analysis/job before changing env or
    # recreating Hub; old interrupted job records are not live handles.
    with sqlite3.connect('file:/srv/lab/data/n8n/home/database.sqlite?mode=ro', uri=True) as c:
        active = c.execute("""SELECT count(*) FROM execution_entity e JOIN workflow_entity w ON w.id=e.workflowId
            WHERE w.name LIKE 'Clodds Research%' AND e.status IN ('new','running','waiting')""").fetchone()[0]
    if active:
        raise RuntimeError('research_busy_retry_later')
    script = """import os
from hub.store import Store
s=Store(os.environ['DATABASE_URL'])
r=s.one("SELECT (SELECT count(*) FROM research WHERE status='running')+(SELECT count(*) FROM jobs WHERE status='running' AND started_at>now()-interval '30 minutes') n")
print(r['n'])
"""
    result = subprocess.run(['docker', 'exec', 'research-hub', 'python', '-c', script],
                            capture_output=True, timeout=20)
    if result.returncode or result.stdout.strip() != b'0':
        raise RuntimeError('research_busy_or_unavailable')


def recreate(project):
    args = ['docker', 'compose', '-f', str(project / 'compose.yaml')]
    if (project / 'compose.executor.yaml').is_file():
        args += ['-f', str(project / 'compose.executor.yaml')]
    result = subprocess.run(args + ['up', '-d', '--no-deps', '--no-build', 'hub'],
                            capture_output=True, timeout=90)
    if result.returncode:
        raise RuntimeError('service_recreate_failed')


def healthy():
    for _ in range(30):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8766/health', timeout=2) as response:
                data = json.load(response)
            if data.get('status') == 'healthy' and data.get('mode') == 'paper':
                return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError('service_health_not_confirmed')


def apply(payload, directory=CONFIG, project=PROJECT):
    key, value = validate(payload)
    # A local lock serializes configuration helpers; it is not a workflow lock.
    with open(directory / '.api-configuration.lock', 'a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        ensure_idle()
        paths = [directory / 'hub.env', directory / 'settings.json']
        originals = [(p.read_text(), p.stat().st_mode & 0o777, p.stat().st_gid) for p in paths]
        settings = json.loads(originals[1][0])
        settings['sources'][KEYS[key]] = True
        updated = [env_update(originals[0][0], key, value), json.dumps(settings, ensure_ascii=False, indent=2)+'\n']
        backup = directory / 'backups' / ('api-' + uuid.uuid4().hex)
        backup.mkdir(parents=True, mode=0o700)
        os.chmod(backup.parent, 0o700)
        for p in paths:
            shutil.copy2(p, backup / p.name)
        changed, restart_attempted = False, False
        try:
            # Second guard after backup I/O, immediately before mutation.
            ensure_idle()
            changed = True
            for p, text, (_, mode, group) in zip(paths, updated, originals):
                atomic(p, text, mode, group)
            restart_attempted = True
            recreate(project)
            healthy()
        except Exception:
            restored, service_restored = not changed, None
            if changed:
                try:
                    for p, (text, mode, group) in zip(paths, originals):
                        atomic(p, text, mode, group)
                    restored = True
                    if restart_attempted:
                        recreate(project)
                        healthy()
                        service_restored = True
                except Exception:
                    if restart_attempted:
                        service_restored = False
            raise ConfigurationFailure('configuration_failed', configuration_applied=not restored,
                                       previous_files_restored=restored, service_restored=service_restored,
                                       backup=backup.name) from None
        return {'source': KEYS[key], 'configuration_saved': True, 'service_recreated': True,
                'service_healthy': True, 'external_access_validated': False, 'backup': backup.name}


def main():
    if os.geteuid() != 0:
        raise RuntimeError('root_required')
    if sys.argv[1:] == ['status']:
        env = read_env(CONFIG / 'hub.env')
        print(json.dumps({k: bool(env.get(k)) for k in KEYS}))
    elif sys.argv[1:] == ['ready']:
        ensure_idle()
        print(json.dumps({'ready_to_configure': True, 'configuration_applied': False}))
    elif sys.argv[1:]:
        raise ValueError('unsupported_command')
    else:
        print(json.dumps(apply(json.loads(sys.stdin.read(16384)))))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Never let a parser error display an input secret in a traceback.
        result = error.result if isinstance(error, ConfigurationFailure) else {
            'error': type(error).__name__, 'configuration_applied': False}
        print(json.dumps(result))
        sys.exit(1)
