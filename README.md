# HI-5 FOOTWEAR & BOUTIQUES – POS

A local Flask + SQLite retail POS starter application for footwear and clothing.
No paid APIs, API keys, GST or cloud service required.

## Run in VS Code (Windows PowerShell)

```powershell
cd hi5_pos
py -3.10 -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000

If PowerShell blocks activation, run:
```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\\.venv\\Scripts\\Activate.ps1
```

The SQLite database is created automatically at `instance/hi5_pos.db`.
Uploaded product images and the optional shop logo are stored under `static/uploads/`.
Back up the `instance` and `static/uploads` folders regularly.

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
This is a locally hosted single-shop application. Before using it as the only record system,
test with sample transactions and keep regular database backups. For multiple simultaneous
cashiers across different devices, deploy with a production WSGI server and shared database.

Browsers do not let websites choose a WhatsApp contact or attach a local image through a
WhatsApp share link. When native image sharing is unavailable, the POS downloads the PNG
and opens a pre-addressed WhatsApp message; the image must then be attached manually.
