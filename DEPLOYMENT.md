# MySQL Migration and Deployment Readiness

## Migration completed

The Flask application now uses `mysql-connector-python`, a bounded connection
pool, parameterized MySQL queries, and `utf8mb4`. `DB_HOST`, `DB_PORT`,
`DB_NAME`, `DB_USER`, and `DB_PASSWORD` are loaded from root `.env`; credentials
are not logged. A request reuses one pooled connection and closes it at
teardown. SQL transactions commit on success and roll back on exceptions.

Before import, the existing MySQL `hi5_billing` schema was confirmed empty.
The SQLite database was backed up using SQLite's online backup API to
`.migration-backups\hi5_pos_before_mysql_20261103_131447.db`; the backup passed
`PRAGMA integrity_check`. The importer created InnoDB tables with UTF-8,
primary/unique keys, indexes, checks, and foreign keys, then preserved IDs and
relationships. Counts after migration:

| Table | SQLite | MySQL |
|---|---:|---:|
| settings | 1 | 1 |
| stock_preferences | 1 | 1 |
| inventory_migrations | 3 | 3 |
| products | 0 | 0 |
| customers | 0 | 0 |
| bills | 0 | 0 |
| bill_items | 0 | 0 |
| payments | 0 | 0 |
| stock_movements | 0 | 0 |
| bill_requests | 0 | 0 |
| audit_log | 0 | 0 |

The source contained no product, customer, or sales rows to transfer. The
SQLite database remains in `instance\hi5_pos.db`; the verified backup is also
retained separately. Rerunning the migration against the same source
fingerprint was tested and did not insert duplicate records. The importer
refuses to merge into any populated target table.

The migration script inspects columns, indexes, constraints, foreign keys,
SQLite integrity, and row counts. On the first import it also verifies each
record's values before committing.

## Database provider and Vercel

The current local MySQL integration is complete. For Vercel, the production
database must be a managed MySQL-compatible provider with TLS and verified
connection/backup limits; do not point Vercel at a desktop `localhost`
database. For a low-volume Vercel deployment, TiDB Cloud Starter is a
cost-effective option to evaluate first, subject to confirming its TLS,
connection limits, and backup/restore guarantees.

If you prefer conventional managed MySQL with documented recovery tiers,
evaluate Aiven for MySQL. Prices and plan names change, so verify current
backup retention and connection caps before choosing. Vercel Python Functions
can run Flask, but the service's scaling makes bounded/reused connections
essential. This repository still has no Vercel deployment configuration, and
uploads remain on the local filesystem; move them to durable object storage
before deploying.

Sources checked 2026-11-03:

- [Vercel Flask deployment](https://vercel.com/docs/frameworks/backend/flask)
- [Vercel Function concurrency scaling](https://vercel.com/docs/functions/concurrency-scaling)
- [TiDB Cloud overview](https://docs.pingcap.com/tidbcloud/tidb-cloud-intro/)
- [TiDB Cloud pricing](https://www.pingcap.com/pricing/)
- [Aiven MySQL pricing](https://aiven.io/mysql)
- [Aiven MySQL availability and backup history](https://aiven.io/docs/products/mysql/concepts/high-availability)

## Local setup

From the repository root in PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
if (!(Test-Path .env)) { Copy-Item .env.example .env }
python -c "import secrets; print(secrets.token_urlsafe(48))"
python -c "from getpass import getpass; from werkzeug.security import generate_password_hash; print(generate_password_hash(getpass()))"
```

Keep existing valid credentials; set generated values only if creating new
credentials. `.env` is ignored by Git. Run the migration and app:

```powershell
python migrate_sqlite_to_mysql.py
python app.py
```

Open `http://127.0.0.1:5001`. The verified migration backup is retained under
`.migration-backups\`. Tests use a separate marked MySQL database:

```powershell
python -m unittest discover -s tests -v
```

## Required steps before Vercel

1. Provision a managed MySQL-compatible production database with TLS and
   documented backup/restore, and test its recovery procedure.
2. Set `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`,
   `SECRET_KEY`, and `ADMIN_PASSWORD_HASH` in Vercel environment variables.
   Never paste credentials into source or commit `.env`.
3. Configure image uploads to durable object storage.
4. Add and validate Vercel's Flask entry point/configuration and bounded
   connections against a non-production database.
5. Test sign-in, product creation, billing, stock deduction, rollback, and
   persistence on the deployed preview before production cutover.

No Vercel deployment or cloud production database was configured or tested in
this task.
