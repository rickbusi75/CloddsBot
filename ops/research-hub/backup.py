#!/usr/bin/env python3
"""Host helper: atomic logical Postgres dump, included by the lab backup."""
import os
from pathlib import Path
import subprocess
import tempfile


def backup():
    target = Path('/srv/lab/dumps/research-hub/research.dump')
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='research-', suffix='.pending', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            subprocess.run(['docker', 'exec', 'research-db', 'pg_dump', '-U', 'research', '-d', 'research', '-Fc'], stdout=output, stderr=subprocess.PIPE, check=True, timeout=120)
            output.flush()
            os.fsync(output.fileno())
        subprocess.run(['docker', 'exec', '-i', 'research-db', 'pg_restore', '--list'], stdin=open(name, 'rb'), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True, timeout=30)
        os.chmod(name, 0o600)
        os.replace(name, target)
        print('Research Hub: coherent logical PostgreSQL dump ready')
    finally:
        Path(name).unlink(missing_ok=True)


if __name__ == '__main__':
    backup()
