# Autonomy database snapshots

The host runs `backup-cloddsbot.py` from the existing `lab-backup` job. Its final
`runpy.run_path` invokes `/srv/lab/scripts/backup-clodds-observer.py`, installed
from `ops/autonomy/backup-observer.py`. Updating the repository or container image
alone does not update this host helper.

The helper snapshots `observer.sqlite` and, when present, `live.sqlite` into
`/srv/lab/dumps/cloddsbot`. Each database uses the SQLite online backup API,
integrity verification, a private temporary file, file synchronization and an
atomic rename. Linux also synchronizes the target directory. The snapshots are
individually coherent; they are not a transaction spanning both databases.

A corrupt source or expired deadline preserves the previous valid dump and fails
the backup job. If a previously backed-up source disappears, the helper refuses
to publish a new apparently current archive with an unexplained stale dump.
An absent optional live ledger with no prior dump remains absent. No ledger is
activated or initialized, and there are no exchange or network requests.

Host installation must also extend `/usr/local/sbin/lab-backup` with this tar
exclusion, immediately beside the existing observer exclusion:

```bash
--exclude='srv/lab/data/cloddsbot/autonomy/live.sqlite*' \
```

Archive the coherent dumps, rather than the actively changing databases and their
WAL/SHM files. Preserve all other projects' hooks/exclusions and the global backup
lock. Save the previous helper and host script, validate syntax, run the existing
job and verify its checksum. Restore the selected dumps from the resulting archive
into an isolated directory and check integrity; never restore over running data
as a test. A reservation restored in `SENDING` remains uncertain and must not be
resent. Verify that the updated helper and exclusion are actually installed.

Tests use only temporary databases, including an open WAL, interrupted/corrupt
snapshot sources, timeout and an absent optional ledger:

```bash
python3 -m unittest discover -s ops/autonomy -p test_backup.py -v
```
