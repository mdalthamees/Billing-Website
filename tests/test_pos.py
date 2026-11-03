import os
import hmac
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import mysql.connector
import database
from backup_manager import list_backup_history
from dotenv import dotenv_values
from werkzeug.security import generate_password_hash

_env = dotenv_values(".env")
_test_database = "hi5_billing_test_adfe86e7"
_setup_connection = mysql.connector.connect(
    host=_env.get("DB_HOST", "localhost"),
    port=int(_env.get("DB_PORT") or 3306),
    user=_env.get("DB_USER"),
    password=_env.get("DB_PASSWORD"),
    charset="utf8mb4",
    use_unicode=True,
)
_setup_cursor = _setup_connection.cursor()
_setup_cursor.execute(
    "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s",
    (_test_database,),
)
_database_exists = _setup_cursor.fetchone() is not None
if not _database_exists:
    _setup_cursor.execute(
        f"CREATE DATABASE `{_test_database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
    )
_setup_cursor.close()
_setup_connection.close()
_setup_connection = mysql.connector.connect(
    host=_env.get("DB_HOST", "localhost"),
    port=int(_env.get("DB_PORT") or 3306),
    database=_test_database,
    user=_env.get("DB_USER"),
    password=_env.get("DB_PASSWORD"),
    charset="utf8mb4",
    use_unicode=True,
)
_setup_cursor = _setup_connection.cursor()
_setup_cursor.execute("SHOW TABLES")
_existing_tables = {row[0] for row in _setup_cursor.fetchall()}
_marker_table = "hi5_test_database_marker"
if _database_exists and _existing_tables and _marker_table not in _existing_tables:
    raise RuntimeError("Refusing to use a MySQL test database without its marker.")
_setup_cursor.execute(
    f"""CREATE TABLE IF NOT EXISTS `{_marker_table}` (
        marker VARCHAR(64) NOT NULL PRIMARY KEY
    ) ENGINE=InnoDB"""
)
_setup_cursor.execute(f"SELECT marker FROM `{_marker_table}`")
if any(row[0] != "hi5-billing-workspace-tests-v1" for row in _setup_cursor.fetchall()):
    raise RuntimeError("The configured MySQL test database has an unexpected marker.")
_setup_cursor.execute(
    f"INSERT IGNORE INTO `{_marker_table}` (marker) VALUES (%s)",
    ("hi5-billing-workspace-tests-v1",),
)
_setup_connection.commit()
_setup_cursor.close()
_setup_connection.close()
for _key in ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD"):
    if _env.get(_key) is not None:
        os.environ[_key] = str(_env[_key])
os.environ["DB_NAME"] = _test_database
os.environ["MYSQL_POOL_SIZE"] = "2"
os.environ["SECRET_KEY"] = "test-only-secret"
os.environ["ADMIN_PASSWORD_HASH"] = generate_password_hash("test-only-password")

from app import app, db


class PosTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        previous_backup_directory = app.config["BACKUP_DIRECTORY"]
        backup_directory = tempfile.TemporaryDirectory()
        app.config["BACKUP_DIRECTORY"] = backup_directory.name
        self.addCleanup(backup_directory.cleanup)
        self.addCleanup(app.config.__setitem__, "BACKUP_DIRECTORY", previous_backup_directory)
        self.client = app.test_client()
        with db() as connection:
            for table in (
                "bill_requests",
                "bill_items",
                "payments",
                "stock_movements",
                "bills",
                "customers",
                "products",
                "audit_log",
            ):
                connection.execute(f"DELETE FROM {table}")
            connection.execute(
                "UPDATE stock_preferences SET minimum_stock=5 WHERE id=1"
            )
            connection.execute(
                """UPDATE settings SET shop_name=?,address=?,phone='',
                    whatsapp_phone='',logo='' WHERE id=1""",
                (
                    "HI-5 FOOTWEAR & BOUTIQUES",
                    "Gandhi Street, R.S. Mangalam, Ramanathapuram, Tamil Nadu",
                ),
            )
            cursor = connection.execute(
                """INSERT INTO products
                (name,code,department,category,selling_price,stock,created_at)
                VALUES(?,?,?,?,?,?,?)""",
                ("Test Shoe", "TEST-001", "Footwear", "Men's Shoes", 125.50, 3, "2026-01-01"),
            )
            self.product_id = cursor.lastrowid
            connection.execute(
                """INSERT INTO stock_movements
                (product_id,product_name,product_code,movement_type,quantity,note,created_at)
                VALUES(?,?,?,'opening',?,?,?)""",
                (
                    self.product_id,
                    "Test Shoe",
                    "TEST-001",
                    3,
                    "Test fixture opening balance",
                    "2026-01-01",
                ),
            )
        self.client.get("/login")
        with self.client.session_transaction() as session:
            login_token = session["_csrf_token"]
        login = self.client.post(
            "/login",
            data={"password": "test-only-password", "csrf_token": login_token},
        )
        self.assertEqual(login.status_code, 302)
        self.client.get("/products")
        with self.client.session_transaction() as session:
            self.csrf_token = session["_csrf_token"]

    def post_headers(self):
        return {"X-CSRF-Token": self.csrf_token}

    def test_existing_pages_render_and_security_headers_are_set(self):
        for path in (
            "/",
            "/stock",
            "/products",
            "/billing",
            "/customers",
            "/sales",
            "/settings",
            "/history",
            "/reports",
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(response.headers["X-Frame-Options"], "DENY")

    def test_post_without_csrf_token_is_rejected(self):
        response = self.client.post("/products/save", data={"name": "Blocked"})
        self.assertEqual(response.status_code, 400)
        with db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM products").fetchone()[0], 1)

    def test_protected_pages_require_admin_login(self):
        client = app.test_client()
        response = client.get("/products")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)
        backups = client.get("/admin/backups")
        self.assertEqual(backups.status_code, 302)
        self.assertIn("/login", backups.location)

    def test_login_temporarily_locks_after_repeated_failures(self):
        remote_addr = "192.0.2.55"
        client = app.test_client()
        client.environ_base["REMOTE_ADDR"] = remote_addr
        client.get("/login")
        with client.session_transaction() as session:
            csrf_token = session["_csrf_token"]
        for attempt in range(5):
            response = client.post(
                "/login",
                data={"password": "incorrect-password", "csrf_token": csrf_token},
            )
        self.assertEqual(response.status_code, 429)
        blocked = client.post(
            "/login",
            data={"password": "test-only-password", "csrf_token": csrf_token},
        )
        self.assertEqual(blocked.status_code, 429)
        identifier = hmac.new(
            app.secret_key.encode(), remote_addr.encode(), "sha256"
        ).hexdigest()
        with db() as connection:
            connection.execute(
                "DELETE FROM admin_login_attempts WHERE identifier=?", (identifier,)
            )

    def test_remote_mysql_requires_certificate_and_hostname_verification(self):
        previous_pool = database._pool
        previous_config = database._pool_config
        remote_config = {
            "MYSQL_POOL_SIZE": 1,
            "DB_HOST": "mysql.example.invalid",
            "DB_PORT": 3306,
            "DB_NAME": "hi5_test",
            "DB_USER": "hi5_app",
            "DB_PASSWORD": "not-a-real-password",
            "DB_SSL_CA": "",
            "DB_CONNECT_TIMEOUT": 5,
        }
        try:
            database._pool = None
            database.configure_mysql(remote_config)
            with self.assertRaisesRegex(RuntimeError, "DB_SSL_CA"):
                database.mysql_pool()
            remote_config["DB_SSL_CA"] = r"C:\certs\mysql-ca.pem"
            database.configure_mysql(remote_config)
            with patch("database.pooling.MySQLConnectionPool") as pool_factory:
                database.mysql_pool()
            options = pool_factory.call_args.kwargs
            self.assertTrue(options["ssl_verify_cert"])
            self.assertTrue(options["ssl_verify_identity"])
            self.assertFalse(options["ssl_disabled"])
        finally:
            database._pool = previous_pool
            database._pool_config = previous_config

    def test_manual_backup_and_restore_create_safety_backup(self):
        response = self.client.get("/admin/backups")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Create backup now", response.data)
        response = self.client.post(
            "/admin/backups/create", headers=self.post_headers()
        )
        self.assertEqual(response.status_code, 302)
        backups = list_backup_history(app.config["BACKUP_DIRECTORY"])
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0]["status"], "success")

        with db() as connection:
            connection.execute(
                "UPDATE products SET stock=8 WHERE id=?", (self.product_id,)
            )
        response = self.client.post(
            "/admin/backups/restore",
            data={
                "backup_filename": backups[0]["filename"],
                "confirm_restore": "RESTORE",
                "csrf_token": self.csrf_token,
            },
        )
        self.assertEqual(response.status_code, 302)
        with db() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT stock FROM products WHERE id=?", (self.product_id,)
                ).fetchone()[0],
                3,
            )
        self.assertEqual(
            len([record for record in list_backup_history(app.config["BACKUP_DIRECTORY"])
                 if record["status"] == "success"]),
            2,
        )

    def test_backup_rejects_tampering_and_path_traversal(self):
        response = self.client.post(
            "/admin/backups/restore",
            data={
                "backup_filename": "../../.env",
                "confirm_restore": "RESTORE",
                "csrf_token": self.csrf_token,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(list_backup_history(app.config["BACKUP_DIRECTORY"]), [])

        self.client.post("/admin/backups/create", headers=self.post_headers())
        backup = list_backup_history(app.config["BACKUP_DIRECTORY"])[0]
        path = Path(app.config["BACKUP_DIRECTORY"]) / backup["filename"]
        with path.open("ab") as backup_file:
            backup_file.write(b"modified")
        download = self.client.get(
            f"/admin/backups/{backup['filename']}/download"
        )
        self.assertEqual(download.status_code, 404)
        restore = self.client.post(
            "/admin/backups/restore",
            data={
                "backup_filename": backup["filename"],
                "confirm_restore": "RESTORE",
                "csrf_token": self.csrf_token,
            },
        )
        self.assertEqual(restore.status_code, 302)
        with db() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT stock FROM products WHERE id=?", (self.product_id,)
                ).fetchone()[0],
                3,
            )
        self.assertEqual(
            len(list_backup_history(app.config["BACKUP_DIRECTORY"])),
            1,
        )

    def test_database_connection_failure_is_recorded_without_exposing_details(self):
        with patch("app.mysql_pool", side_effect=mysql.connector.Error("private connection detail")):
            response = self.client.post(
                "/admin/backups/create", headers=self.post_headers()
            )
        self.assertEqual(response.status_code, 302)
        self.assertNotIn(b"private connection detail", response.data)
        history = list_backup_history(app.config["BACKUP_DIRECTORY"])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["status"], "failure")
        self.assertNotIn("private connection detail", history[0]["message"])

    def test_shop_phone_and_whatsapp_numbers_validate_and_persist(self):
        response = self.client.post(
            "/settings",
            data={
                "shop_name": "HI-5 Test",
                "address": "R.S. Mangalam",
                "phone": "+91 (86675) 79407",
                "whatsapp_phone": "+44 7700 900123",
                "csrf_token": self.csrf_token,
            },
        )
        self.assertEqual(response.status_code, 302)
        with db() as connection:
            row = connection.execute(
                "SELECT phone,whatsapp_phone FROM settings WHERE id=1"
            ).fetchone()
        self.assertEqual(row["phone"], "918667579407")
        self.assertEqual(row["whatsapp_phone"], "447700900123")

        response = self.client.post(
            "/settings",
            data={
                "shop_name": "HI-5 Test",
                "address": "R.S. Mangalam",
                "phone": "123",
                "whatsapp_phone": "9876543210",
                "csrf_token": self.csrf_token,
            },
        )
        self.assertEqual(response.status_code, 302)
        with db() as connection:
            row = connection.execute(
                "SELECT phone,whatsapp_phone FROM settings WHERE id=1"
            ).fetchone()
        self.assertEqual(row["phone"], "918667579407")
        self.assertEqual(row["whatsapp_phone"], "447700900123")

    def test_product_validation_rejects_empty_name_and_invalid_price(self):
        response = self.client.post(
            "/products/save",
            data={
                "name": " ",
                "code": "TEST-002",
                "department": "Footwear",
                "category": "Shoes",
                "selling_price": "10",
                "stock": "1",
                "csrf_token": self.csrf_token,
            },
        )
        self.assertEqual(response.status_code, 302)
        response = self.client.post(
            "/products/save",
            data={
                "name": "Bad Price",
                "code": "TEST-003",
                "department": "Footwear",
                "category": "Shoes",
                "selling_price": "NaN",
                "stock": "1",
                "csrf_token": self.csrf_token,
            },
        )
        self.assertEqual(response.status_code, 302)
        with db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM products").fetchone()[0], 1)

    def test_product_creation_persists_across_page_refresh(self):
        response = self.client.post(
            "/products/save",
            data={
                "name": "New Dress",
                "code": "TEST-004",
                "department": "Dress",
                "category": "Kurtis",
                "purchase_price": "200",
                "selling_price": "450",
                "stock": "4",
                "csrf_token": self.csrf_token,
            },
        )
        self.assertEqual(response.status_code, 302)
        first = self.client.get("/products?q=TEST-004")
        refreshed = self.client.get("/products?q=TEST-004")
        self.assertIn(b"New Dress", first.data)
        self.assertIn(b"New Dress", refreshed.data)
        with db() as connection:
            row = connection.execute(
                "SELECT stock FROM products WHERE code=?", ("TEST-004",)
            ).fetchone()
            self.assertEqual(row["stock"], 4)

    def test_bill_uses_database_price_and_idempotency_prevents_double_sale(self):
        payload = {
            "idempotency_key": "a" * 32,
            "items": [{"id": self.product_id, "quantity": 1, "price": 0}],
            "discount": 0,
            "received": 125.50,
            "payment_method": "Cash",
        }
        first = self.client.post(
            "/api/bills", json=payload, headers=self.post_headers()
        )
        self.assertEqual(first.status_code, 200, first.get_json())
        first_data = first.get_json()
        self.assertEqual(first_data["receipt"]["total"], 125.5)

        replay = self.client.post(
            "/api/bills", json=payload, headers=self.post_headers()
        )
        self.assertEqual(replay.status_code, 200, replay.get_json())
        self.assertEqual(replay.get_json()["bill_id"], first_data["bill_id"])
        with db() as connection:
            self.assertEqual(connection.execute("SELECT stock FROM products").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM bills").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM bill_items").fetchone()[0], 1)

    def test_insufficient_stock_rolls_back_bill_and_stock(self):
        response = self.client.post(
            "/api/bills",
            json={
                "idempotency_key": "b" * 32,
                "items": [{"id": self.product_id, "quantity": 4}],
                "payment_method": "UPI",
            },
            headers=self.post_headers(),
        )
        self.assertEqual(response.status_code, 400)
        with db() as connection:
            self.assertEqual(connection.execute("SELECT stock FROM products").fetchone()[0], 3)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM bills").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM bill_items").fetchone()[0], 0)

    def test_bill_failure_after_stock_update_rolls_back_everything(self):
        with patch("app.audit", side_effect=ValueError("forced audit failure")):
            response = self.client.post(
                "/api/bills",
                json={
                    "idempotency_key": "9" * 32,
                    "items": [{"id": self.product_id, "quantity": 1}],
                    "payment_method": "Cash",
                },
                headers=self.post_headers(),
            )
        self.assertEqual(response.status_code, 400)
        with db() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT stock FROM products WHERE id=?", (self.product_id,)
                ).fetchone()[0],
                3,
            )
            for table in ("bills", "bill_items", "payments", "bill_requests"):
                self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM stock_movements WHERE movement_type='sale'"
                ).fetchone()[0],
                0,
            )

    def test_bill_rejects_invalid_payment_method(self):
        response = self.client.post(
            "/api/bills",
            json={
                "idempotency_key": "c" * 32,
                "items": [{"id": self.product_id, "quantity": 1}],
                "payment_method": "wire",
            },
            headers=self.post_headers(),
        )
        self.assertEqual(response.status_code, 400)

    def test_billing_category_filter_and_customer_sales_summary(self):
        billing_page = self.client.get("/billing")
        self.assertEqual(billing_page.status_code, 200)
        self.assertIn(b'Men&#39;s Shoes', billing_page.data)
        filtered_products = self.client.get("/api/products?category=Men%27s%20Shoes")
        self.assertEqual(filtered_products.status_code, 200)
        self.assertEqual(
            [product["code"] for product in filtered_products.get_json()],
            ["TEST-001"],
        )

        response = self.client.post(
            "/api/bills",
            json={
                "idempotency_key": "7" * 32,
                "items": [{"id": self.product_id, "quantity": 1}],
                "received": 125.50,
                "payment_method": "UPI",
                "customer_name": "QA Sales Search",
                "customer_phone": "9000000007",
            },
            headers=self.post_headers(),
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        bill = response.get_json()
        sales = self.client.get("/sales?q=9000000007")
        self.assertEqual(sales.status_code, 200)
        self.assertIn(bill["bill_no"].encode(), sales.data)
        self.assertIn("₹125.50".encode(), sales.data)
        self.assertIn(b"UPI", sales.data)
        self.assertIn(b"Reprint receipt", sales.data)

    def test_sales_date_reports_and_exports_work_with_mysql_timestamps(self):
        response = self.client.post(
            "/api/bills",
            json={
                "idempotency_key": "d" * 32,
                "items": [{"id": self.product_id, "quantity": 1}],
                "received": 125.50,
                "payment_method": "UPI",
            },
            headers=self.post_headers(),
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        from datetime import date

        for path in (
            "/",
            "/reports",
            f"/sales?start={date.today().isoformat()}&end={date.today().isoformat()}",
            "/export/sales/csv",
            "/export/sales/xlsx",
            "/export/history.xlsx",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_analytics_filters_profit_snapshot_and_report_exports(self):
        with db() as connection:
            connection.execute(
                "UPDATE products SET purchase_price=? WHERE id=?",
                (40, self.product_id),
            )
        response = self.client.post(
            "/api/bills",
            json={
                "idempotency_key": "f" * 32,
                "items": [{"id": self.product_id, "quantity": 2}],
                "discount": 51,
                "received": 150,
                "payment_method": "UPI",
            },
            headers=self.post_headers(),
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        bill = response.get_json()

        with db() as connection:
            movement = connection.execute(
                "SELECT unit_purchase_price FROM stock_movements "
                "WHERE bill_id=? AND movement_type='sale'",
                (bill["bill_id"],),
            ).fetchone()
        self.assertEqual(float(movement["unit_purchase_price"]), 40.0)

        from datetime import date, timedelta

        today = date.today().isoformat()
        params = f"period=today&product_id={self.product_id}&category=Men%27s%20Shoes"
        dashboard = self.client.get(f"/reports?{params}")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(b"Sales analytics", dashboard.data)
        self.assertIn(b"Average bill value", dashboard.data)
        self.assertIn("₹200.00".encode(), dashboard.data)
        self.assertIn("₹120.00".encode(), dashboard.data)
        self.assertIn(b"complete purchase-cost snapshot", dashboard.data)

        yesterday = (date.today() - timedelta(days=1)).isoformat()
        no_sales = self.client.get(
            f"/reports?period=custom&start={yesterday}&end={yesterday}"
        )
        self.assertEqual(no_sales.status_code, 200)
        self.assertIn(b"No product sales in this date range.", no_sales.data)

        invalid_range = self.client.get(
            f"/reports?period=custom&start={today}&end={yesterday}"
        )
        self.assertEqual(invalid_range.status_code, 200)
        self.assertIn(b"start date must be on or before", invalid_range.data)

        excel = self.client.get(f"/reports/export/xlsx?{params}")
        self.assertEqual(excel.status_code, 200)
        self.assertEqual(
            excel.mimetype,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        from io import BytesIO
        from openpyxl import load_workbook

        workbook = load_workbook(BytesIO(excel.data), read_only=True, data_only=True)
        self.assertEqual(workbook.sheetnames, ["Summary", "Sales Details"])
        self.assertEqual(workbook["Summary"]["B4"].value, 200)
        details = list(workbook["Sales Details"].values)
        self.assertEqual(details[1][0], bill["bill_no"])
        self.assertEqual(details[1][7], 2)
        self.assertEqual(details[1][12], 200)
        workbook.close()

        pdf = self.client.get(f"/reports/export/pdf?{params}")
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf.mimetype, "application/pdf")
        self.assertTrue(pdf.data.startswith(b"%PDF-"))

    def test_analytics_and_exports_require_admin_session(self):
        client = app.test_client()
        for path in ("/reports", "/reports/export/xlsx", "/reports/export/pdf"):
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/login", response.location)

    def test_analytics_date_validation_and_category_index(self):
        response = self.client.get("/reports?period=unknown")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Choose today, yesterday", response.data)
        with db() as connection:
            index = connection.execute(
                "SELECT COUNT(DISTINCT INDEX_NAME) FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='bill_items' "
                "AND INDEX_NAME='ix_bill_items_category_bill'"
            ).fetchone()[0]
        self.assertEqual(index, 1)

    def test_analytics_does_not_estimate_profit_without_sale_cost(self):
        response = self.client.post(
            "/api/bills",
            json={
                "idempotency_key": "1" * 32,
                "items": [{"id": self.product_id, "quantity": 1}],
                "payment_method": "Cash",
            },
            headers=self.post_headers(),
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        dashboard = self.client.get("/reports?period=today")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(b"Profit analytics", dashboard.data)
        self.assertIn(b"Unavailable", dashboard.data)
        self.assertIn(b"Profit is not estimated from current product prices.", dashboard.data)
        self.assertNotIn(b'class="profit-value"', dashboard.data)

    def test_analytics_product_and_stock_tables_are_paginated(self):
        from datetime import datetime

        with db() as connection:
            for index in range(21):
                code = f"PAGE-{index:03d}"
                product = connection.execute(
                    """INSERT INTO products
                    (name,code,department,category,selling_price,stock,created_at)
                    VALUES(?,?,?,?,?,?,?)""",
                    (
                        f"Page Product {index:02d}", code, "Footwear", "Page Test",
                        20, 1, "2026-01-01",
                    ),
                )
                connection.execute(
                    """INSERT INTO stock_movements
                    (product_id,product_name,product_code,movement_type,quantity,
                     previous_stock,resulting_stock,note,created_at)
                    VALUES(?,?,?,'opening',?,0,1,'Pagination test',?)""",
                    (
                        product.lastrowid, f"Page Product {index:02d}", code, 1,
                        datetime.now().isoformat(timespec="seconds"),
                    ),
                )
        response = self.client.get(
            "/reports?period=this_year&page=2&movement_page=2"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Page 2 of 2", response.data)
        self.assertIn(b"Page Product", response.data)
        self.assertIn(b"Stock history pages", response.data)

    def test_csv_export_neutralizes_formula_text(self):
        with db() as connection:
            connection.execute("UPDATE products SET name=? WHERE id=?", ("=1+1", self.product_id))
        response = self.client.get("/export/products/csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"'=1+1", response.data)

    def test_product_edit_search_duplicate_and_delete_are_consistent(self):
        response = self.client.get("/products?q=TEST-001")
        self.assertIn(b"Test Shoe", response.data)

        response = self.client.post(
            "/products/save",
            data={
                "id": str(self.product_id),
                "name": "Edited Shoe",
                "code": "TEST-001",
                "department": "Footwear",
                "category": "Men's Shoes",
                "purchase_price": "70",
                "selling_price": "150.75",
                "stock": "5",
                "csrf_token": self.csrf_token,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(b"Edited Shoe", self.client.get("/products?q=Edited").data)
        with db() as connection:
            product = connection.execute(
                "SELECT name,selling_price,stock FROM products WHERE id=?",
                (self.product_id,),
            ).fetchone()
            movement = connection.execute(
                "SELECT movement_type,quantity FROM stock_movements "
                "WHERE product_id=? ORDER BY id DESC LIMIT 1",
                (self.product_id,),
            ).fetchone()
        self.assertEqual(product["name"], "Edited Shoe")
        self.assertEqual(float(product["selling_price"]), 150.75)
        self.assertEqual(product["stock"], 5)
        self.assertEqual((movement["movement_type"], movement["quantity"]), ("adjustment_in", 2))

        self.client.post(
            "/products/save",
            data={
                "name": "Duplicate Code",
                "code": "TEST-001",
                "department": "Footwear",
                "category": "Shoes",
                "selling_price": "99",
                "stock": "1",
                "csrf_token": self.csrf_token,
            },
        )
        with db() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM products WHERE code=?", ("TEST-001",)
                ).fetchone()[0],
                1,
            )

        response = self.client.post(
            f"/products/delete/{self.product_id}",
            data={"csrf_token": self.csrf_token},
        )
        self.assertEqual(response.status_code, 302)
        with db() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT id FROM products WHERE id=?", (self.product_id,)
                ).fetchone()
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM audit_log "
                    "WHERE entity_type='product' AND action='deleted' AND entity_id=?",
                    (str(self.product_id),),
                ).fetchone()[0],
                1,
            )

    def test_product_validation_rejects_negative_stock_zero_price_and_duplicate(self):
        for code, selling_price, stock in (
            ("TEST-NEGATIVE", "10", "-1"),
            ("TEST-ZERO", "0", "1"),
            ("TEST-DUPLICATE", "10", "1"),
        ):
            if code == "TEST-DUPLICATE":
                code = "TEST-001"
            with self.subTest(code=code, selling_price=selling_price, stock=stock):
                self.client.post(
                    "/products/save",
                    data={
                        "name": "Invalid Product",
                        "code": code,
                        "department": "Footwear",
                        "category": "Shoes",
                        "selling_price": selling_price,
                        "stock": stock,
                        "csrf_token": self.csrf_token,
                    },
                )
        with db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM products").fetchone()[0], 1)

    def test_stock_in_out_history_low_stock_alert_and_overdraw_protection(self):
        for movement_type, quantity in (("stock_in", "4"), ("stock_out", "6")):
            data = {
                "product_id": str(self.product_id),
                "movement_type": movement_type,
                "quantity": quantity,
                "note": "QA integration scenario",
                "csrf_token": self.csrf_token,
            }
            if movement_type == "stock_in":
                data.update({"purchase_price": "32.75", "supplier": "QA Supplier"})
            response = self.client.post(
                "/stock/movement",
                data=data,
            )
            self.assertEqual(response.status_code, 302)

        with db() as connection:
            stock = connection.execute(
                "SELECT stock FROM products WHERE id=?", (self.product_id,)
            ).fetchone()[0]
            movements = connection.execute(
                "SELECT movement_type,quantity,previous_stock,resulting_stock,supplier,"
                "unit_purchase_price FROM stock_movements "
                "WHERE product_id=? ORDER BY id",
                (self.product_id,),
            ).fetchall()
            purchase_price = connection.execute(
                "SELECT purchase_price FROM products WHERE id=?", (self.product_id,)
            ).fetchone()[0]
        self.assertEqual(stock, 1)
        self.assertEqual(float(purchase_price), 32.75)
        self.assertEqual(
            [(row["movement_type"], row["quantity"]) for row in movements],
            [("opening", 3), ("stock_in", 4), ("stock_out", 6)],
        )
        self.assertEqual(
            (movements[1]["previous_stock"], movements[1]["resulting_stock"],
             movements[1]["supplier"], float(movements[1]["unit_purchase_price"])),
            (3, 7, "QA Supplier", 32.75),
        )
        self.assertEqual(
            (movements[2]["previous_stock"], movements[2]["resulting_stock"]),
            (7, 1),
        )

        self.client.post(
            "/stock/movement",
            data={
                "product_id": str(self.product_id),
                "movement_type": "stock_out",
                "quantity": "2",
                "note": "Overdraw must fail",
                "csrf_token": self.csrf_token,
            },
        )
        with db() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT stock FROM products WHERE id=?", (self.product_id,)
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM stock_movements WHERE product_id=?",
                    (self.product_id,),
                ).fetchone()[0],
                3,
            )

        stock_page = self.client.get("/stock?q=TEST-001")
        dashboard = self.client.get("/")
        self.assertEqual(stock_page.status_code, 200)
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(b"Test Shoe", stock_page.data)
        self.assertIn(b"Test Shoe", dashboard.data)
        self.assertIn(b"Previous quantity", stock_page.data)
        self.assertIn(b"QA Supplier", stock_page.data)

    def test_multi_product_discount_payment_bill_number_and_receipt(self):
        with db() as connection:
            second = connection.execute(
                """INSERT INTO products
                (name,code,department,category,selling_price,stock,created_at)
                VALUES(?,?,?,?,?,?,?)""",
                ("Test Dress", "TEST-005", "Dress", "Kurtis", 49.25, 6, "2026-01-01"),
            )
            second_product_id = second.lastrowid

        response = self.client.post(
            "/api/bills",
            json={
                "idempotency_key": "e" * 32,
                "items": [
                    {"id": self.product_id, "quantity": 2},
                    {"id": second_product_id, "quantity": 1},
                ],
                "discount": 20.25,
                "received": 100,
                "payment_method": "UPI",
                "customer_name": "QA Customer",
                "customer_phone": "9000000001",
            },
            headers=self.post_headers(),
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        bill = response.get_json()
        self.assertRegex(bill["bill_no"], r"^HI5-\d{6}-\d{6}-[A-F0-9]{6}$")
        self.assertEqual(bill["receipt"]["subtotal"], 300.25)
        self.assertEqual(bill["receipt"]["discount"], 20.25)
        self.assertEqual(bill["receipt"]["total"], 280.0)
        self.assertEqual(bill["receipt"]["balance"], 180.0)

        with db() as connection:
            saved_bill = connection.execute(
                "SELECT subtotal,discount,total,received,balance,payment_status "
                "FROM bills WHERE id=?",
                (bill["bill_id"],),
            ).fetchone()
            stocks = connection.execute(
                "SELECT id,stock FROM products WHERE id IN (?,?) ORDER BY id",
                (self.product_id, second_product_id),
            ).fetchall()
            item_count = connection.execute(
                "SELECT COUNT(*) FROM bill_items WHERE bill_id=?", (bill["bill_id"],)
            ).fetchone()[0]
            movement_count = connection.execute(
                "SELECT COUNT(*) FROM stock_movements "
                "WHERE bill_id=? AND movement_type='sale'",
                (bill["bill_id"],),
            ).fetchone()[0]
            movement_stock = connection.execute(
                "SELECT previous_stock,resulting_stock FROM stock_movements "
                "WHERE bill_id=? AND product_id=?",
                (bill["bill_id"], self.product_id),
            ).fetchone()
            payment = connection.execute(
                "SELECT amount,method FROM payments WHERE bill_id=?",
                (bill["bill_id"],),
            ).fetchone()
        self.assertEqual(float(saved_bill["total"]), 280.0)
        self.assertEqual(saved_bill["payment_status"], "Partially Paid")
        self.assertEqual([row["stock"] for row in stocks], [1, 5])
        self.assertEqual((item_count, movement_count), (2, 2))
        self.assertEqual(
            (movement_stock["previous_stock"], movement_stock["resulting_stock"]),
            (3, 1),
        )
        self.assertEqual((float(payment["amount"]), payment["method"]), (100.0, "UPI"))

        receipt = self.client.get(f"/receipt/{bill['bill_id']}")
        self.assertEqual(receipt.status_code, 200)
        for text in (
            bill["bill_no"],
            "Test Shoe",
            "Test Dress",
            "Share receipt image",
            "கருப்பு கடை",
            "Ramanathapuram District, Tamil Nadu",
            "A4 invoice",
            "80mm thermal",
            "58mm thermal",
        ):
            self.assertIn(text.encode(), receipt.data)
        self.assertIn(b"receipt-sharing.js", receipt.data)

    def test_receipt_handles_long_special_names_and_missing_customer_phone(self):
        long_name = "Trail & Tide 'கருப்பு' " + ("W" * 210)
        with db() as connection:
            connection.execute(
                "UPDATE products SET name=? WHERE id=?",
                (long_name, self.product_id),
            )
            connection.execute(
                "UPDATE settings SET phone=?,whatsapp_phone=? WHERE id=1",
                ("918667579407", "918667579408"),
            )
        response = self.client.post(
            "/api/bills",
            json={
                "idempotency_key": "2" * 32,
                "items": [{"id": self.product_id, "quantity": 1}],
                "payment_method": "Cash",
            },
            headers=self.post_headers(),
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        bill = response.get_json()
        receipt = self.client.get(f"/receipt/{bill['bill_id']}")
        self.assertEqual(receipt.status_code, 200)
        self.assertIn(b"Customer WhatsApp number", receipt.data)
        self.assertIn(b"Trail &amp; Tide", receipt.data)
        self.assertIn("கருப்பு கடை".encode(), receipt.data)
        self.assertIn(b"WhatsApp: 918667579408", receipt.data)
        self.assertIn(b"Preview print", receipt.data)
        self.assertIn(b"print-format-a4", receipt.data)
        self.assertIn(b"receipt-preview-dialog", receipt.data)
        self.assertIn(b"whatsappReceiptUrl(", receipt.data)
        self.assertIn(b"receipt-sharing.js", receipt.data)
        self.assertIn(b"image/png", receipt.data)
        self.assertIn(b'id="receipt-recipient"', receipt.data)
        self.assertIn(b'value=""', receipt.data)
        self.assertNotIn(b"+91 8667579407", receipt.data)
        self.assertNotIn(b"@media print", receipt.data)
        with db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM bills").fetchone()[0], 1)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM stock_movements "
                    "WHERE bill_id=? AND movement_type='sale'",
                    (bill["bill_id"],),
                ).fetchone()[0],
                1,
            )

    def test_invalid_multi_item_bill_rolls_back_every_record(self):
        with db() as connection:
            second = connection.execute(
                """INSERT INTO products
                (name,code,department,category,selling_price,stock,created_at)
                VALUES(?,?,?,?,?,?,?)""",
                ("Test Dress", "TEST-006", "Dress", "Kurtis", 49.25, 6, "2026-01-01"),
            )
            second_product_id = second.lastrowid

        response = self.client.post(
            "/api/bills",
            json={
                "idempotency_key": "f" * 32,
                "items": [
                    {"id": self.product_id, "quantity": 1},
                    {"id": second_product_id, "quantity": 1},
                ],
                "discount": 200,
                "payment_method": "Cash",
                "customer_name": "Must Roll Back",
                "customer_phone": "9000000002",
            },
            headers=self.post_headers(),
        )
        self.assertEqual(response.status_code, 400)
        with db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM bills").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM bill_items").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM payments").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM customers").fetchone()[0], 0)
            self.assertEqual(
                connection.execute(
                    "SELECT SUM(stock) FROM products WHERE id IN (?,?)",
                    (self.product_id, second_product_id),
                ).fetchone()[0],
                9,
            )

    def test_dashboard_and_reports_show_persisted_sales_and_best_seller(self):
        response = self.client.post(
            "/api/bills",
            json={
                "idempotency_key": "1" * 32,
                "items": [{"id": self.product_id, "quantity": 1}],
                "payment_method": "Cash",
            },
            headers=self.post_headers(),
        )
        self.assertEqual(response.status_code, 200, response.get_json())

        dashboard = self.client.get("/")
        reports = self.client.get("/reports")
        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(reports.status_code, 200)
        self.assertIn(b"Today's Sales", dashboard.data)
        self.assertIn(b"125.50", dashboard.data)
        self.assertIn(b"Test Shoe", dashboard.data)
        self.assertIn(b"Total Stock Received</span><strong>3</strong>", dashboard.data)
        self.assertIn(b"Test Shoe", reports.data)
        with db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM bills").fetchone()[0], 1)
            self.assertEqual(
                connection.execute("SELECT stock FROM products").fetchone()[0], 2
            )


if __name__ == "__main__":
    unittest.main()
