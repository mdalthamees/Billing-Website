import os
import unittest

import mysql.connector
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
            response = self.client.post(
                "/stock/movement",
                data={
                    "product_id": str(self.product_id),
                    "movement_type": movement_type,
                    "quantity": quantity,
                    "note": "QA integration scenario",
                    "csrf_token": self.csrf_token,
                },
            )
            self.assertEqual(response.status_code, 302)

        with db() as connection:
            stock = connection.execute(
                "SELECT stock FROM products WHERE id=?", (self.product_id,)
            ).fetchone()[0]
            movements = connection.execute(
                "SELECT movement_type,quantity FROM stock_movements "
                "WHERE product_id=? ORDER BY id",
                (self.product_id,),
            ).fetchall()
        self.assertEqual(stock, 1)
        self.assertEqual(
            [(row["movement_type"], row["quantity"]) for row in movements],
            [("opening", 3), ("stock_in", 4), ("stock_out", 6)],
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
            payment = connection.execute(
                "SELECT amount,method FROM payments WHERE bill_id=?",
                (bill["bill_id"],),
            ).fetchone()
        self.assertEqual(float(saved_bill["total"]), 280.0)
        self.assertEqual(saved_bill["payment_status"], "Partially Paid")
        self.assertEqual([row["stock"] for row in stocks], [1, 5])
        self.assertEqual((item_count, movement_count), (2, 2))
        self.assertEqual((float(payment["amount"]), payment["method"]), (100.0, "UPI"))

        receipt = self.client.get(f"/receipt/{bill['bill_id']}")
        self.assertEqual(receipt.status_code, 200)
        for text in (bill["bill_no"], "Test Shoe", "Test Dress", "Share Receipt Image"):
            self.assertIn(text.encode(), receipt.data)
        self.assertIn(b"receipt-sharing.js", receipt.data)

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
        self.assertIn(b"Total Stock Quantity</span><strong>3</strong>", dashboard.data)
        self.assertIn(b"Test Shoe", reports.data)
        with db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM bills").fetchone()[0], 1)
            self.assertEqual(
                connection.execute("SELECT stock FROM products").fetchone()[0], 2
            )


if __name__ == "__main__":
    unittest.main()
