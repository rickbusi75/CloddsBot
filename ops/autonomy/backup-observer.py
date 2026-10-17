#!/usr/bin/env python3
"""Atomic coherent snapshots of research and optional live ledger databases."""
import argparse
import os
from pathlib import Path
import sqlite3
import tempfile

def backup(root='/srv/lab'):
    root = Path(root)
    for name in ('observer.sqlite', 'live.sqlite'):
        source = root / 'data/cloddsbot/autonomy' / name
        if not source.exists():
            continue
        target = root / 'dumps/cloddsbot' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=name + '.', suffix='.pending', dir=target.parent)
        os.close(descriptor)
        temporary = Path(temporary)
        try:
            with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=30) as incoming:
                with sqlite3.connect(temporary, timeout=30) as outgoing:
                    incoming.backup(outgoing)
                    if outgoing.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise RuntimeError('Autonomy snapshot integrity failed')
            temporary.chmod(0o600)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        print(name + ': coherent SQLite snapshot ready')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default='/srv/lab')
    backup(parser.parse_args().root)
