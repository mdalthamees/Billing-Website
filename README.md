# HI-5 FOOTWEAR & BOUTIQUES – POS

A Flask + MySQL retail POS application for footwear and clothing.

## SQLite-to-MySQL migration and local run (Windows PowerShell)

```powershell
cd C:\path\to\Billing-Website
py -3.12 -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
pip install -r requirements.txt
if (!(Test-Path .env)) { Copy-Item .env.example .env }
# Keep existing .env values; configure the MySQL connection and admin hash there.
python migrate_sqlite_to_mysql.py
python app.py
```

Open http://127.0.0.1:5001

If PowerShell blocks activation, run:
```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\\.venv\\Scripts\\Activate.ps1
```

The importer makes a verified SQLite backup under `.migration-backups\` before
creating MySQL tables or importing records. It preserves SQLite IDs and
relationships, verifies table row counts and record values, and records a
fingerprint so rerunning it against the same snapshot does not duplicate rows.
It refuses to import into populated target tables or overwrite a previous
import. The original `instance\hi5_pos.db` is retained. Back up the MySQL
database and `static\uploads\` regularly.

Generate a local secret in PowerShell with:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
python -c "from getpass import getpass; from werkzeug.security import generate_password_hash; print(generate_password_hash(getpass()))"
```

Paste the first result into `.env` as `SECRET_KEY=...` and the second as
`ADMIN_PASSWORD_HASH=...`. The app requires administrator sign-in. Do not
commit `.env`. Tests use the marked `hi5_billing_test_adfe86e7` MySQL database,
never the business database.

Open **History & Excel** from the sidebar to review recent activity or download the complete workbook. Individual deletions and the clear-all operation archive record snapshots before removing current records. Records deleted before the archive feature was added cannot be reconstructed; records still present are included in the live-data sheets.

## Included
- Product catalog with separate department, category, size and colour variants.
- Cart billing across footwear and dress departments in one bill.
- Stock validation and automatic stock deduction, with stock-in/out movement history and configurable low-stock alerts.
- Stock Maintenance dashboard with live stock, sales, availability and product-level inventory totals.
- Customer records and outstanding balances.
- Individual product, customer, bill, payment and reversible stock-movement deletion; the Settings page also provides a confirmed clear-all option.
- Permanent History & Excel archive for product/customer additions and edits, bills with line items and payments, stock movements, and deleted/cleared records. Download a workbook with separate activity, products, bills, bill-items, payments, stock, customer, and settings sheets.
- Cash / UPI / Card payment and partial payments.
- Printable receipt with shop logo; full-receipt image sharing to WhatsApp where supported, with a download fallback (no paid API).
- After billing, the POS generates a PNG receipt and opens the device's native file-share flow when supported; WhatsApp links target +91 8667579407 without third-party APIs.
- Sales history, filters, CSV and Excel exports.
- Dashboard summary and sales chart.
- Product/customer management and shop settings.
- Responsive mobile navigation, card-based mobile tables, and reduced-motion-aware interface animations.

## Important
This is a locally hosted single-shop application. Keep regular MySQL backups.
For multiple simultaneous cashiers across different devices, deploy with a
production WSGI server and shared database.

Browsers do not let websites choose a WhatsApp contact or attach a local image through a
WhatsApp share link. When native image sharing is unavailable, the POS downloads the PNG
and opens a pre-addressed WhatsApp message; the image must then be attached manually.

## Database and deployment status

The application now uses pooled `mysql-connector-python` connections with
parameterized queries and UTF-8 (`utf8mb4`). The SQLite file is retained as a
backup/source for the idempotent migration command. Vercel deployment and
durable cloud storage for uploaded images are separate tasks; do not use
serverless local files as permanent storage.
