# Database versioning

We're using `sqitch` to version our database changes.

## Deploying changes locally

In order to deploy your changes to the local database, use the following command:

```console
make db-deploy
```

This creates the local `archive` database on the same PostgreSQL server and deploys
both independently owned Sqitch projects. The equivalent direct commands are:

```console
sqitch --chdir db deploy db:postgres://dev:dev@postgres/dev
sqitch --chdir db/archive deploy db:postgres://dev:dev@postgres/archive
```

The main project preserves historical archive migrations for deployed Sqitch plans,
then removes those objects in `00022.archive-database-boundary`. Archive tables,
types, triggers, and indexes are owned by `db/archive` and must be deployed to a
separate database. The boundary migration refuses to remove a non-empty historical
archive. An existing installation must copy its archive rows first; preserve internal
IDs, sequence positions, version links, idempotency rows, and immutable storage
coordinates during that cutover.

After deploying the archive Sqitch project, use the cutover utility while archive
writes are stopped:

```console
uv run --frozen python tools/migrate_archive_database.py \
  postgres://dev:dev@postgres/dev \
  postgres://dev:dev@postgres/archive
```

The utility locks the source archive tables against writes, converts document type
foreign keys to immutable identifiers, copies the three archive tables in one target
transaction, preserves sequence positions, and verifies every copied row. It removes
the legacy source rows only after the target commits successfully. A rerun is accepted
only when the target exactly matches the source.

## Archive metadata search index upgrade

`db/archive/00001.archive-metadata-search` is an incremental change depending on
`00000.archive`. Existing deployed changes and their plan identities are preserved.

```console
sqitch --chdir db/archive deploy --verify db:postgres://dev:dev@postgres/archive
```

The migration builds `archive_document_versions_metadata_gin_idx` using
`CREATE INDEX CONCURRENTLY ... USING gin (metadata jsonb_path_ops)`. Deploy and
revert deliberately have no enclosing transaction: concurrent index operations
cannot run inside one. Do not wrap these scripts in an external transaction.
The deployment includes its verification script, so `IF NOT EXISTS` cannot silently
accept a conflicting index definition even when `--verify` is omitted.

On retry, an invalid leftover of the expected GIN index is dropped concurrently and
rebuilt. A still-running build is left alone and deployment reports an error; retry
after it completes or has been cancelled. A valid matching index from an interrupted
deployment is reused. A conflicting definition fails verification rather than
being silently accepted or automatically replacing a valid unrelated definition.
Verification checks the table, column, GIN method, `jsonb_path_ops`, absence of
predicates/expressions, and both index validity and readiness.

At large archive sizes this is a substantial index build, despite allowing writes.
Budget migration execution time and disk/WAL capacity for the scans; long-running
transactions can delay concurrent build phases. Deploy the index before enabling
search traffic. Reverting removes the index, not archive data, but removes that
performance support:

```console
sqitch --chdir db/archive revert --to 00000.archive -y db:postgres://dev:dev@postgres/archive
```

PostgreSQL/Sqitch integration tests exercise a fresh deployment, an upgrade with
existing immutable snapshots, revert/redeploy, cancellation leaving an invalid
index, active-build protection, valid-index reuse, and conflicting-definition
rejection. They also check HEAD-only filtering and real keyset traversal.
CI runs them against its PostgreSQL service. Locally, provide a disposable instance
and a role allowed to create databases; tests create and drop only their own
randomly named databases:

```console
ARCHIVE_POSTGRES_TEST_DSN=postgresql://dev:dev@localhost:5432/postgres \
  uv run --frozen pytest tests/services/test_archive_search_postgres.py
```

`sqitch` must be on PATH; the optional `SQITCH` environment variable can specify its
command. Integration tests are skipped without `ARCHIVE_POSTGRES_TEST_DSN`.
