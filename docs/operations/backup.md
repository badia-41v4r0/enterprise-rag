# Backup and restore

Targets: **RPO 15 minutes, RTO 4 hours** (ADR 0051). Four things must be backed up, and two of them separately:

| What | How | Where |
|---|---|---|
| Postgres (everything, embeddings included) | pgBackRest: WAL archiving + nightly encrypted base backups | a backup bucket, versioned |
| Blobs (original files) | a versioned bucket (S3 backend), or a scheduled sync of `ERAG_BLOB_DIR` | a second location |
| Master key (`ERAG_SECRETS_KEY` / key file) | offline copy (password manager, HSM, sealed envelope) | **apart from the backups** |
| pgBackRest passphrase, S3 credentials | same as the master key | **apart from the backups** |

Whoever holds a backup and its keys can read every document and secret. Keep them apart.

## Set up pgBackRest
1. Install pgBackRest on the Postgres host (or use CloudNativePG's Barman integration on Kubernetes, which gives the same PITR).
2. Copy `deploy/backup/pgbackrest.conf` to `/etc/pgbackrest/pgbackrest.conf`, fill in the bucket, and put the key and passphrase in the environment (`PGBACKREST_REPO1_S3_KEY`, `PGBACKREST_REPO1_S3_KEY_SECRET`, `PGBACKREST_REPO1_CIPHER_PASS`).
3. In `postgresql.conf`: `archive_mode = on`, `archive_command = 'pgbackrest --stanza=erag archive-push %p'`, `archive_timeout = 300`, `wal_level = replica`. Restart Postgres.
4. `pgbackrest --stanza=erag stanza-create`, then `pgbackrest --stanza=erag check`.
5. Schedule `--type=full` weekly and `--type=diff` daily.

## Restore (point in time)
1. Stop the app and the workers.
2. `pgbackrest --stanza=erag --delta --type=time "--target=2026-11-19 10:15:00+00" restore`, start Postgres, let it replay WAL to the target.
3. Restore blobs to the same point (bucket versioning, or the synced copy).
4. Put the master key in place.
5. `alembic upgrade head` (no-op unless you restored into a newer release).
6. **`erag backup verify --full`**: schema at head, every secret decrypts with this key, every current blob present with its hash, every ready document has embeddings. Do not open the installation to users until it passes.
7. Start the app and the workers; check `/health/ready`.

## The restore drill
Run it after every upgrade and at least quarterly, on a copy:

```
uv run python scripts/restore_drill.py \
    --source postgresql+asyncpg://erag:...@db:5432/erag --container <postgres container> \
    --email <an existing user> --password <their password>
```

It records searches as that user, dumps and restores into a throw-away database, migrates it, runs `backup verify`, logs in through the real app, repeats the searches and compares. It prints ids and counts only. Last run on the evaluation database (2026-11-19): migrated 0019 → 0020, verify passed, 20/20 searches identical.

The drill's dump is logical (`pg_dump -Fc`). It proves that a backup of this release restores, migrates and answers the same; rehearse the pgBackRest PITR path itself (above) on your own infrastructure.
