import os, csv, io, logging, secrets, hmac
import json
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal, InvalidOperation
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_file, abort, session
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash
from dotenv import load_dotenv
import mysql.connector
from mysql.connector import IntegrityError
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.cell import WriteOnlyCell
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle
from database import close_request_connection, configure_mysql, mysql_cursor, mysql_pool, initialize_mysql_schema
from backup_manager import BackupError, backup_path, create_backup, list_backup_history, record_backup_failure, restore_backup, validate_backup

BASE = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(BASE, ".env"))
UPLOADS = os.path.join(BASE, "static", "uploads")
os.makedirs(UPLOADS, exist_ok=True)
configured_backup_directory = os.environ.get("BACKUP_DIRECTORY", "").strip()
BACKUP_DIRECTORY = os.path.abspath(
    configured_backup_directory
    if os.path.isabs(configured_backup_directory)
    else os.path.join(BASE, configured_backup_directory or "backups")
)

app = Flask(__name__)
APP_ENV = (os.environ.get("APP_ENV") or os.environ.get("FLASK_ENV") or "").strip().lower()
if APP_ENV not in {"development", "production", "test", "testing"}:
    raise RuntimeError("Set APP_ENV explicitly to development, testing, or production.")
app.secret_key = os.environ.get("SECRET_KEY") or os.environ.get("HI5_SECRET_KEY")
if not app.secret_key:
    if APP_ENV == "production":
        raise RuntimeError("SECRET_KEY must be configured in production.")
    app.secret_key = secrets.token_urlsafe(32)
    logging.warning("No SECRET_KEY configured; using a temporary local development key.")
if APP_ENV == "production" and len(app.secret_key) < 32:
    raise RuntimeError("SECRET_KEY must be at least 32 characters in production.")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=APP_ENV == "production",
    SESSION_REFRESH_EACH_REQUEST=False,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    BACKUP_DIRECTORY=BACKUP_DIRECTORY,
    DB_HOST=os.environ.get("DB_HOST", "localhost"),
    DB_PORT=int(os.environ.get("DB_PORT", "3306")),
    DB_NAME=os.environ.get("DB_NAME", "hi5_billing"),
    DB_USER=os.environ.get("DB_USER", "root"),
    DB_PASSWORD=os.environ.get("DB_PASSWORD", ""),
    DB_SSL_CA=os.environ.get("DB_SSL_CA", ""),
    MYSQL_POOL_SIZE=int(os.environ.get("MYSQL_POOL_SIZE", "5")),
    DB_CONNECT_TIMEOUT=int(os.environ.get("DB_CONNECT_TIMEOUT", "5")),
)
configure_mysql(app.config)
app.teardown_appcontext(close_request_connection)
ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH", "")
if APP_ENV == "production" and not ADMIN_PASSWORD_HASH:
    raise RuntimeError("ADMIN_PASSWORD_HASH must be configured in production.")
if APP_ENV == "production" and (
    not os.environ.get("DB_USER")
    or os.environ.get("DB_USER", "").lower() in {"root", "admin"}
    or not os.environ.get("DB_PASSWORD")
    or not os.environ.get("DB_NAME")
):
    raise RuntimeError("Production requires a dedicated MySQL account, password, and database name.")
if ADMIN_PASSWORD_HASH:
    try:
        check_password_hash(ADMIN_PASSWORD_HASH,"")
    except ValueError as error:
        raise RuntimeError("ADMIN_PASSWORD_HASH is not a valid Werkzeug password hash.") from error
ALLOWED = {"png", "jpg", "jpeg", "webp"}
LEGACY_SHOP_ADDRESS = "Gandhi Street, R.S. Mangalam, Ramanathapuram, Tamil Nadu"
RECEIPT_SHOP_ADDRESS = "Gandhi Street, R.S. Mangalam, Ramanathapuram District, Tamil Nadu"

def db():
    return mysql_cursor()

def format_receipt_address(address):
    if address == LEGACY_SHOP_ADDRESS:
        return RECEIPT_SHOP_ADDRESS
    return address

def csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


LOGIN_FAILURE_LIMIT = 5
LOGIN_WINDOW = timedelta(minutes=15)
LOGIN_LOCKOUT = timedelta(minutes=15)


def _login_identifier():
    address = request.remote_addr or "unknown"
    return hmac.new(app.secret_key.encode(), address.encode(), "sha256").hexdigest()


def _login_lockout_remaining(identifier, now):
    with db() as connection:
        connection.execute(
            """INSERT IGNORE INTO admin_login_attempts
               (identifier,failed_attempts,window_started_at,locked_until,updated_at)
               VALUES(?,0,?,NULL,?)""",
            (identifier, now, now),
        )
        row = connection.execute(
            "SELECT failed_attempts,window_started_at,locked_until "
            "FROM admin_login_attempts WHERE identifier=? FOR UPDATE",
            (identifier,),
        ).fetchone()
        locked_until = row["locked_until"]
        if locked_until and locked_until > now:
            return int((locked_until - now).total_seconds()) + 1
        if now - row["window_started_at"] >= LOGIN_WINDOW:
            connection.execute(
                """UPDATE admin_login_attempts SET failed_attempts=0,
                   window_started_at=?,locked_until=NULL,updated_at=? WHERE identifier=?""",
                (now, now, identifier),
            )
    return 0


def _record_login_failure(identifier, now):
    with db() as connection:
        connection.execute(
            """INSERT IGNORE INTO admin_login_attempts
               (identifier,failed_attempts,window_started_at,locked_until,updated_at)
               VALUES(?,0,?,NULL,?)""",
            (identifier, now, now),
        )
        row = connection.execute(
            "SELECT failed_attempts,window_started_at FROM admin_login_attempts "
            "WHERE identifier=? FOR UPDATE",
            (identifier,),
        ).fetchone()
        failures = row["failed_attempts"] + 1
        window_start = row["window_started_at"]
        if now - window_start >= LOGIN_WINDOW:
            failures = 1
            window_start = now
        locked_until = now + LOGIN_LOCKOUT if failures >= LOGIN_FAILURE_LIMIT else None
        connection.execute(
            """UPDATE admin_login_attempts SET failed_attempts=?,window_started_at=?,
               locked_until=?,updated_at=? WHERE identifier=?""",
            (failures, window_start, locked_until, now, identifier),
        )
    return int(LOGIN_LOCKOUT.total_seconds()) if locked_until else 0

@app.before_request
def protect_state_changes():
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return
    expected = session.get("_csrf_token", "")
    supplied = request.form.get("csrf_token", "") or request.headers.get("X-CSRF-Token", "")
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        abort(400, description="The request could not be verified. Reload the page and try again.")

@app.before_request
def require_admin():
    if request.endpoint in {"login", "static"}:
        return
    if not session.get("admin_authenticated"):
        if request.path.startswith("/api/"):
            return jsonify(error="Administrator login is required."),401
        return redirect(url_for("login"))

@app.context_processor
def inject_csrf_token():
    return {"csrf_token": csrf_token}

@app.errorhandler(400)
def handle_bad_request(error):
    if request.path.startswith("/api/"):
        return jsonify(error=error.description),400
    return error

@app.errorhandler(500)
def handle_internal_error(error):
    original=getattr(error,"original_exception",None)
    if original:
        app.logger.error("Unhandled request error.",
            exc_info=(type(original),original,original.__traceback__))
    else:
        app.logger.error("Unhandled request error: %s",error)
    if request.path.startswith("/api/"):
        return jsonify(error="An internal server error occurred."),500
    return "An internal server error occurred.",500

def parse_amount(value, field_name, default="0"):
    try:
        amount = Decimal(str(value if value not in (None, "") else default))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"{field_name} must be a valid amount.")
    if not amount.is_finite() or amount < 0 or amount > Decimal("999999999.99"):
        raise ValueError(f"{field_name} must be a non-negative finite amount.")
    try:
        return amount.quantize(Decimal("0.01"))
    except InvalidOperation as error:
        raise ValueError(f"{field_name} is outside the supported range.") from error


def normalize_phone(value, field_name):
    value = value.strip()
    if not value:
        return ""
    if any(character not in "0123456789+(). -" for character in value):
        raise ValueError(f"{field_name} may contain only digits and phone formatting characters.")
    digits = "".join(character for character in value if character.isdigit())
    if not 7 <= len(digits) <= 15:
        raise ValueError(f"{field_name} must contain 7 to 15 digits, including its country code.")
    return digits

@app.route("/login", methods=["GET","POST"])
def login():
    if session.get("admin_authenticated"):
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        identifier = _login_identifier()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            remaining = _login_lockout_remaining(identifier, now)
        except mysql.connector.Error:
            app.logger.error("Could not check the administrator login lockout (MySQL error).")
            return render_template("login.html", login_error="Sign-in is temporarily unavailable."), 503
        if remaining:
            flash("Too many failed attempts. Try again in 15 minutes.", "danger")
            return render_template("login.html"), 429
        password = request.form.get("password", "")
        if not ADMIN_PASSWORD_HASH:
            app.logger.error("Admin login is unavailable because ADMIN_PASSWORD_HASH is not configured.")
            flash("Admin login is not configured. Set ADMIN_PASSWORD_HASH in .env and restart the app.","danger")
        elif check_password_hash(ADMIN_PASSWORD_HASH,password):
            try:
                with db() as connection:
                    connection.execute("DELETE FROM admin_login_attempts WHERE identifier=?", (identifier,))
            except mysql.connector.Error:
                app.logger.error("Could not clear the administrator login lockout (MySQL error).")
                return render_template("login.html", login_error="Sign-in is temporarily unavailable."), 503
            session.clear()
            session["admin_authenticated"]=True
            session.permanent = True
            return redirect(url_for("dashboard"))
        else:
            try:
                remaining = _record_login_failure(identifier, now)
            except mysql.connector.Error:
                app.logger.error("Could not record the administrator login failure (MySQL error).")
                return render_template("login.html", login_error="Sign-in is temporarily unavailable."), 503
            if remaining:
                flash("Too many failed attempts. Try again in 15 minutes.", "danger")
                return render_template("login.html"), 429
            flash("Invalid administrator password.","danger")
    return render_template("login.html")

@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))

def init_db():
    connection = mysql_pool().get_connection()
    try:
        initialize_mysql_schema(connection)
    except mysql.connector.Error:
        logging.exception("Could not initialize the MySQL schema.")
        raise
    finally:
        connection.close()

    with db() as c:
        c.execute("""INSERT OR IGNORE INTO settings(id,shop_name,address,phone,logo)
        VALUES(1,'HI-5 FOOTWEAR & BOUTIQUES','Gandhi Street, R.S. Mangalam, Ramanathapuram, Tamil Nadu','','')""")
        c.execute("INSERT OR IGNORE INTO stock_preferences(id,minimum_stock) VALUES(1,5)")
        imported = c.execute(
            "SELECT 1 FROM inventory_migrations WHERE migration_key='opening-stock-v1'"
        ).fetchone()
        if not imported:
            products = c.execute("""SELECT p.id,p.name,p.code,p.stock,p.created_at,
                COALESCE(SUM(bi.quantity),0) sold
                FROM products p LEFT JOIN bill_items bi ON bi.product_id=p.id
                GROUP BY p.id""").fetchall()
            for product in products:
                opening_stock = product["stock"] + product["sold"]
                if opening_stock > 0:
                    c.execute("""INSERT INTO stock_movements
                        (product_id,product_name,product_code,movement_type,quantity,note,created_at)
                        VALUES(?,?,?,'opening',?,?,?)""",
                        (product["id"],product["name"],product["code"],opening_stock,
                         "Opening balance from existing inventory and sales",
                         product["created_at"]))
            c.execute("""INSERT INTO inventory_migrations(migration_key,completed_at)
                VALUES('opening-stock-v1',?)""",(datetime.now().isoformat(timespec="seconds"),))
        imported_sales = c.execute(
            "SELECT 1 FROM inventory_migrations WHERE migration_key='bill-sales-v2'"
        ).fetchone()
        if not imported_sales:
            sales = c.execute("""SELECT bi.product_id,bi.product_name,bi.code,bi.bill_id,
                b.bill_no,b.created_at,SUM(bi.quantity) quantity
                FROM bill_items bi JOIN bills b ON b.id=bi.bill_id
                GROUP BY bi.product_id,bi.product_name,bi.code,bi.bill_id,b.bill_no,b.created_at""").fetchall()
            for sale in sales:
                recorded = c.execute("""SELECT COALESCE(SUM(quantity),0)
                    FROM stock_movements
                    WHERE movement_type='sale' AND bill_id=? AND product_id IS ?
                    AND product_name=? AND product_code=?""",
                    (sale["bill_id"],sale["product_id"],sale["product_name"],sale["code"] or "")).fetchone()[0]
                missing_quantity = sale["quantity"]-recorded
                if missing_quantity > 0:
                    c.execute("""INSERT INTO stock_movements
                        (product_id,product_name,product_code,movement_type,quantity,note,bill_id,created_at)
                        VALUES(?,?,?,'sale',?,?,?,?)""",
                        (sale["product_id"],sale["product_name"],sale["code"] or "",
                         missing_quantity,f"Sale {sale['bill_no']} (historical)",
                         sale["bill_id"],sale["created_at"]))
            c.execute("""INSERT INTO inventory_migrations(migration_key,completed_at)
                VALUES('bill-sales-v2',?)""",(datetime.now().isoformat(timespec="seconds"),))

def audit(c, action, entity_type, entity_id="", reference="", details=None):
    cur = c.execute("""INSERT INTO audit_log
        (created_at,action,entity_type,entity_id,reference,details_json)
        VALUES(?,?,?,?,?,?)""",
        (datetime.now().isoformat(timespec="seconds"),action,entity_type,
         str(entity_id or ""),reference,
         json.dumps(details or {},ensure_ascii=False,default=str)))
    return cur.lastrowid

def settings():
    with db() as c:
        return dict(c.execute("SELECT * FROM settings WHERE id=1").fetchone())

@app.context_processor
def inject_globals():
    return {"shop": settings(), "today": date.today().isoformat()}

def upload_image(file, prefix):
    if not file or not file.filename:
        return ""
    ext = file.filename.rsplit(".",1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED:
        raise ValueError("Only PNG, JPG, JPEG and WEBP images are allowed.")
    name = secure_filename(f"{prefix}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}.{ext}")
    file.save(os.path.join(UPLOADS, name))
    return name

@app.route("/")
def dashboard():
    today_s = date.today().isoformat()
    month_s = date.today().replace(day=1).isoformat()
    with db() as c:
        today = c.execute("SELECT COALESCE(SUM(total),0) sales,COUNT(*) bills FROM bills WHERE date(created_at)=?",(today_s,)).fetchone()
        month = c.execute("SELECT COALESCE(SUM(total),0) FROM bills WHERE date(created_at)>=?",(month_s,)).fetchone()[0]
        products = c.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        customers = c.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
        stock_value = c.execute("SELECT COALESCE(SUM(stock*purchase_price),0) FROM products").fetchone()[0]
        pending = c.execute("SELECT COALESCE(SUM(balance),0) FROM bills").fetchone()[0]
        minimum_stock = c.execute("SELECT minimum_stock FROM stock_preferences WHERE id=1").fetchone()[0]
        low = c.execute("SELECT * FROM products WHERE stock>0 AND stock<=? ORDER BY stock ASC LIMIT 8",(minimum_stock,)).fetchall()
        stock_stats = c.execute("""SELECT
            COUNT(*) total_products,
            COALESCE(SUM(stock),0) available_stock,
            COALESCE(SUM(CASE WHEN stock>0 AND stock<=? THEN 1 ELSE 0 END),0) low_stock_products,
            COALESCE(SUM(CASE WHEN stock=0 THEN 1 ELSE 0 END),0) out_of_stock_products,
            COALESCE((SELECT SUM(quantity) FROM bill_items),0) total_products_sold,
            COALESCE((SELECT SUM(quantity) FROM stock_movements
                WHERE movement_type IN ('opening','stock_in')),0) total_stock_received
            FROM products""",(minimum_stock,)).fetchone()
        chart = c.execute("""SELECT date(created_at) d, SUM(total) total FROM bills
            WHERE date(created_at)>=date('now','-6 days') GROUP BY date(created_at) ORDER BY d""").fetchall()
        dept = c.execute("SELECT department,SUM(line_total) total FROM bill_items GROUP BY department").fetchall()
        recent = c.execute("SELECT * FROM bills ORDER BY id DESC LIMIT 6").fetchall()
    return render_template("dashboard.html", today=today, month=month, products=products,
        customers=customers, stock_value=stock_value, pending=pending, low=low,
        stock_stats=stock_stats, minimum_stock=minimum_stock,
        chart=[dict(x) for x in chart], dept=[dict(x) for x in dept], recent=recent)


@app.route("/admin/backups")
def backup_management():
    history = list_backup_history(app.config["BACKUP_DIRECTORY"])
    return render_template("backups.html", backups=history)


@app.route("/admin/backups/create", methods=["POST"])
def create_database_backup():
    connection = None
    try:
        connection = mysql_pool().get_connection()
        record = create_backup(
            connection,
            app.config["DB_NAME"],
            app.config["BACKUP_DIRECTORY"],
        )
    except (BackupError, mysql.connector.Error, OSError, RuntimeError, ValueError, EOFError) as error:
        app.logger.error("Database backup failed (%s).", type(error).__name__)
        if connection is None:
            try:
                record_backup_failure(app.config["BACKUP_DIRECTORY"], app.config["DB_NAME"], error)
            except OSError:
                app.logger.error("Could not write the backup failure history (filesystem error).")
        flash("Database backup failed. Check the application log and backup configuration.", "danger")
    else:
        try:
            with db() as audit_connection:
                audit(audit_connection, "backup_created", "database_backup", reference=record["filename"])
        except mysql.connector.Error:
            app.logger.error("Backup succeeded, but its audit entry could not be written (MySQL error).")
            flash(
                f"Backup created: {record['filename']}, but the audit entry could not be saved.",
                "warning",
            )
        else:
            flash(
                f"Backup created: {record['filename']} "
                f"({record['size_bytes']:,} bytes, {record['row_count']:,} rows).",
                "success",
            )
    finally:
        if connection is not None:
            connection.close()
    return redirect(url_for("backup_management"))


@app.route("/admin/backups/<filename>/download")
def download_database_backup(filename):
    history = list_backup_history(app.config["BACKUP_DIRECTORY"])
    if not any(record["filename"] == filename and record["status"] == "success" for record in history):
        abort(404)
    try:
        path = backup_path(app.config["BACKUP_DIRECTORY"], filename)
    except BackupError:
        abort(404)
    response = send_file(path, as_attachment=True, download_name=filename, mimetype="application/gzip")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/admin/backups/restore", methods=["POST"])
def restore_database_backup():
    if request.form.get("confirm_restore") != "RESTORE":
        flash("Type RESTORE in the confirmation field to continue.", "danger")
        return redirect(url_for("backup_management"))
    filename = request.form.get("backup_filename", "")
    connection = None
    try:
        connection = mysql_pool().get_connection()
        validate_backup(connection, app.config["DB_NAME"], app.config["BACKUP_DIRECTORY"], filename)
        connection.close()
        connection = None
        safety_connection = mysql_pool().get_connection()
        try:
            safety = create_backup(
                safety_connection,
                app.config["DB_NAME"],
                app.config["BACKUP_DIRECTORY"],
            )
        finally:
            safety_connection.close()
        connection = mysql_pool().get_connection()
        record = restore_backup(
            connection,
            app.config["DB_NAME"],
            app.config["BACKUP_DIRECTORY"],
            filename,
        )
    except (BackupError, mysql.connector.Error, OSError, RuntimeError, ValueError, EOFError) as error:
        app.logger.error("Database restore failed (%s).", type(error).__name__)
        flash("Database restore failed. The existing database was not intentionally replaced.", "danger")
    else:
        try:
            with db() as audit_connection:
                audit(
                    audit_connection,
                    "backup_restored",
                    "database_backup",
                    reference=filename,
                    details={"safety_backup": safety["filename"]},
                )
        except mysql.connector.Error:
            app.logger.error("Restore succeeded, but its audit entry could not be written (MySQL error).")
            flash(
                f"Restore completed from {record['filename']}, but the audit entry could not be saved. "
                f"Safety backup: {safety['filename']}.",
                "warning",
            )
        else:
            flash(
                f"Restore completed from {record['filename']}. "
                f"Safety backup: {safety['filename']}.",
                "success",
            )
    finally:
        if connection is not None:
            connection.close()
    return redirect(url_for("backup_management"))


@app.route("/stock")
def stock_maintenance():
    department = request.args.get("department","")
    q = request.args.get("q","").strip()
    with db() as c:
        minimum_stock = c.execute("SELECT minimum_stock FROM stock_preferences WHERE id=1").fetchone()[0]
        stock_stats = c.execute("""SELECT
            COUNT(*) total_products,
            COALESCE(SUM(stock),0) available_stock,
            COALESCE(SUM(CASE WHEN stock>0 AND stock<=? THEN 1 ELSE 0 END),0) low_stock_products,
            COALESCE(SUM(CASE WHEN stock=0 THEN 1 ELSE 0 END),0) out_of_stock_products,
            COALESCE((SELECT SUM(quantity) FROM bill_items),0) total_products_sold,
            COALESCE((SELECT SUM(quantity) FROM stock_movements
                WHERE movement_type IN ('opening','stock_in')),0) total_stock_received
            FROM products""",(minimum_stock,)).fetchone()
        sql = """SELECT p.*,
            COALESCE((SELECT SUM(quantity) FROM stock_movements sm
                WHERE sm.product_id=p.id
                AND sm.movement_type IN ('opening','stock_in','adjustment_in')),0) total_stock_added,
            COALESCE((SELECT SUM(quantity) FROM bill_items bi
                WHERE bi.product_id=p.id),0) quantity_sold
            FROM products p WHERE 1=1"""
        args = []
        if department:
            sql += " AND p.department=?"
            args.append(department)
        if q:
            sql += """ AND (p.name LIKE ? OR p.code LIKE ? OR p.category LIKE ?
                OR p.subcategory LIKE ? OR p.size LIKE ?)"""
            args.extend([f"%{q}%"]*5)
        products = c.execute(sql+" ORDER BY p.department,p.name,p.size,p.colour",args).fetchall()
        stock_options = c.execute("SELECT id,name,code,stock FROM products ORDER BY name,size,colour").fetchall()
        movements = c.execute("""SELECT sm.*,COALESCE(p.name,sm.product_name) display_name,
            COALESCE(p.code,sm.product_code) display_code
            FROM stock_movements sm LEFT JOIN products p ON p.id=sm.product_id
            ORDER BY sm.id DESC LIMIT 100""").fetchall()
    return render_template("stock.html", products=products, movements=movements,
        stock_options=stock_options, stock_stats=stock_stats, minimum_stock=minimum_stock,
        department=department, q=q)

@app.route("/stock/movement", methods=["POST"])
def stock_movement():
    product_id = request.form.get("product_id","")
    movement_type = request.form.get("movement_type","")
    note = request.form.get("note","").strip()
    supplier = request.form.get("supplier","").strip()
    purchase_price_raw = request.form.get("purchase_price","").strip()
    try:
        if not product_id.isdecimal():
            raise ValueError("Select a valid product.")
        quantity = int(request.form.get("quantity","0"))
        if quantity < 1 or quantity>2147483647:
            raise ValueError("Quantity must be a positive whole number.")
        if movement_type not in {"stock_in","stock_out"}:
            raise ValueError("Choose Stock In or Stock Out.")
        if not note:
            raise ValueError("Enter a reason for this stock movement.")
        if len(note)>160:
            raise ValueError("The stock movement reason must be 160 characters or fewer.")
        if movement_type == "stock_in" and len(supplier)>160:
            raise ValueError("Supplier details must be 160 characters or fewer.")
        purchase_price = (
            parse_amount(purchase_price_raw, "Purchase price")
            if movement_type == "stock_in" and purchase_price_raw else None
        )
        with db() as c:
            product = c.execute("SELECT * FROM products WHERE id=? FOR UPDATE",
                (product_id,)).fetchone()
            if not product:
                raise ValueError("Select a valid product.")
            previous_stock = product["stock"]
            if movement_type == "stock_out":
                cur = c.execute("UPDATE products SET stock=stock-? WHERE id=? AND stock>=?",
                    (quantity,product_id,quantity))
                if cur.rowcount != 1:
                    raise ValueError(f"Only {product['stock']} units are available for stock out.")
            else:
                cur = c.execute(
                    "UPDATE products SET stock=stock+? WHERE id=? AND stock<=?",
                    (quantity,product_id,2147483647-quantity))
                if cur.rowcount != 1:
                    raise ValueError("The received quantity would exceed the supported stock limit.")
                if purchase_price is not None:
                    c.execute("UPDATE products SET purchase_price=? WHERE id=?",
                        (purchase_price,product_id))
            resulting_stock = previous_stock + quantity if movement_type == "stock_in" else previous_stock - quantity
            c.execute("""INSERT INTO stock_movements
                (product_id,product_name,product_code,movement_type,quantity,previous_stock,
                 resulting_stock,note,supplier,unit_purchase_price,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(product["id"],product["name"],product["code"],
                movement_type,quantity,previous_stock,resulting_stock,note,
                supplier if movement_type == "stock_in" else "",
                purchase_price if movement_type == "stock_in" else None,
                datetime.now().isoformat(timespec="seconds")))
            movement_id = c.execute("SELECT LAST_INSERT_ID()").fetchone()[0]
            movement = c.execute("SELECT * FROM stock_movements WHERE id=?",
                (movement_id,)).fetchone()
            audit(c,"added","stock movement",movement["id"],product["code"],
                {"movement":dict(movement),"product":dict(product)})
        flash("Stock movement recorded.","success")
    except (ValueError, mysql.connector.Error) as e:
        flash("Could not record stock movement: "+str(e),"danger")
    return redirect(url_for("stock_maintenance"))

@app.route("/stock/settings", methods=["POST"])
def stock_settings():
    try:
        minimum_stock = int(request.form.get("minimum_stock",""))
        if minimum_stock < 0 or minimum_stock>2147483647:
            raise ValueError("Minimum stock must be between 0 and 2,147,483,647.")
        with db() as c:
            previous = c.execute("SELECT minimum_stock FROM stock_preferences WHERE id=1").fetchone()[0]
            c.execute("UPDATE stock_preferences SET minimum_stock=? WHERE id=1",(minimum_stock,))
            audit(c,"updated","stock alert setting",1,"minimum stock",
                {"before":{"minimum_stock":previous},"after":{"minimum_stock":minimum_stock}})
        flash("Minimum stock alert updated.","success")
    except (ValueError, mysql.connector.Error) as e:
        flash("Could not update minimum stock alert: "+str(e),"danger")
    return redirect(url_for("stock_maintenance"))

@app.route("/products")
def products():
    department = request.args.get("department","")
    q = request.args.get("q","").strip()
    with db() as c:
        sql = "SELECT * FROM products WHERE 1=1"; args=[]
        if department: sql += " AND department=?"; args.append(department)
        if q: sql += " AND (name LIKE ? OR code LIKE ? OR category LIKE ? OR subcategory LIKE ?)"; args += [f"%{q}%"]*4
        rows = c.execute(sql+" ORDER BY department,category,name,size,colour",args).fetchall()
    return render_template("products.html", products=rows, department=department, q=q)

@app.route("/products/save", methods=["POST"])
def product_save():
    f=request.form
    try:
        name=f.get("name","").strip()
        code=f.get("code","").strip()
        department=f.get("department","").strip()
        category=f.get("category","").strip()
        if not name:
            raise ValueError("Product name is required.")
        if not code:
            raise ValueError("Product code is required.")
        if department not in {"Footwear","Dress"}:
            raise ValueError("Choose a valid department.")
        if not category:
            raise ValueError("Product category is required.")
        stock_text=f.get("stock","0").strip()
        if not stock_text.isdecimal():
            raise ValueError("Stock quantity must be a non-negative whole number.")
        stock=int(stock_text)
        if stock>2147483647:
            raise ValueError("Stock quantity is outside the supported range.")
        purchase_price=parse_amount(f.get("purchase_price"),"Purchase price")
        selling_price=parse_amount(f.get("selling_price"),"Selling price")
        if selling_price <= 0:
            raise ValueError("Selling price must be greater than zero.")
        image_name=upload_image(request.files.get("image"),"product")
        with db() as c:
            pid=f.get("id","").strip()
            vals=(name,code,department,category,f.get("subcategory","").strip(),
              f.get("gender","").strip(),f.get("age_group","").strip(),f.get("size","").strip(),
              f.get("colour","").strip(),f.get("brand","").strip(),
              float(purchase_price),float(selling_price),stock)
            if pid:
                if not pid.isdecimal():
                    raise ValueError("Invalid product identifier.")
                current = c.execute("SELECT * FROM products WHERE id=?",(pid,)).fetchone()
                if not current:
                    raise ValueError("Product not found.")
                before = dict(current)
                if image_name:
                    c.execute("""UPDATE products SET name=?,code=?,department=?,category=?,subcategory=?,gender=?,age_group=?,size=?,colour=?,brand=?,purchase_price=?,selling_price=?,image=? WHERE id=?""", vals[:12]+(image_name,pid))
                else:
                    c.execute("""UPDATE products SET name=?,code=?,department=?,category=?,subcategory=?,gender=?,age_group=?,size=?,colour=?,brand=?,purchase_price=?,selling_price=? WHERE id=?""", vals[:12]+(pid,))
                difference = stock-current["stock"]
                if difference:
                    movement_type = "adjustment_in" if difference > 0 else "adjustment_out"
                    c.execute("UPDATE products SET stock=? WHERE id=?",(stock,pid))
                    c.execute("""INSERT INTO stock_movements
                        (product_id,product_name,product_code,movement_type,quantity,note,created_at)
                        VALUES(?,?,?,?,?,?,?)""",(pid,f["name"].strip(),f["code"].strip(),
                        movement_type,abs(difference),"Stock corrected while editing product",
                        datetime.now().isoformat(timespec="seconds")))
                    movement_id = c.execute("SELECT LAST_INSERT_ID()").fetchone()[0]
                    movement = c.execute("SELECT * FROM stock_movements WHERE id=?",
                        (movement_id,)).fetchone()
                    audit(c,"added","stock movement",movement["id"],f["code"].strip(),
                        {"movement":dict(movement)})
                updated = c.execute("SELECT * FROM products WHERE id=?",(pid,)).fetchone()
                audit(c,"updated","product",pid,updated["code"],
                    {"before":before,"after":dict(updated)})
            else:
                c.execute("""INSERT INTO products(name,code,department,category,subcategory,gender,age_group,size,colour,brand,purchase_price,selling_price,stock,image,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", vals+(image_name,datetime.now().isoformat(timespec="seconds")))
                pid = c.execute("SELECT LAST_INSERT_ID()").fetchone()[0]
                if stock:
                    c.execute("""INSERT INTO stock_movements
                        (product_id,product_name,product_code,movement_type,quantity,note,created_at)
                        VALUES(?,?,?,'opening',?,?,?)""",(pid,name,code,
                        stock,"Opening stock entered during product setup",
                        datetime.now().isoformat(timespec="seconds")))
                    movement_id = c.execute("SELECT LAST_INSERT_ID()").fetchone()[0]
                    movement = c.execute("SELECT * FROM stock_movements WHERE id=?",
                        (movement_id,)).fetchone()
                    audit(c,"added","stock movement",movement["id"],f["code"].strip(),
                        {"movement":dict(movement)})
                created = c.execute("SELECT * FROM products WHERE id=?",(pid,)).fetchone()
                audit(c,"created","product",pid,created["code"],dict(created))
        flash("Product saved successfully.","success")
    except ValueError as e:
        flash("Could not save product: "+str(e),"danger")
    except IntegrityError as e:
        logging.info("Product save rejected by a database constraint: %s",e)
        flash("Could not save product: that product code is already in use.","danger")
    except mysql.connector.Error:
        logging.exception("Database error while saving a product.")
        flash("Could not save product because of a database error. No changes were saved.","danger")
    return redirect(url_for("products"))

@app.route("/products/delete/<int:pid>", methods=["POST"])
def product_delete(pid):
    try:
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            product=c.execute("SELECT * FROM products WHERE id=?",(pid,)).fetchone()
            if not product:
                abort(404)
            movements = c.execute("SELECT * FROM stock_movements WHERE product_id=?",
                (pid,)).fetchall()
            audit(c,"deleted","product",pid,product["code"],
                {"product":dict(product),"stock_movements":[dict(row) for row in movements]})
            c.execute("UPDATE bill_items SET product_id=NULL WHERE product_id=?",(pid,))
            c.execute("DELETE FROM stock_movements WHERE product_id=?",(pid,))
            c.execute("DELETE FROM products WHERE id=?",(pid,))
        flash("Product deleted. Existing bills keep their saved product details.","success")
    except mysql.connector.Error as e:
        flash("Could not delete product: "+str(e),"danger")
    return redirect(url_for("products"))

@app.route("/customers/delete/<int:cid>", methods=["POST"])
def customer_delete(cid):
    with db() as c:
        customer = c.execute("SELECT * FROM customers WHERE id=?",(cid,)).fetchone()
        if not customer:
            abort(404)
        audit(c,"deleted","customer",cid,customer["phone"] or customer["name"],
            {"customer":dict(customer)})
        c.execute("UPDATE bills SET customer_id=NULL WHERE customer_id=?",(cid,))
        c.execute("DELETE FROM customers WHERE id=?",(cid,))
    flash("Customer deleted. Existing bills keep their saved customer details.","success")
    return redirect(url_for("customers"))

@app.route("/sales/delete/<int:bid>", methods=["POST"])
def bill_delete(bid):
    try:
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            bill = c.execute("SELECT * FROM bills WHERE id=?",(bid,)).fetchone()
            if not bill:
                abort(404)
            bill_items = c.execute("SELECT * FROM bill_items WHERE bill_id=?",(bid,)).fetchall()
            payments = c.execute("SELECT * FROM payments WHERE bill_id=?",(bid,)).fetchall()
            movements = c.execute("SELECT * FROM stock_movements WHERE bill_id=?",(bid,)).fetchall()
            audit(c,"deleted","bill",bid,bill["bill_no"],
                {"bill":dict(bill),"items":[dict(row) for row in bill_items],
                 "payments":[dict(row) for row in payments],
                 "stock_movements":[dict(row) for row in movements]})
            items = c.execute("""SELECT product_id,SUM(quantity) quantity
                FROM bill_items WHERE bill_id=? AND product_id IS NOT NULL
                GROUP BY product_id""",(bid,)).fetchall()
            for item in items:
                c.execute("UPDATE products SET stock=stock+? WHERE id=?",
                    (item["quantity"],item["product_id"]))
            c.execute("DELETE FROM stock_movements WHERE bill_id=?",(bid,))
            c.execute("DELETE FROM bills WHERE id=?",(bid,))
        flash("Bill deleted and sold quantities returned to stock.","success")
    except mysql.connector.Error as e:
        flash("Could not delete bill: "+str(e),"danger")
    return redirect(url_for("sales"))

@app.route("/payments/<int:payment_id>/delete", methods=["POST"])
def payment_delete(payment_id):
    try:
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            payment = c.execute("SELECT bill_id FROM payments WHERE id=?",(payment_id,)).fetchone()
            if not payment:
                abort(404)
            bill_id = payment["bill_id"]
            payment_record = c.execute("SELECT * FROM payments WHERE id=?",(payment_id,)).fetchone()
            bill_before = c.execute("SELECT * FROM bills WHERE id=?",(bill_id,)).fetchone()
            audit_id = audit(c,"deleted","payment",payment_id,str(bill_id),
                {"payment":dict(payment_record),"bill_before":dict(bill_before)})
            c.execute("DELETE FROM payments WHERE id=?",(payment_id,))
            bill = c.execute("SELECT total FROM bills WHERE id=?",(bill_id,)).fetchone()
            received = round(c.execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE bill_id=?",
                (bill_id,)).fetchone()[0],2)
            balance = round(bill["total"]-received,2)
            status = "Paid" if balance == 0 else ("Partially Paid" if received > 0 else "Pending")
            c.execute("UPDATE bills SET received=?,balance=?,payment_status=? WHERE id=?",
                (received,balance,status,bill_id))
            bill_after = c.execute("SELECT * FROM bills WHERE id=?",(bill_id,)).fetchone()
            c.execute("UPDATE audit_log SET details_json=? WHERE id=?",
                (json.dumps({"payment":dict(payment_record),"bill_before":dict(bill_before),
                             "bill_after":dict(bill_after)},ensure_ascii=False),audit_id))
        flash("Payment deleted and bill balance recalculated.","success")
    except mysql.connector.Error as e:
        flash("Could not delete payment: "+str(e),"danger")
    return redirect(url_for("receipt",bid=bill_id))

@app.route("/stock/movements/<int:movement_id>/delete", methods=["POST"])
def stock_movement_delete(movement_id):
    try:
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            movement = c.execute("SELECT * FROM stock_movements WHERE id=?",(movement_id,)).fetchone()
            if not movement:
                abort(404)
            if movement["movement_type"] == "sale":
                raise ValueError("Bill sales cannot be deleted here. Delete the related bill to reverse the sale.")
            if movement["movement_type"] not in {"opening","stock_in","stock_out","adjustment_in","adjustment_out"}:
                raise ValueError("This stock movement cannot be reversed.")
            product = c.execute("SELECT * FROM products WHERE id=?",
                (movement["product_id"],)).fetchone() if movement["product_id"] is not None else None
            audit(c,"deleted","stock movement",movement_id,movement["product_code"],
                {"movement":dict(movement),"product_before":dict(product) if product else None})
            product_id = movement["product_id"]
            if product_id is not None:
                stock_change = movement["quantity"] if movement["movement_type"] in {"stock_out","adjustment_out"} else -movement["quantity"]
                if stock_change < 0:
                    product = c.execute("SELECT stock FROM products WHERE id=?",(product_id,)).fetchone()
                    if not product or product["stock"] < abs(stock_change):
                        raise ValueError("This stock addition cannot be reversed because some of its units are no longer available.")
                c.execute("UPDATE products SET stock=stock+? WHERE id=?",(stock_change,product_id))
            c.execute("DELETE FROM stock_movements WHERE id=?",(movement_id,))
        flash("Stock movement deleted and inventory adjusted.","success")
    except (ValueError, mysql.connector.Error) as e:
        flash("Could not delete stock movement: "+str(e),"danger")
    return redirect(url_for("stock_maintenance"))

@app.route("/api/products")
def api_products():
    q=request.args.get("q","").strip()
    dept=request.args.get("department","")
    category=request.args.get("category","").strip()
    with db() as c:
        sql="SELECT * FROM products WHERE stock>0"; args=[]
        if q: sql+=" AND (name LIKE ? OR code LIKE ? OR category LIKE ? OR subcategory LIKE ?)"; args += [f"%{q}%"]*4
        if dept: sql+=" AND department=?"; args.append(dept)
        if category:
            sql+=" AND COALESCE(NULLIF(subcategory,''),category)=?"
            args.append(category)
        rows=c.execute(sql+" ORDER BY name LIMIT 100",args).fetchall()
    return jsonify([dict(x) for x in rows])

@app.route("/billing")
def billing():
    with db() as c:
        categories=c.execute("""SELECT DISTINCT COALESCE(NULLIF(subcategory,''),category) category
            FROM products WHERE stock>0 AND COALESCE(NULLIF(subcategory,''),category)<>''
            ORDER BY category""").fetchall()
    return render_template("billing.html",categories=categories)

@app.route("/api/bills", methods=["POST"])
def create_bill():
    try:
        data=request.get_json(silent=True)
        if not isinstance(data,dict):
            raise ValueError("A valid JSON request is required.")
        items=data.get("items")
        if not isinstance(items,list) or not items or len(items)>200:
            raise ValueError("Add between 1 and 200 bill items.")
        idempotency_key=data.get("idempotency_key","")
        if not isinstance(idempotency_key,str) or len(idempotency_key)!=32:
            raise ValueError("The bill request key is invalid. Reload the page and try again.")
        try:
            bytes.fromhex(idempotency_key)
        except ValueError:
            raise ValueError("The bill request key is invalid. Reload the page and try again.")
        discount=parse_amount(data.get("discount"),"Discount")
        received=parse_amount(data.get("received"),"Amount received")
        method=data.get("payment_method","Cash")
        if method not in {"Cash","UPI","Card"}:
            raise ValueError("Choose Cash, UPI or Card as the payment method.")
        cname=data.get("customer_name") or ""
        phone=data.get("customer_phone") or ""
        if not isinstance(cname,str) or not isinstance(phone,str):
            raise ValueError("Customer name and phone must be text.")
        cname=cname.strip()
        phone=phone.strip()
        if len(cname)>120 or len(phone)>32:
            raise ValueError("Customer name or phone is too long.")
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            previous=c.execute(
                "SELECT response_json FROM bill_requests WHERE idempotency_key=?",
                (idempotency_key,)).fetchone()
            if previous:
                return jsonify(json.loads(previous["response_json"]))
            subtotal=Decimal("0.00")
            checked=[]
            requested_quantities={}
            for item in items:
                if not isinstance(item,dict):
                    raise ValueError("Each bill item must be a product and quantity.")
                product_id=item.get("id")
                qty=item.get("quantity")
                if isinstance(product_id,bool) or not isinstance(product_id,int) or product_id<1:
                    raise ValueError("Invalid product.")
                if isinstance(qty,bool) or not isinstance(qty,int) or qty<1:
                    raise ValueError("Product quantities must be positive whole numbers.")
                if qty>2147483647:
                    raise ValueError("Product quantity is outside the supported range.")
                p=c.execute("SELECT * FROM products WHERE id=?",(product_id,)).fetchone()
                if not p:
                    raise ValueError("Invalid product.")
                requested_quantities[p["id"]]=requested_quantities.get(p["id"],0)+qty
                if p["stock"]<requested_quantities[p["id"]]: raise ValueError(f"Insufficient stock for {p['name']} ({p['size']} / {p['colour']}). Available: {p['stock']}")
                price=Decimal(str(p["selling_price"])).quantize(Decimal("0.01"))
                subtotal += price*qty
                checked.append((p,qty,price))
            if discount>subtotal: raise ValueError("Discount cannot exceed subtotal.")
            subtotal=subtotal.quantize(Decimal("0.01"))
            total=(subtotal-discount).quantize(Decimal("0.01"))
            if received>total:
                raise ValueError("Amount received cannot exceed the bill total.")
            balance=(total-received).quantize(Decimal("0.01"))
            status="Paid" if balance==0 else ("Partially Paid" if received>0 else "Pending")
            cid=None
            if phone:
                old=c.execute("SELECT * FROM customers WHERE phone=?",(phone,)).fetchone()
                if old:
                    cid=old["id"]
                    if cname and cname != old["name"]:
                        c.execute("UPDATE customers SET name=? WHERE id=?",(cname,cid))
                        changed = c.execute("SELECT * FROM customers WHERE id=?",(cid,)).fetchone()
                        audit(c,"updated","customer",cid,phone,
                            {"before":dict(old),"after":dict(changed)})
                else:
                    cur=c.execute("INSERT INTO customers(name,phone,created_at) VALUES(?,?,?)",(cname or "Customer",phone,datetime.now().isoformat(timespec="seconds")))
                    cid=cur.lastrowid
                    added = c.execute("SELECT * FROM customers WHERE id=?",(cid,)).fetchone()
                    audit(c,"created","customer",cid,phone,dict(added))
            bill_no="HI5-"+datetime.now().strftime("%y%m%d-%H%M%S")+"-"+secrets.token_hex(3).upper()
            bill_created_at=datetime.now().isoformat(timespec="seconds")
            cur=c.execute("""INSERT INTO bills(bill_no,customer_id,customer_name,customer_phone,subtotal,discount,total,received,balance,payment_method,payment_status,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(bill_no,cid,cname,phone,float(subtotal),float(discount),float(total),float(received),float(balance),method,status,bill_created_at))
            bid=cur.lastrowid
            for p,qty,price in checked:
                c.execute("""INSERT INTO bill_items(bill_id,product_id,product_name,code,department,category,size,colour,quantity,price,line_total)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(bid,p["id"],p["name"],p["code"],p["department"],p["subcategory"] or p["category"],p["size"],p["colour"],qty,float(price),float(price*qty)))
                updated=c.execute("UPDATE products SET stock=stock-? WHERE id=? AND stock>=?",
                    (qty,p["id"],qty))
                if updated.rowcount != 1:
                    raise ValueError(f"Insufficient stock for {p['name']} ({p['size']} / {p['colour']}).")
                resulting_stock=c.execute("SELECT stock FROM products WHERE id=?",
                    (p["id"],)).fetchone()["stock"]
                previous_stock=resulting_stock+qty
                c.execute("""INSERT INTO stock_movements
                    (product_id,product_name,product_code,movement_type,quantity,previous_stock,
                     resulting_stock,note,unit_purchase_price,bill_id,created_at)
                    VALUES(?,?,?,'sale',?,?,?,?,?,?,?)""",(p["id"],p["name"],p["code"],qty,
                    previous_stock,resulting_stock,f"Sale {bill_no}",
                    p["purchase_price"] if Decimal(str(p["purchase_price"])) > 0 else None,bid,
                    datetime.now().isoformat(timespec="seconds")))
            if received>0:
                c.execute("INSERT INTO payments(bill_id,amount,method,created_at) VALUES(?,?,?,?)",(bid,float(received),method,datetime.now().isoformat(timespec="seconds")))
            bill_record=c.execute("SELECT * FROM bills WHERE id=?",(bid,)).fetchone()
            bill_items=c.execute("SELECT * FROM bill_items WHERE bill_id=?",(bid,)).fetchall()
            bill_payments=c.execute("SELECT * FROM payments WHERE bill_id=?",(bid,)).fetchall()
            bill_movements=c.execute("SELECT * FROM stock_movements WHERE bill_id=?",(bid,)).fetchall()
            audit(c,"created","bill",bid,bill_no,
                {"bill":dict(bill_record),"items":[dict(row) for row in bill_items],
                 "payments":[dict(row) for row in bill_payments],
                 "stock_movements":[dict(row) for row in bill_movements]})
            shop=dict(c.execute("SELECT * FROM settings WHERE id=1").fetchone())
            response={
                "ok":True,"bill_id":bid,"bill_no":bill_no,"receipt":{
                    "shopName":shop["shop_name"],
                    "address":format_receipt_address(shop["address"]),
                    "shopPhone":shop["phone"],
                    "logoUrl":url_for("static",filename="uploads/"+shop["logo"]) if shop["logo"] else "",
                    "billNumber":bill_no,"date":bill_created_at,
                    "customerName":cname or "Walk-in Customer","customerPhone":phone or "—",
                    "subtotal":float(subtotal),"discount":float(discount),"total":float(total),
                    "received":float(received),"balance":float(balance),
                    "paymentMethod":method,"paymentStatus":status,
                    "items":[{
                        "name":p["name"],"category":p["subcategory"] or p["category"],
                        "size":p["size"],"colour":p["colour"],"quantity":qty,
                        "price":float(price),"total":float((price*qty).quantize(Decimal("0.01")))
                    } for p,qty,price in checked]
                }
            }
            c.execute("""INSERT INTO bill_requests(idempotency_key,bill_id,response_json,created_at)
                VALUES(?,?,?,?)""",(idempotency_key,bid,json.dumps(response,ensure_ascii=False),
                datetime.now().isoformat(timespec="seconds")))
        return jsonify(response)
    except (ValueError, KeyError, TypeError, InvalidOperation) as e:
        return jsonify(error=str(e)),400
    except mysql.connector.Error:
        logging.exception("Database error while creating a bill; the transaction was rolled back.")
        return jsonify(error="The bill could not be saved. No stock or payment changes were committed."),500

def get_bill(bid):
    with db() as c:
        b=c.execute("SELECT * FROM bills WHERE id=?",(bid,)).fetchone()
        if not b: return None,None
        items=c.execute("SELECT * FROM bill_items WHERE bill_id=?",(bid,)).fetchall()
    return b,items

@app.route("/receipt/<int:bid>")
def receipt(bid):
    b,items=get_bill(bid)
    if not b: abort(404)
    with db() as c:
        payments=c.execute("SELECT * FROM payments WHERE bill_id=? ORDER BY id DESC",(bid,)).fetchall()
        shop_address=format_receipt_address(
            c.execute("SELECT address FROM settings WHERE id=1").fetchone()["address"])
    return render_template("receipt.html", bill=b, items=items, payments=payments,
        receipt_address=shop_address)

@app.route("/sales")
def sales():
    q=request.args.get("q",""); status=request.args.get("status",""); start=request.args.get("start",""); end=request.args.get("end","")
    dept=request.args.get("department","")
    where=" WHERE 1=1"; args=[]
    if q: where+=" AND (bill_no LIKE ? OR customer_name LIKE ? OR customer_phone LIKE ?)"; args += [f"%{q}%"]*3
    if status: where+=" AND payment_status=?"; args.append(status)
    if start: where+=" AND date(created_at)>=?"; args.append(start)
    if end: where+=" AND date(created_at)<=?"; args.append(end)
    if dept: where+=" AND id IN (SELECT bill_id FROM bill_items WHERE department=?)"; args.append(dept)
    with db() as c:
        rows=c.execute("SELECT * FROM bills"+where+" ORDER BY id DESC",args).fetchall()
        sales_totals=c.execute("""SELECT COUNT(*) bill_count,
            COALESCE(SUM(total),0) total_sales,
            COALESCE(SUM(received),0) total_received,
            COALESCE(SUM(balance),0) total_balance
            FROM bills"""+where,args).fetchone()
    return render_template("sales.html", bills=rows, sales_totals=sales_totals,
        q=q, status=status, start=start, end=end, department=dept)

@app.route("/customers")
def customers():
    with db() as c:
        rows=c.execute("""SELECT c.*,COUNT(b.id) purchases,COALESCE(SUM(b.total),0) total_purchases,
            COALESCE(SUM(b.received),0) total_paid,COALESCE(SUM(b.balance),0) outstanding,
            MAX(b.created_at) last_purchase FROM customers c LEFT JOIN bills b ON b.customer_id=c.id
            GROUP BY c.id ORDER BY c.name""").fetchall()
    return render_template("customers.html", customers=rows)

@app.route("/customers/save",methods=["POST"])
def customer_save():
    name=request.form.get("name","").strip(); phone=request.form.get("phone","").strip()
    if not name: flash("Customer name is required.","danger")
    elif len(name)>120 or len(phone)>32: flash("Customer name or phone is too long.","danger")
    else:
        try:
            with db() as c:
                cur=c.execute("INSERT INTO customers(name,phone,created_at) VALUES(?,?,?)",
                    (name,phone or None,datetime.now().isoformat(timespec="seconds")))
                customer=c.execute("SELECT * FROM customers WHERE id=?",(cur.lastrowid,)).fetchone()
                audit(c,"created","customer",customer["id"],customer["phone"] or customer["name"],
                    dict(customer))
            flash("Customer added.","success")
        except IntegrityError: flash("That phone number already exists.","warning")
    return redirect(url_for("customers"))

@app.route("/bill/<int:bid>/payment",methods=["POST"])
def add_payment(bid):
    method=request.form.get("method","Cash")
    try:
        amount=parse_amount(request.form.get("amount"),"Payment amount")
        if amount<=0: raise ValueError("Enter a payment amount greater than zero.")
        if method not in {"Cash","UPI","Card"}: raise ValueError("Choose a valid payment method.")
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            b=c.execute("SELECT * FROM bills WHERE id=?",(bid,)).fetchone()
            if not b: abort(404)
            if amount>Decimal(str(b["balance"])):
                raise ValueError("Payment amount cannot exceed the remaining balance.")
            received=(Decimal(str(b["received"]))+amount).quantize(Decimal("0.01"))
            balance=(Decimal(str(b["total"]))-received).quantize(Decimal("0.01"))
            status="Paid" if balance==0 else "Partially Paid"
            c.execute("UPDATE bills SET received=?,balance=?,payment_status=? WHERE id=?",
                (float(received),float(balance),status,bid))
            cur=c.execute("INSERT INTO payments(bill_id,amount,method,created_at) VALUES(?,?,?,?)",
                (bid,float(amount),method,datetime.now().isoformat(timespec="seconds")))
            payment=c.execute("SELECT * FROM payments WHERE id=?",(cur.lastrowid,)).fetchone()
            updated_bill=c.execute("SELECT * FROM bills WHERE id=?",(bid,)).fetchone()
            audit(c,"created","payment",payment["id"],b["bill_no"],
                {"payment":dict(payment),"bill_after":dict(updated_bill)})
        flash("Payment recorded.","success")
    except ValueError as e:
        flash(str(e),"danger")
    except mysql.connector.Error:
        logging.exception("Database error while recording a payment.")
        flash("Could not record payment because of a database error. No changes were saved.","danger")
    return redirect(url_for("receipt",bid=bid))

@app.route("/settings",methods=["GET","POST"])
def shop_settings():
    if request.method=="POST":
        f=request.form
        try:
            shop_name=f.get("shop_name","").strip()
            address=f.get("address","").strip()
            phone=normalize_phone(f.get("phone",""),"Shop phone number")
            whatsapp_phone=normalize_phone(
                f.get("whatsapp_phone",""),"Shop WhatsApp number")
            if not shop_name or not address:
                raise ValueError("Shop name and address are required.")
            if len(shop_name)>255 or len(address)>2000 or len(phone)>32:
                raise ValueError("Shop name, address or phone exceeds its supported length.")
            logo=upload_image(request.files.get("logo"),"logo")
            with db() as c:
                before=dict(c.execute("SELECT * FROM settings WHERE id=1").fetchone())
                if logo:
                    c.execute("""UPDATE settings SET shop_name=?,address=?,phone=?,
                        whatsapp_phone=?,logo=? WHERE id=1""",
                        (shop_name,address,phone,whatsapp_phone,logo))
                else:
                    c.execute("""UPDATE settings SET shop_name=?,address=?,phone=?,
                        whatsapp_phone=? WHERE id=1""",
                        (shop_name,address,phone,whatsapp_phone))
                after=dict(c.execute("SELECT * FROM settings WHERE id=1").fetchone())
                audit(c,"updated","shop settings",1,after["shop_name"],
                    {"before":before,"after":after})
            flash("Shop settings updated.","success")
        except ValueError as e: flash(str(e),"danger")
        except mysql.connector.Error:
            logging.exception("Database error while updating shop settings.")
            flash("Could not save shop settings because of a database error. No changes were saved.","danger")
        return redirect(url_for("shop_settings"))
    return render_template("settings.html")

@app.route("/settings/reset",methods=["POST"])
def reset_shop_settings():
    with db() as c:
        before=dict(c.execute("SELECT * FROM settings WHERE id=1").fetchone())
        c.execute("""UPDATE settings SET shop_name=?,address=?,phone='',whatsapp_phone='',
            logo='' WHERE id=1""",
            ("HI-5 FOOTWEAR & BOUTIQUES",
             "Gandhi Street, R.S. Mangalam, Ramanathapuram, Tamil Nadu"))
        after=dict(c.execute("SELECT * FROM settings WHERE id=1").fetchone())
        audit(c,"reset","shop settings",1,after["shop_name"],
            {"before":before,"after":after})
    flash("Shop settings reset to the HI-5 defaults.","success")
    return redirect(url_for("shop_settings"))

@app.route("/settings/clear-all",methods=["POST"])
def clear_all_records():
    if request.form.get("confirm_text","").strip() != "CLEAR ALL":
        flash("Type CLEAR ALL exactly to confirm permanent data removal.","danger")
        return redirect(url_for("shop_settings"))
    try:
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            for table, entity, id_column, ref_column in (
                ("products","product","id","code"),
                ("customers","customer","id","phone"),
                ("bills","bill","id","bill_no"),
                ("bill_items","bill item","id","bill_id"),
                ("payments","payment","id","bill_id"),
                ("stock_movements","stock movement","id","product_code"),
            ):
                for row in c.execute(f"SELECT * FROM {table}").fetchall():
                    audit(c,"cleared",entity,row[id_column],str(row[ref_column] or ""),
                        {table.rstrip("s"):dict(row)})
            settings_before=dict(c.execute("SELECT * FROM settings WHERE id=1").fetchone())
            stock_before=dict(c.execute("SELECT * FROM stock_preferences WHERE id=1").fetchone())
            audit(c,"cleared","shop settings",1,settings_before["shop_name"],settings_before)
            audit(c,"cleared","stock alert setting",1,"minimum stock",stock_before)
            record_counts={table:c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("products","customers","bills","bill_items","payments","stock_movements")}
            audit(c,"cleared","all records","","CLEAR ALL",{"counts":record_counts})
            c.execute("DELETE FROM bill_items")
            c.execute("DELETE FROM payments")
            c.execute("DELETE FROM stock_movements")
            c.execute("DELETE FROM bills")
            c.execute("DELETE FROM products")
            c.execute("DELETE FROM customers")
            c.execute("""UPDATE settings SET shop_name=?,address=?,phone='',logo='' WHERE id=1""",
                ("HI-5 FOOTWEAR & BOUTIQUES",
                 "Gandhi Street, R.S. Mangalam, Ramanathapuram, Tamil Nadu"))
            c.execute("UPDATE stock_preferences SET minimum_stock=5 WHERE id=1")
            c.execute("DELETE FROM inventory_migrations")
            c.execute("""INSERT INTO inventory_migrations(migration_key,completed_at)
                VALUES('clear-all-completed',?)""",(datetime.now().isoformat(timespec="seconds"),))
        flash("Current products, customers, bills, payments and stock history were cleared. Their snapshots remain in History & Excel.","success")
    except mysql.connector.Error as e:
        flash("Could not clear all records: "+str(e),"danger")
    return redirect(url_for("shop_settings"))

def export_rows(kind):
    with db() as c:
        if kind=="sales":
            return [dict(x) for x in c.execute("""SELECT bill_no,created_at,customer_name,customer_phone,subtotal,discount,total,received,balance,payment_method,payment_status FROM bills ORDER BY id DESC""")]
        if kind=="products":
            return [dict(x) for x in c.execute("""SELECT name,code,department,category,subcategory,gender,age_group,size,colour,brand,purchase_price,selling_price,stock FROM products ORDER BY department,name""")]
        if kind=="customers":
            return [dict(x) for x in c.execute("""SELECT c.name,c.phone,COUNT(b.id) purchases,COALESCE(SUM(b.total),0) total_purchases,COALESCE(SUM(b.received),0) total_paid,COALESCE(SUM(b.balance),0) outstanding,MAX(b.created_at) last_purchase FROM customers c LEFT JOIN bills b ON b.customer_id=c.id GROUP BY c.id""")]
        if kind=="pending":
            return [dict(x) for x in c.execute("SELECT bill_no,created_at,customer_name,customer_phone,total,received,balance,payment_status FROM bills WHERE balance>0 ORDER BY id DESC")]
    return []

def spreadsheet_safe(value):
    if isinstance(value,str) and value.lstrip(" \t\r\n").startswith(("=","+","-","@")):
        return "'"+value
    return value

@app.route("/export/<kind>/<fmt>")
def export(kind,fmt):
    if kind not in {"sales","products","customers","pending"} or fmt not in {"csv","xlsx"}: abort(404)
    rows=export_rows(kind)
    columns=list(rows[0].keys()) if rows else ["No records"]
    if fmt=="csv":
        s=io.StringIO(); w=csv.DictWriter(s,fieldnames=columns); w.writeheader()
        for r in rows: w.writerow({key:spreadsheet_safe(value) for key,value in r.items()})
        data=io.BytesIO(s.getvalue().encode("utf-8-sig")); mime="text/csv"; ext="csv"
    else:
        wb=Workbook(); ws=wb.active; ws.title=kind.title()
        ws.append(columns)
        for cell in ws[1]:
            cell.font=Font(bold=True,color="FFFFFF"); cell.fill=PatternFill("solid",fgColor="B5122B")
        for r in rows: ws.append([spreadsheet_safe(r.get(k)) for k in columns])
        for col in ws.columns:
            letter=col[0].column_letter
            ws.column_dimensions[letter].width=min(max(max(len(str(x.value or "")) for x in col)+2,12),32)
        data=io.BytesIO(); wb.save(data); data.seek(0); mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"; ext="xlsx"
    data.seek(0)
    return send_file(data,as_attachment=True,download_name=f"hi5_{kind}_{date.today()}.{ext}",mimetype=mime)

@app.route("/history")
def history():
    with db() as c:
        counts={table:c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("products","customers","bills","bill_items","payments","stock_movements")}
        total_events=c.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        events=[dict(row) for row in c.execute("""SELECT * FROM audit_log
            ORDER BY id DESC LIMIT 100""").fetchall()]
    return render_template("history.html",counts=counts,total_events=total_events,events=events)

@app.route("/export/history.xlsx")
def export_history():
    worksheets=(
        ("Activity Log","audit_log"),
        ("Products","products"),
        ("Customers","customers"),
        ("Bills","bills"),
        ("Bill Items","bill_items"),
        ("Payments","payments"),
        ("Stock Movements","stock_movements"),
        ("Shop Settings","settings"),
        ("Stock Alerts","stock_preferences"),
    )
    workbook=Workbook()
    workbook.remove(workbook.active)
    with db() as c:
        for title,table in worksheets:
            cursor=c.execute(f"SELECT * FROM {table} ORDER BY id")
            headers=[column[0] for column in cursor.description]
            sheet=workbook.create_sheet(title)
            sheet.append(headers)
            for cell in sheet[1]:
                cell.font=Font(bold=True,color="FFFFFF")
                cell.fill=PatternFill("solid",fgColor="B5122B")
            for row in cursor:
                values=[]
                for value in row:
                    if isinstance(value,str) and value.startswith(("=","+","-","@")):
                        value="'"+value
                    values.append(value)
                sheet.append(values)
            sheet.freeze_panes="A2"
            if headers:
                sheet.auto_filter.ref=sheet.dimensions
            for column in sheet.columns:
                letter=column[0].column_letter
                width=max((len(str(cell.value or "")) for cell in column),default=10)
                sheet.column_dimensions[letter].width=min(max(width+2,12),42)
    output=io.BytesIO()
    workbook.save(output)
    output.seek(0)
    return send_file(output,as_attachment=True,
        download_name=f"hi5_history_{date.today()}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

def report_filters(args):
    today = date.today()
    period = args.get("period", "this_month")
    if period == "today":
        start = end = today
    elif period == "yesterday":
        start = end = today - timedelta(days=1)
    elif period == "this_week":
        start = today - timedelta(days=today.weekday())
        end = today
    elif period == "this_month":
        start = today.replace(day=1)
        end = today
    elif period == "this_year":
        start = today.replace(month=1, day=1)
        end = today
    elif period == "custom":
        try:
            start = date.fromisoformat(args.get("start", ""))
            end = date.fromisoformat(args.get("end", ""))
        except ValueError as error:
            raise ValueError("Choose a valid start and end date for the custom range.") from error
        if start.isoformat() != args.get("start") or end.isoformat() != args.get("end"):
            raise ValueError("Dates must use the YYYY-MM-DD format.")
        if start > end:
            raise ValueError("The start date must be on or before the end date.")
    else:
        raise ValueError("Choose today, yesterday, this week, this month, this year, or a custom range.")

    product_id = args.get("product_id", "").strip()
    if product_id and not product_id.isdecimal():
        raise ValueError("Choose a valid product filter.")
    category = args.get("category", "").strip()
    if len(category) > 191:
        raise ValueError("The category filter is too long.")
    page_value = args.get("page", "1")
    if not page_value.isdecimal() or int(page_value) < 1:
        raise ValueError("The product analytics page must be a positive number.")
    movement_page_value = args.get("movement_page", "1")
    if not movement_page_value.isdecimal() or int(movement_page_value) < 1:
        raise ValueError("The stock history page must be a positive number.")
    return {
        "period": period,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "start_at": f"{start.isoformat()}T00:00:00",
        "end_at": f"{(end + timedelta(days=1)).isoformat()}T00:00:00",
        "product_id": int(product_id) if product_id else None,
        "category": category,
        "page": int(page_value),
        "page_size": 20,
        "movement_page": int(movement_page_value),
        "movement_page_size": 20,
    }


def report_item_conditions(filters, item_alias="bi", product_alias="p"):
    conditions = []
    params = []
    if filters["product_id"] is not None:
        conditions.append(f"{item_alias}.product_id=?")
        params.append(filters["product_id"])
    if filters["category"]:
        conditions.append(
            f"COALESCE(NULLIF({item_alias}.category,''),"
            f"NULLIF({product_alias}.subcategory,''),{product_alias}.category)=?"
        )
        params.append(filters["category"])
    return (" AND " + " AND ".join(conditions) if conditions else ""), params


def report_details_count(filters):
    item_filter, item_params = report_item_conditions(filters)
    with db() as connection:
        return connection.execute(
            """SELECT COUNT(*) FROM bills b
            JOIN bill_items bi ON bi.bill_id=b.id
            LEFT JOIN products p ON p.id=bi.product_id
            WHERE b.created_at>=? AND b.created_at<?""" + item_filter,
            [filters["start_at"], filters["end_at"], *item_params],
        ).fetchone()[0]


def iter_report_details(filters, batch_size=1000):
    item_filter, item_params = report_item_conditions(filters)
    last_bill_id = last_item_id = None
    while True:
        keyset = ""
        keyset_params = []
        if last_bill_id is not None:
            keyset = " AND (b.id<? OR (b.id=? AND bi.id<?))"
            keyset_params = [last_bill_id, last_bill_id, last_item_id]
        sql = """SELECT b.id bill_id,bi.id item_id,b.bill_no,b.created_at,
            b.customer_name,b.customer_phone,COALESCE(p.name,bi.product_name) product_name,
            bi.code product_code,COALESCE(NULLIF(bi.category,''),p.category,'') category,
            bi.quantity,bi.price,bi.line_total,
            CASE WHEN b.subtotal>0 THEN bi.line_total*b.total/b.subtotal ELSE 0 END net_item_total,
            b.discount,b.total,b.payment_method
            FROM bills b JOIN bill_items bi ON bi.bill_id=b.id
            LEFT JOIN products p ON p.id=bi.product_id
            WHERE b.created_at>=? AND b.created_at<?""" + item_filter + keyset
        with db() as connection:
            batch = connection.execute(
                sql + " ORDER BY b.id DESC,bi.id DESC LIMIT ?",
                [filters["start_at"], filters["end_at"], *item_params, *keyset_params, batch_size],
            ).fetchall()
        if not batch:
            break
        for row in batch:
            yield row
        last_bill_id = batch[-1]["bill_id"]
        last_item_id = batch[-1]["item_id"]
        if len(batch) < batch_size:
            break


def report_xlsx(filters, summary, row_count):
    workbook = Workbook(write_only=True)
    summary_sheet = workbook.create_sheet("Summary")
    summary_sheet.append(["HI-5 FOOTWEAR & BOUTIQUES — Sales Report"])
    summary_sheet.append(["Date range", f"{filters['start']} to {filters['end']}"])
    summary_sheet.append(["Bills", summary["bill_count"]])
    summary_sheet.append(["Sales", float(summary["sales"] or 0)])
    summary_sheet.append(["Amount received", float(summary["received"] or 0)])
    summary_sheet.append(["Balance due", float(summary["balance"] or 0)])
    summary_sheet.append(["Detailed bill items", row_count])
    details = workbook.create_sheet("Sales Details")
    headers = [
        "Bill Number", "Date", "Customer", "Mobile", "Product", "Code",
        "Category", "Quantity", "Unit Price", "Item Value", "Net Item Sales",
        "Bill Discount", "Bill Total", "Payment Method",
    ]
    header_cells = [WriteOnlyCell(details, value=header) for header in headers]
    for cell in header_cells:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="B5122B")
    details.append(header_cells)
    for row in iter_report_details(filters):
        details.append([
            spreadsheet_safe(row["bill_no"]), row["created_at"],
            spreadsheet_safe(row["customer_name"]), spreadsheet_safe(row["customer_phone"]),
            spreadsheet_safe(row["product_name"]), spreadsheet_safe(row["product_code"]),
            spreadsheet_safe(row["category"]), row["quantity"], float(row["price"]),
            float(row["line_total"]), float(row["net_item_total"]), float(row["discount"]),
            float(row["total"]), spreadsheet_safe(row["payment_method"]),
        ])
    details.freeze_panes = "A2"
    details.auto_filter.ref = f"A1:N{row_count + 1}"
    for column, width in zip("ABCDEFGHIJKLMN", (24, 21, 24, 18, 30, 18, 24, 12, 14, 14, 16, 15, 14, 18)):
        details.column_dimensions[column].width = width
    output = io.BytesIO()
    workbook.save(output)
    output.seek(0)
    return output


def report_pdf(filters, summary, row_count):
    output = io.BytesIO()
    document = SimpleDocTemplate(
        output, pagesize=landscape(A4), leftMargin=10 * mm, rightMargin=10 * mm,
        topMargin=10 * mm, bottomMargin=10 * mm,
    )
    styles = getSampleStyleSheet()
    elements = [
        Paragraph("HI-5 FOOTWEAR &amp; BOUTIQUES — Sales Report", styles["Title"]),
        Paragraph(
            f"Date range: {filters['start']} to {filters['end']} &nbsp; | &nbsp; "
            f"Bills: {summary['bill_count']} &nbsp; | &nbsp; "
            f"Sales: INR {Decimal(str(summary['sales'] or 0)):,.2f} &nbsp; | &nbsp; "
            f"Received: INR {Decimal(str(summary['received'] or 0)):,.2f} &nbsp; | &nbsp; "
            f"Balance: INR {Decimal(str(summary['balance'] or 0)):,.2f}",
            styles["BodyText"],
        ),
        Spacer(1, 5 * mm),
    ]
    headings = [
        "Bill number", "Date", "Customer", "Product", "Qty", "Unit price",
        "Net item sales", "Discount", "Bill total", "Payment",
    ]
    rows = [[Paragraph(value, styles["BodyText"]) for value in headings]]
    for row in iter_report_details(filters):
        values = (
            row["bill_no"], str(row["created_at"])[:19], row["customer_name"] or "Walk-in",
            row["product_name"], str(row["quantity"]),
            f"INR {Decimal(str(row['price'])):,.2f}",
            f"INR {Decimal(str(row['net_item_total'])):,.2f}",
            f"INR {Decimal(str(row['discount'])):,.2f}",
            f"INR {Decimal(str(row['total'])):,.2f}", row["payment_method"],
        )
        rows.append([
            Paragraph(html_escape(str(value)), styles["BodyText"])
            for value in values
        ])
    table = LongTable(
        rows,
        colWidths=(22 * mm, 24 * mm, 29 * mm, 39 * mm, 10 * mm, 20 * mm,
                   24 * mm, 20 * mm, 22 * mm, 20 * mm),
        repeatRows=1,
    )
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#171717")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d8d8d8")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    elements.append(table)
    document.build(elements)
    output.seek(0)
    return output


def html_escape(value):
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


@app.route("/reports")
def reports():
    try:
        filters = report_filters(request.args)
    except ValueError as error:
        flash(str(error), "danger")
        filters = report_filters({})
    period_params = [filters["start_at"], filters["end_at"]]
    revenue = "CASE WHEN b.subtotal>0 THEN bi.line_total*b.total/b.subtotal ELSE 0 END"
    today = date.today()
    tomorrow = today + timedelta(days=1)
    summary_ranges = (
        (today.isoformat(), tomorrow.isoformat()),
        (today.replace(day=1).isoformat(), tomorrow.isoformat()),
        (today.replace(month=1, day=1).isoformat(), tomorrow.isoformat()),
        (filters["start"], (date.fromisoformat(filters["end"]) + timedelta(days=1)).isoformat()),
        (filters["start"], (date.fromisoformat(filters["end"]) + timedelta(days=1)).isoformat()),
        (filters["start"], (date.fromisoformat(filters["end"]) + timedelta(days=1)).isoformat()),
        (filters["start"], (date.fromisoformat(filters["end"]) + timedelta(days=1)).isoformat()),
        (filters["start"], (date.fromisoformat(filters["end"]) + timedelta(days=1)).isoformat()),
    )
    summary_params = [
        value for start, end in summary_ranges
        for value in (f"{start}T00:00:00", f"{end}T00:00:00")
    ]
    with db() as connection:
        summary = connection.execute(
            """SELECT
                COALESCE(SUM(CASE WHEN created_at>=? AND created_at<? THEN total ELSE 0 END),0) today_sales,
                COALESCE(SUM(CASE WHEN created_at>=? AND created_at<? THEN total ELSE 0 END),0) month_sales,
                COALESCE(SUM(CASE WHEN created_at>=? AND created_at<? THEN total ELSE 0 END),0) year_sales,
                COALESCE(SUM(CASE WHEN created_at>=? AND created_at<? THEN total ELSE 0 END),0) period_sales,
                COUNT(CASE WHEN created_at>=? AND created_at<? THEN 1 END) bill_count,
                COALESCE(AVG(CASE WHEN created_at>=? AND created_at<? THEN total END),0) average_bill,
                COALESCE(SUM(CASE WHEN created_at>=? AND created_at<? THEN received ELSE 0 END),0) received,
                COALESCE(SUM(CASE WHEN created_at>=? AND created_at<? THEN balance ELSE 0 END),0) balance
                FROM bills""",
            summary_params,
        ).fetchone()
        inventory = connection.execute(
            """SELECT COUNT(*) total_products,COALESCE(SUM(stock),0) available_stock,
                COALESCE(SUM(CASE WHEN stock>0 AND stock<=? THEN 1 ELSE 0 END),0) low_stock,
                COALESCE(SUM(CASE WHEN stock=0 THEN 1 ELSE 0 END),0) out_of_stock
                FROM products""",
            (connection.execute("SELECT minimum_stock FROM stock_preferences WHERE id=1").fetchone()[0],),
        ).fetchone()
        minimum_stock = connection.execute(
            "SELECT minimum_stock FROM stock_preferences WHERE id=1"
        ).fetchone()[0]
        categories = connection.execute(
            """SELECT COALESCE(NULLIF(subcategory,''),category) category
                FROM products WHERE category<>''
                UNION SELECT category FROM bill_items WHERE category<>''
                ORDER BY category"""
        ).fetchall()
        product_options = connection.execute(
            """SELECT id,name,code,size,colour FROM products
                ORDER BY name,size,colour"""
        ).fetchall()
        product_filter = ""
        product_params = []
        if filters["product_id"] is not None:
            product_filter += " AND p.id=?"
            product_params.append(filters["product_id"])
        if filters["category"]:
            product_filter += " AND COALESCE(NULLIF(p.subcategory,''),p.category)=?"
            product_params.append(filters["category"])
        low_stock = connection.execute(
            """SELECT p.id,p.name,p.code,p.category,p.size,p.colour,p.stock
                FROM products p WHERE p.stock>0 AND p.stock<=?""" + product_filter + """
                ORDER BY p.stock,p.name LIMIT 10""",
            [minimum_stock, *product_params],
        ).fetchall()
        out_of_stock = connection.execute(
            """SELECT p.id,p.name,p.code,p.category,p.size,p.colour,p.stock
                FROM products p WHERE p.stock=0""" + product_filter + """
                ORDER BY p.name LIMIT 10""",
            product_params,
        ).fetchall()
        product_total = connection.execute(
            "SELECT COUNT(*) FROM products p WHERE 1=1" + product_filter,
            product_params,
        ).fetchone()[0]
        product_offset = (filters["page"] - 1) * filters["page_size"]
        product_sales_filter, product_sales_params = report_item_conditions(
            filters, product_alias="sold_product"
        )
        analytics_item_filter, analytics_item_params = report_item_conditions(filters)
        product_query = """SELECT p.id,p.name,p.code,
            COALESCE(NULLIF(p.subcategory,''),p.category) category,p.size,p.colour,
            COALESCE(s.quantity_sold,0) quantity_sold,COALESCE(s.revenue,0) revenue
            FROM products p
            LEFT JOIN (
                SELECT bi.product_id,SUM(bi.quantity) quantity_sold,
                    SUM(""" + revenue + """) revenue
                FROM bills b JOIN bill_items bi ON bi.bill_id=b.id
                LEFT JOIN products sold_product ON sold_product.id=bi.product_id
                WHERE b.created_at>=? AND b.created_at<?""" + product_sales_filter + """
                GROUP BY bi.product_id
            ) s ON s.product_id=p.id
            WHERE 1=1""" + product_filter + """
            ORDER BY quantity_sold DESC,p.name,p.size,p.colour LIMIT ? OFFSET ?"""
        product_rows = connection.execute(
            product_query,
            [*period_params, *product_sales_params, *product_params,
             filters["page_size"], product_offset],
        ).fetchall()
        top_products = connection.execute(
            product_query.replace(
                "ORDER BY quantity_sold DESC,p.name,p.size,p.colour LIMIT ? OFFSET ?",
                "AND COALESCE(s.quantity_sold,0)>0 "
                "ORDER BY quantity_sold DESC,p.name,p.size,p.colour LIMIT 10",
            ),
            [*period_params, *product_sales_params, *product_params],
        ).fetchall()
        least_products = connection.execute(
            product_query.replace(
                "ORDER BY quantity_sold DESC,p.name,p.size,p.colour LIMIT ? OFFSET ?",
                "ORDER BY quantity_sold ASC,p.name,p.size,p.colour LIMIT 10",
            ),
            [*period_params, *product_sales_params, *product_params],
        ).fetchall()
        category_sales = connection.execute(
            """SELECT COALESCE(NULLIF(bi.category,''),p.category,'Uncategorised') category,
                SUM(""" + revenue + """) revenue,SUM(bi.quantity) quantity
                FROM bill_items bi JOIN bills b ON b.id=bi.bill_id
                LEFT JOIN products p ON p.id=bi.product_id
                WHERE b.created_at>=? AND b.created_at<?""" + analytics_item_filter + """
                GROUP BY category ORDER BY revenue DESC""",
            [*period_params, *analytics_item_params],
        ).fetchall()
        payment_methods = connection.execute(
            """SELECT COALESCE(pay.method,'Pending') method,COUNT(DISTINCT b.id) bills,
                COALESCE(SUM(pay.amount),0) amount
                FROM bills b LEFT JOIN payments pay ON pay.bill_id=b.id
                WHERE b.created_at>=? AND b.created_at<?
                GROUP BY method ORDER BY amount DESC,method""",
            period_params,
        ).fetchall()
        trend_specs = (
            ("daily", "DATE(b.created_at)"),
            ("weekly", "DATE_FORMAT(DATE_SUB(DATE(b.created_at), INTERVAL WEEKDAY(b.created_at) DAY), '%Y-%m-%d')"),
            ("monthly", "DATE_FORMAT(b.created_at, '%Y-%m')"),
            ("yearly", "YEAR(b.created_at)"),
        )
        trends = {}
        for key, bucket in trend_specs:
            trends[key] = connection.execute(
                """SELECT """ + bucket + """ bucket,SUM(""" + revenue + """) revenue
                    FROM bill_items bi JOIN bills b ON b.id=bi.bill_id
                    LEFT JOIN products p ON p.id=bi.product_id
                    WHERE b.created_at>=? AND b.created_at<?""" + analytics_item_filter + """
                    GROUP BY bucket ORDER BY bucket""",
                [*period_params, *analytics_item_params],
            ).fetchall()
        profit = connection.execute(
            """SELECT
                COALESCE(SUM(CASE WHEN COALESCE(cost.cost_quantity,0)=sales.quantity
                    THEN sales.net_revenue-cost.cost_total ELSE 0 END),0) available_profit,
                COALESCE(SUM(CASE WHEN COALESCE(cost.cost_quantity,0)=sales.quantity
                    THEN 1 ELSE 0 END),0) cost_complete_groups,
                COALESCE(SUM(CASE WHEN COALESCE(cost.cost_quantity,0)<>sales.quantity
                    THEN sales.quantity ELSE 0 END),0) missing_cost_units
                FROM (
                    SELECT bi.bill_id,bi.product_id,SUM(bi.quantity) quantity,
                        SUM(""" + revenue + """) net_revenue
                    FROM bills b JOIN bill_items bi ON bi.bill_id=b.id
                    LEFT JOIN products p ON p.id=bi.product_id
                    WHERE b.created_at>=? AND b.created_at<?""" + analytics_item_filter + """
                    GROUP BY bi.bill_id,bi.product_id
                ) sales
                LEFT JOIN (
                    SELECT bill_id,product_id,
                        SUM(CASE WHEN unit_purchase_price>0 THEN quantity ELSE 0 END) cost_quantity,
                        SUM(CASE WHEN unit_purchase_price>0
                            THEN quantity*unit_purchase_price ELSE 0 END) cost_total
                    FROM stock_movements sm JOIN bills cost_bill ON cost_bill.id=sm.bill_id
                    WHERE sm.movement_type='sale'
                        AND cost_bill.created_at>=? AND cost_bill.created_at<?
                    GROUP BY bill_id,product_id
                ) cost ON cost.bill_id=sales.bill_id AND cost.product_id=sales.product_id""",
            [*period_params, *analytics_item_params, *period_params],
        ).fetchone()
        stock_filter = ""
        stock_params = [filters["start_at"], filters["end_at"]]
        if filters["product_id"] is not None:
            stock_filter += " AND sm.product_id=?"
            stock_params.append(filters["product_id"])
        if filters["category"]:
            stock_filter += " AND COALESCE(NULLIF(p.subcategory,''),p.category)=?"
            stock_params.append(filters["category"])
        stock_movement_total = connection.execute(
            """SELECT COUNT(*) FROM stock_movements sm LEFT JOIN products p ON p.id=sm.product_id
                WHERE sm.created_at>=? AND sm.created_at<?""" + stock_filter,
            stock_params,
        ).fetchone()[0]
        movement_offset = (filters["movement_page"] - 1) * filters["movement_page_size"]
        stock_movements = connection.execute(
            """SELECT sm.id,sm.product_name,sm.product_code,sm.movement_type,sm.quantity,
                sm.previous_stock,sm.resulting_stock,sm.note,sm.supplier,sm.created_at,
                sm.unit_purchase_price
                FROM stock_movements sm LEFT JOIN products p ON p.id=sm.product_id
                WHERE sm.created_at>=? AND sm.created_at<?""" + stock_filter + """
                ORDER BY sm.id DESC LIMIT ? OFFSET ?""",
            [*stock_params, filters["movement_page_size"], movement_offset],
        ).fetchall()

    page_count = max(1, (product_total + filters["page_size"] - 1) // filters["page_size"])
    movement_page_count = max(
        1, (stock_movement_total + filters["movement_page_size"] - 1) // filters["movement_page_size"]
    )
    if filters["page"] > page_count or filters["movement_page"] > movement_page_count:
        query = {key: value for key, value in request.args.items()}
        query["page"] = min(filters["page"], page_count)
        query["movement_page"] = min(filters["movement_page"], movement_page_count)
        return redirect(url_for("reports", **query))
    return render_template(
        "reports.html",
        filters=filters,
        summary=summary,
        inventory=inventory,
        minimum_stock=minimum_stock,
        categories=[row["category"] for row in categories],
        product_options=product_options,
        low_stock=low_stock,
        out_of_stock=out_of_stock,
        products=product_rows,
        product_total=product_total,
        page_count=page_count,
        stock_movement_total=stock_movement_total,
        movement_page_count=movement_page_count,
        top_products=top_products,
        least_products=least_products,
        category_sales=category_sales,
        payment_methods=payment_methods,
        trends=trends,
        profit=profit,
        stock_movements=stock_movements,
    )


@app.route("/reports/export/<fmt>")
def export_report(fmt):
    if fmt not in {"xlsx", "pdf"}:
        abort(404)
    try:
        filters = report_filters(request.args)
    except ValueError as error:
        abort(400, description=str(error))
    count = report_details_count(filters)
    if fmt == "pdf" and count > 20000:
        abort(413, description="This PDF contains more than 20,000 line items. Narrow the date range or export to Excel.")
    with db() as connection:
        summary = connection.execute(
            """SELECT COUNT(*) bill_count,COALESCE(SUM(total),0) sales,
                COALESCE(SUM(received),0) received,COALESCE(SUM(balance),0) balance
                FROM bills WHERE created_at>=? AND created_at<?""",
            (filters["start_at"], filters["end_at"]),
        ).fetchone()
    output = report_xlsx(filters, summary, count) if fmt == "xlsx" else report_pdf(filters, summary, count)
    mime = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if fmt == "xlsx" else "application/pdf"
    )
    return send_file(
        output, as_attachment=True,
        download_name=f"hi5_sales_{filters['start']}_to_{filters['end']}.{fmt}",
        mimetype=mime,
    )

@app.after_request
def set_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options","nosniff")
    response.headers.setdefault("X-Frame-Options","DENY")
    response.headers.setdefault("Referrer-Policy","same-origin")
    if APP_ENV == "production":
        response.headers.setdefault("Strict-Transport-Security","max-age=31536000")
    return response

init_db()

if __name__ == "__main__":
    app.run(host="127.0.0.1",port=int(os.environ.get("PORT","5001")),
        debug=os.environ.get("FLASK_DEBUG","").lower()=="true" and APP_ENV!="production")
