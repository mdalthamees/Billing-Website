import re
from contextlib import contextmanager

from mysql.connector import pooling


MYSQL_TABLE_DDL = (
    """CREATE TABLE IF NOT EXISTS settings (
        id INT NOT NULL PRIMARY KEY,
        shop_name VARCHAR(255) NOT NULL,
        address TEXT NOT NULL,
        phone VARCHAR(32) NOT NULL DEFAULT '',
        logo VARCHAR(255) NOT NULL DEFAULT '',
        CONSTRAINT chk_settings_id CHECK (id = 1)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
    """CREATE TABLE IF NOT EXISTS customers (
        id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        name VARCHAR(120) NOT NULL,
        phone VARCHAR(32) NULL,
        created_at VARCHAR(32) NOT NULL,
        UNIQUE KEY uq_customers_phone (phone),
        KEY ix_customers_name (name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
    """CREATE TABLE IF NOT EXISTS products (
        id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        name VARCHAR(255) NOT NULL,
        code VARCHAR(191) NOT NULL,
        department VARCHAR(64) NOT NULL,
        category VARCHAR(191) NOT NULL,
        subcategory VARCHAR(191) NOT NULL DEFAULT '',
        gender VARCHAR(64) NOT NULL DEFAULT '',
        age_group VARCHAR(64) NOT NULL DEFAULT '',
        size VARCHAR(64) NOT NULL DEFAULT '',
        colour VARCHAR(96) NOT NULL DEFAULT '',
        brand VARCHAR(128) NOT NULL DEFAULT '',
        purchase_price DECIMAL(12,2) NOT NULL DEFAULT 0,
        selling_price DECIMAL(12,2) NOT NULL DEFAULT 0,
        stock INT NOT NULL DEFAULT 0,
        image VARCHAR(255) NOT NULL DEFAULT '',
        created_at VARCHAR(32) NOT NULL,
        UNIQUE KEY uq_products_code (code),
        KEY ix_products_department_category (department, category),
        KEY ix_products_stock (stock)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
    """CREATE TABLE IF NOT EXISTS bills (
        id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        bill_no VARCHAR(40) NOT NULL,
        customer_id BIGINT NULL,
        customer_name VARCHAR(120) NOT NULL DEFAULT '',
        customer_phone VARCHAR(32) NOT NULL DEFAULT '',
        subtotal DECIMAL(12,2) NOT NULL,
        discount DECIMAL(12,2) NOT NULL DEFAULT 0,
        total DECIMAL(12,2) NOT NULL,
        received DECIMAL(12,2) NOT NULL DEFAULT 0,
        balance DECIMAL(12,2) NOT NULL DEFAULT 0,
        payment_method VARCHAR(24) NOT NULL,
        payment_status VARCHAR(32) NOT NULL,
        created_at VARCHAR(32) NOT NULL,
        UNIQUE KEY uq_bills_bill_no (bill_no),
        KEY ix_bills_created_at (created_at),
        KEY ix_bills_customer_id (customer_id),
        CONSTRAINT fk_bills_customer FOREIGN KEY (customer_id)
            REFERENCES customers(id) ON DELETE SET NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
    """CREATE TABLE IF NOT EXISTS bill_items (
        id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        bill_id BIGINT NOT NULL,
        product_id BIGINT NULL,
        product_name VARCHAR(255) NOT NULL,
        code VARCHAR(191) NOT NULL DEFAULT '',
        department VARCHAR(64) NOT NULL DEFAULT '',
        category VARCHAR(191) NOT NULL DEFAULT '',
        size VARCHAR(64) NOT NULL DEFAULT '',
        colour VARCHAR(96) NOT NULL DEFAULT '',
        quantity INT NOT NULL,
        price DECIMAL(12,2) NOT NULL,
        line_total DECIMAL(12,2) NOT NULL,
        KEY ix_bill_items_bill_id (bill_id),
        KEY ix_bill_items_product_id (product_id),
        KEY ix_bill_items_department (department),
        CONSTRAINT fk_bill_items_bill FOREIGN KEY (bill_id)
            REFERENCES bills(id) ON DELETE CASCADE,
        CONSTRAINT fk_bill_items_product FOREIGN KEY (product_id)
            REFERENCES products(id) ON DELETE SET NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
    """CREATE TABLE IF NOT EXISTS payments (
        id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        bill_id BIGINT NOT NULL,
        amount DECIMAL(12,2) NOT NULL,
        method VARCHAR(24) NOT NULL,
        created_at VARCHAR(32) NOT NULL,
        KEY ix_payments_bill_id (bill_id),
        CONSTRAINT fk_payments_bill FOREIGN KEY (bill_id)
            REFERENCES bills(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
    """CREATE TABLE IF NOT EXISTS stock_preferences (
        id INT NOT NULL PRIMARY KEY,
        minimum_stock INT NOT NULL DEFAULT 5,
        CONSTRAINT chk_stock_preferences_id CHECK (id = 1)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
    """CREATE TABLE IF NOT EXISTS stock_movements (
        id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        product_id BIGINT NULL,
        product_name VARCHAR(255) NOT NULL,
        product_code VARCHAR(191) NOT NULL DEFAULT '',
        movement_type VARCHAR(32) NOT NULL,
        quantity INT NOT NULL,
        note VARCHAR(255) NOT NULL DEFAULT '',
        bill_id BIGINT NULL,
        created_at VARCHAR(32) NOT NULL,
        KEY ix_stock_movements_product_id (product_id),
        KEY ix_stock_movements_bill_id (bill_id),
        KEY ix_stock_movements_created_at (created_at),
        CONSTRAINT chk_stock_movements_quantity CHECK (quantity > 0),
        CONSTRAINT fk_stock_movements_product FOREIGN KEY (product_id)
            REFERENCES products(id) ON DELETE SET NULL,
        CONSTRAINT fk_stock_movements_bill FOREIGN KEY (bill_id)
            REFERENCES bills(id) ON DELETE SET NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
    """CREATE TABLE IF NOT EXISTS inventory_migrations (
        migration_key VARCHAR(128) NOT NULL PRIMARY KEY,
        completed_at VARCHAR(32) NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
    """CREATE TABLE IF NOT EXISTS audit_log (
        id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        created_at VARCHAR(32) NOT NULL,
        action VARCHAR(64) NOT NULL,
        entity_type VARCHAR(64) NOT NULL,
        entity_id VARCHAR(128) NOT NULL DEFAULT '',
        reference VARCHAR(255) NOT NULL DEFAULT '',
        details_json LONGTEXT NOT NULL,
        KEY ix_audit_log_created_at (created_at),
        KEY ix_audit_log_entity (entity_type, entity_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
    """CREATE TABLE IF NOT EXISTS bill_requests (
        idempotency_key CHAR(32) NOT NULL PRIMARY KEY,
        bill_id BIGINT NOT NULL,
        response_json LONGTEXT NOT NULL,
        created_at VARCHAR(32) NOT NULL,
        UNIQUE KEY uq_bill_requests_bill_id (bill_id),
        CONSTRAINT fk_bill_requests_bill FOREIGN KEY (bill_id)
            REFERENCES bills(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
    """CREATE TABLE IF NOT EXISTS hi5_migration_history (
        migration_id VARCHAR(64) NOT NULL PRIMARY KEY,
        source_fingerprint CHAR(64) NOT NULL,
        row_counts_json LONGTEXT NOT NULL,
        completed_at VARCHAR(32) NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
)

TABLE_ORDER = (
    "settings",
    "customers",
    "products",
    "bills",
    "bill_items",
    "payments",
    "stock_preferences",
    "stock_movements",
    "inventory_migrations",
    "audit_log",
    "bill_requests",
)


class DictRow(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


def _mysql_sql(statement):
    sql = statement.strip()
    sql = re.sub(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT IGNORE INTO", sql, flags=re.I)
    sql = re.sub(r"\bSELECT\s+last_insert_rowid\s*\(\s*\)", "SELECT LAST_INSERT_ID() AS id", sql, flags=re.I)
    sql = re.sub(r"\bdate\s*\(\s*created_at\s*\)", "DATE(created_at)", sql, flags=re.I)
    sql = re.sub(
        r"date\s*\(\s*'now'\s*,\s*'-6 days'\s*\)",
        "DATE_SUB(CURDATE(), INTERVAL 6 DAY)",
        sql,
        flags=re.I,
    )
    sql = re.sub(r"\bstrftime\s*\(\s*'%Y-%m'\s*,\s*created_at\s*\)", "DATE_FORMAT(created_at, '%Y-%m')", sql, flags=re.I)
    sql = re.sub(r"\bproduct_id\s+IS\s+\?", "product_id <=> ?", sql, flags=re.I)
    if re.match(r"^BEGIN\s+IMMEDIATE$", sql, flags=re.I):
        sql = "START TRANSACTION"
    return sql.replace("?", "%s")


class MySQLCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, statement, params=None):
        self._cursor.execute(_mysql_sql(statement), params or ())
        return self

    def executemany(self, statement, rows):
        self._cursor.executemany(_mysql_sql(statement), rows)
        return self

    def fetchone(self):
        row = self._cursor.fetchone()
        return DictRow(row) if row is not None else None

    def fetchall(self):
        return [DictRow(row) for row in self._cursor.fetchall()]

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    @property
    def description(self):
        return self._cursor.description

    def __iter__(self):
        for row in self._cursor:
            yield DictRow(row)

    def close(self):
        self._cursor.close()


_pool = None
_pool_config = None


def configure_mysql(app_config):
    global _pool_config
    _pool_config = {
        key: app_config[key]
        for key in (
            "MYSQL_POOL_SIZE",
            "DB_HOST",
            "DB_PORT",
            "DB_NAME",
            "DB_USER",
            "DB_PASSWORD",
            "DB_CONNECT_TIMEOUT",
        )
    }


def mysql_pool():
    global _pool
    if _pool is None:
        if _pool_config is None:
            raise RuntimeError("MySQL database settings have not been configured.")
        _pool = pooling.MySQLConnectionPool(
            pool_name="hi5_pos",
            pool_size=int(_pool_config["MYSQL_POOL_SIZE"]),
            pool_reset_session=True,
            host=_pool_config["DB_HOST"],
            port=int(_pool_config["DB_PORT"]),
            database=_pool_config["DB_NAME"],
            user=_pool_config["DB_USER"],
            password=_pool_config["DB_PASSWORD"],
            charset="utf8mb4",
            use_unicode=True,
            autocommit=False,
            connection_timeout=int(_pool_config["DB_CONNECT_TIMEOUT"]),
        )
    return _pool


@contextmanager
def mysql_cursor():
    from flask import g, has_app_context

    owned_connection = not has_app_context()
    if owned_connection:
        connection = mysql_pool().get_connection()
    else:
        connection = getattr(g, "_hi5_mysql_connection", None)
        if connection is None or not connection.is_connected():
            connection = mysql_pool().get_connection()
            g._hi5_mysql_connection = connection

    cursor = connection.cursor(dictionary=True, buffered=True)
    try:
        yield MySQLCursor(cursor)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        if owned_connection:
            connection.close()


def close_request_connection(_error=None):
    from flask import g

    connection = g.pop("_hi5_mysql_connection", None)
    if connection is not None:
        connection.close()


def initialize_mysql_schema(connection):
    cursor = connection.cursor()
    try:
        for statement in MYSQL_TABLE_DDL:
            cursor.execute(statement)
        ensure_mysql_constraints(cursor)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()


def ensure_mysql_constraints(cursor):
    constraints = (
        ("settings", "chk_settings_id", "CHECK (id = 1)"),
        ("stock_preferences", "chk_stock_preferences_id", "CHECK (id = 1)"),
        (
            "stock_movements",
            "chk_stock_movements_quantity",
            "CHECK (quantity > 0)",
        ),
    )
    for table, name, definition in constraints:
        cursor.execute(
            "SELECT 1 FROM information_schema.TABLE_CONSTRAINTS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND CONSTRAINT_NAME=%s",
            (table, name),
        )
        if cursor.fetchone() is None:
            cursor.execute(
                f"ALTER TABLE `{table}` ADD CONSTRAINT `{name}` {definition}"
            )
