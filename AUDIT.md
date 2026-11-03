# HI-5 POS Project Audit

Audit completed before implementation changes. The working tree was clean at
the start of the audit.

## Project inventory

- `app.py`: single-file Flask application with dashboard, product, inventory,
  billing, receipt, customer, settings, history, exports, and report routes.
- `templates/`: 11 Jinja templates for the existing screens.
- `static/`: existing CSS, billing/receipt JavaScript, and uploaded logos.
- `requirements.txt`: Flask and openpyxl only.
- `instance/hi5_pos.db`: tracked SQLite database, approximately 72 KiB.
- No `.gitignore`, `.env`, Vercel configuration, Flask-Migrate setup, or test
  suite was present.
- `venv/` and Python bytecode are excluded from this source inventory; the
  local virtual environment is not a deployable dependency specification.

## Existing database

The database was inspected read-only. It contains `settings`, `customers`,
`products`, `bills`, `bill_items`, `payments`, `stock_preferences`,
`stock_movements`, `inventory_migrations`, and `audit_log` tables, plus
`sqlite_sequence`. The current row counts are: settings 1; stock preferences 1;
inventory migrations 3; products, customers, bills, bill items, payments,
stock movements, and audit events 0. No user data was modified.

The schema has primary keys, a unique product code and customer phone, and
foreign keys, but no explicit query indexes or versioned migration framework.
Database creation and opening-stock/data migration logic run automatically
when the module is imported. An empty inventory can be seeded with sample
products unless the clear-all marker exists.

## Findings

1. **Database/deployment mismatch:** the app uses direct SQLite calls and a
   local file, not SQLAlchemy/MySQL. SQLite files and local uploads are not
   durable shared storage for Vercel serverless deployments. No production
   database, credentials, or deployment configuration is available to verify.
2. **Secret and debug defaults:** the Flask secret falls back to a known
   placeholder, and direct startup enables Flask debug mode. Session cookie
   policy and environment-specific configuration are absent.
3. **Request protection:** state-changing forms and APIs have no CSRF
   protection or authentication/authorization layer. No administrator boundary
   exists in the current app.
4. **Billing trust and replay:** bill creation accepts the sale price from the
   browser, permits unrecognized payment methods, and has no idempotency key.
   The frontend disables the button temporarily, but a retried request can
   create another bill. Invoice numbers have only second-level resolution.
5. **Validation and error handling:** product names/codes/prices and several
   other inputs are weakly validated; floating-point special values and
   untrusted values can reach business logic. Product/customer paths return
   generic redirects or expose some database error text. Several routes lack
   consistent error logging and transaction boundaries.
6. **Frontend data handling:** billing product fields are interpolated into
   `innerHTML`, which can turn stored product text into executable markup.
   CSV/XLSX exports do not consistently neutralize spreadsheet formulas.
7. **Feature gaps against the requested scope:** products lack barcode,
   per-product minimum stock, category/size/availability filters; reports are
   limited to monthly/category/top-product views; customer records have
   add/delete but no edit/address fields; suppliers and purchase entries are
   absent. A customer edit route and advanced report/date-range workflows do
   not exist.
8. **Operational safety:** no `.gitignore` or `.env.example` exists, and the
   SQLite database and local virtual environment are tracked. There are no
   automated tests, backup/import procedure, migration instructions, or
   production checks.

## Existing behavior and verification

- Jinja autoescaping is enabled in the HTML templates.
- Product variants already use separate product rows and unique SKUs, and
  billing performs a stock check/update inside a SQLite transaction.
- `python -m py_compile app.py` passed during audit.
- No HTTP, billing, migration, MySQL, or Vercel tests have been run. Importing
  the current application initializes/mutates its database, so application
  tests require an isolated database configuration first.

## Audit boundary

The audit did not connect to a local MySQL server, inspect any external
database, or deploy the application. No database rows were changed.

## Migration implementation follow-up (2026-11-03)

- Added pooled MySQL runtime access in `database.py` with UTF-8, parameter
  binding, request-scoped connection reuse, rollback-on-error behavior, and
  safe MySQL schema initialization.
- Added `migrate_sqlite_to_mysql.py`, which creates and verifies a consistent
  SQLite backup before importing, refuses populated MySQL target tables,
  preserves primary keys and relationships, checks row counts and row values,
  records a source fingerprint, and safely no-ops when rerun against the same
  source.
- The target MySQL server was reachable, reported MySQL 8.0.46 and `utf8mb4`,
  and initially contained no tables. The migration preserved one settings row,
  one stock preference row, and three inventory migration markers. The SQLite
  source had zero product, customer, bill, bill-item, payment, stock-movement,
  bill-request, or audit rows.
- The retained, integrity-checked pre-migration backup is
  `.migration-backups/hi5_pos_before_mysql_20261103_131447.db`. A second
  migration run verified idempotency without creating duplicates. The original
  SQLite database remains present.
- Application routes were smoke-tested against the migrated database.
  Automated MySQL-backed tests use a marked, separate test schema.
- Vercel deployment remains out of scope: no Vercel config was added, and
  uploaded images still use local storage.
