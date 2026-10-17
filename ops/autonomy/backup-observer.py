#!/usr/bin/env python3
"""Coherent observer snapshot invoked by the existing lab backup."""
from pathlib import Path
import sqlite3

source = Path('/srv/lab/data/cloddsbot/autonomy/observer.sqlite')
target = Path('/srv/lab/dumps/cloddsbot/observer.sqlite')
if source.exists():
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect('file:' + str(source) + '?mode=ro', uri=True) as incoming:
        with sqlite3.connect(target) as outgoing:
            incoming.backup(outgoing)
            if outgoing.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Observer snapshot integrity failed')
    target.chmod(0o600)
    print('Observer coherent SQLite snapshot ready')
