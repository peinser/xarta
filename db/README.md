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
