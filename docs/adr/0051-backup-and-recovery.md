# 0051. Backup and recovery

**Status:** Accepted (2026-11-19). Recovery targets (decision 6): RPO 15 minutes, RTO 4 hours.

## Decision
- **Postgres: pgBackRest** (MIT). Continuous WAL archiving (`archive_timeout` 5 min, well within the RPO), nightly encrypted base backups (full weekly, differential daily), point-in-time recovery. Example configuration: `deploy/backup/pgbackrest.conf`.
- **Embeddings are in the backup.** Regenerating 20 million of them takes days, far beyond the RTO.
- **Blobs:** a versioned bucket (S3), or for local storage a scheduled sync to a second location.
- **Keys apart from data.** The EnterpriseRAG master key and the pgBackRest passphrase are backed up separately from the backups themselves: someone holding both a backup and its key can read everything.
- **`erag backup verify`** checks a (restored) installation: schema at this release's head, every stored secret decrypts with the configured master key (values discarded, never printed), current blobs present with matching hashes (sampled or `--full`), and every ready document has embeddings. It exits non-zero on any failure and audits only the outcome.
- **Restore drill** (`scripts/restore_drill.py`): records searches as a persona, dumps and restores into a throw-away database, migrates it to this release, runs `backup verify`, logs in through the real app, repeats the searches and compares. The dump in the drill is logical (`pg_dump -Fc`); after a pgBackRest restore the same verification steps apply. Run it after every upgrade and at least quarterly.

## Consequences
- A wrong or missing master key is found at verification, not at the first connector sync after a disaster.
- The drill's logical dump proves restorability of the data and schema; it does not exercise pgBackRest's PITR path, which the operations guide documents and the operator must rehearse on their own infrastructure.
