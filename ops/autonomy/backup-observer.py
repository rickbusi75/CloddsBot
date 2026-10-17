#!/usr/bin/env python3
"""Atomic, individually coherent snapshots for the existing lab backup."""
import argparse
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import time


def backup(root='/srv/lab', timeout=60):
    root = Path(root).resolve()
    for name in ('observer.sqlite', 'live.sqlite'):
        source = root / 'data/cloddsbot/autonomy' / name
        target = root / 'dumps/cloddsbot' / name
        if not source.exists():
            if target.exists():
                raise RuntimeError(name + ': source missing; previous snapshot retained')
            continue
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=name + '.', suffix='.pending', dir=target.parent)
        os.close(fd)
        temporary = Path(temporary)
        deadline = time.monotonic() + timeout

        def progress(status, remaining, total):
            if time.monotonic() >= deadline:
                raise TimeoutError(name + ': snapshot deadline exceeded')

        try:
            with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=10)) as incoming:
                with closing(sqlite3.connect(temporary, timeout=10)) as outgoing:
                    incoming.backup(outgoing, pages=256, progress=progress, sleep=0.05)
                    if outgoing.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise RuntimeError(name + ': snapshot integrity failed')
            temporary.chmod(0o600)
            # Persist the completed file before publishing it under its final name.
            with temporary.open('r+b') as completed:
                os.fsync(completed.fileno())
            os.replace(temporary, target)
            if os.name != 'nt':
                directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
        print(name + ': coherent SQLite snapshot ready')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default='/srv/lab')
    backup(parser.parse_args().root)
