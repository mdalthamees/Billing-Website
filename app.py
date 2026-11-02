import os, sqlite3, csv, io
import json
from datetime import datetime, date
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_file, abort
from werkzeug.utils import secure_filename
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

BASE = os.path.abspath(os.path.dirname(__file__))
INSTANCE = os.path.join(BASE, "instance")
UPLOADS = os.path.join(BASE, "static", "uploads")
os.makedirs(INSTANCE, exist_ok=True)
os.makedirs(UPLOADS, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("HI5_SECRET_KEY", "change-this-local-secret")
DB = os.path.join(INSTANCE, "hi5_pos.db")
ALLOWED = {"png", "jpg", "jpeg", "webp"}

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY CHECK(id=1), shop_name TEXT NOT NULL,
            address TEXT NOT NULL, phone TEXT DEFAULT '', logo TEXT DEFAULT ''
        );
        INSERT OR IGNORE INTO settings(id,shop_name,address,phone,logo)
        VALUES(1,'HI-5 FOOTWEAR & BOUTIQUES','Gandhi Street, R.S. Mangalam, Ramanathapuram, Tamil Nadu','','');
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            phone TEXT UNIQUE, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            code TEXT NOT NULL UNIQUE, department TEXT NOT NULL,
            category TEXT NOT NULL, subcategory TEXT DEFAULT '',
            gender TEXT DEFAULT '', age_group TEXT DEFAULT '',
            size TEXT DEFAULT '', colour TEXT DEFAULT '', brand TEXT DEFAULT '',
            purchase_price REAL NOT NULL DEFAULT 0, selling_price REAL NOT NULL DEFAULT 0,
            stock INTEGER NOT NULL DEFAULT 0, image TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bills (
            id INTEGER PRIMARY KEY AUTOINCREMENT, bill_no TEXT UNIQUE NOT NULL,
            customer_id INTEGER, customer_name TEXT DEFAULT '', customer_phone TEXT DEFAULT '',
            subtotal REAL NOT NULL, discount REAL NOT NULL DEFAULT 0,
            total REAL NOT NULL, received REAL NOT NULL DEFAULT 0,
            balance REAL NOT NULL DEFAULT 0, payment_method TEXT NOT NULL,
            payment_status TEXT NOT NULL, created_at TEXT NOT NULL,
            FOREIGN KEY(customer_id) REFERENCES customers(id)
        );
        CREATE TABLE IF NOT EXISTS bill_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, bill_id INTEGER NOT NULL,
            product_id INTEGER, product_name TEXT NOT NULL, code TEXT DEFAULT '',
            department TEXT DEFAULT '', category TEXT DEFAULT '', size TEXT DEFAULT '',
            colour TEXT DEFAULT '', quantity INTEGER NOT NULL, price REAL NOT NULL,
            line_total REAL NOT NULL,
            FOREIGN KEY(bill_id) REFERENCES bills(id) ON DELETE CASCADE,
            FOREIGN KEY(product_id) REFERENCES products(id)
        );
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, bill_id INTEGER NOT NULL,
            amount REAL NOT NULL, method TEXT NOT NULL, created_at TEXT NOT NULL,
            FOREIGN KEY(bill_id) REFERENCES bills(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS stock_preferences (
            id INTEGER PRIMARY KEY CHECK(id=1),
            minimum_stock INTEGER NOT NULL DEFAULT 5
        );
        INSERT OR IGNORE INTO stock_preferences(id,minimum_stock) VALUES(1,5);
        CREATE TABLE IF NOT EXISTS stock_movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER,
            product_name TEXT NOT NULL,
            product_code TEXT NOT NULL DEFAULT '',
            movement_type TEXT NOT NULL,
            quantity INTEGER NOT NULL CHECK(quantity>0),
            note TEXT NOT NULL DEFAULT '',
            bill_id INTEGER,
            created_at TEXT NOT NULL,
            FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE SET NULL,
            FOREIGN KEY(bill_id) REFERENCES bills(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS inventory_migrations (
            migration_key TEXT PRIMARY KEY,
            completed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            action TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT DEFAULT '',
            reference TEXT DEFAULT '',
            details_json TEXT NOT NULL
        );
        """)
        count = c.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        cleared_all = c.execute("""SELECT 1 FROM inventory_migrations
            WHERE migration_key='clear-all-completed'""").fetchone()
        if count == 0 and not cleared_all:
            samples = [
              ("Men Running Shoes","FW-MSH-01","Footwear","Men's Shoes","","Men","","9","Black","HI5",850,1299,12),
              ("Ladies Sandal","FW-WS-01","Footwear","Women's Sandals","","Women","","6","Tan","HI5",350,599,18),
              ("Kids School Shoes","FW-KS-01","Footwear","School Shoes","","Unisex","5–8","2","Black","HI5",420,699,10),
              ("Cotton Kurti","DR-KU-01","Dress","Ladies Collection","Kurtis","Women","12+","M","Maroon","",400,799,15),
              ("Printed Top","DR-TP-01","Dress","Ladies Collection","Tops","Women","12+","L","Blue","",250,499,20),
              ("Girls Frock","DR-GF-01","Dress","Girls Kids Collection","Girls Frocks","Girls","3–5","4Y","Pink","",300,649,8),
              ("Boys T-Shirt","DR-BT-01","Dress","Boys Kids Collection","Boys T-Shirts","Boys","5–8","6Y","Green","",180,399,14),
              ("Kids Combo Set","DR-KC-01","Dress","Kids Collection","Kids Sets","Unisex","1–3","2Y","Yellow","",350,699,9)
            ]
            c.executemany("""INSERT INTO products(name,code,department,category,subcategory,gender,age_group,size,colour,brand,purchase_price,selling_price,stock,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", [x+(datetime.now().isoformat(timespec="seconds"),) for x in samples])
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
                WHERE movement_type IN ('opening','stock_in','adjustment_in')),0) total_stock_quantity
            FROM products""",(minimum_stock,)).fetchone()
        chart = c.execute("""SELECT date(created_at) d, SUM(total) total FROM bills
            WHERE date(created_at)>=date('now','-6 days') GROUP BY date(created_at) ORDER BY d""").fetchall()
        dept = c.execute("SELECT department,SUM(line_total) total FROM bill_items GROUP BY department").fetchall()
        recent = c.execute("SELECT * FROM bills ORDER BY id DESC LIMIT 6").fetchall()
    return render_template("dashboard.html", today=today, month=month, products=products,
        customers=customers, stock_value=stock_value, pending=pending, low=low,
        stock_stats=stock_stats, minimum_stock=minimum_stock,
        chart=[dict(x) for x in chart], dept=[dict(x) for x in dept], recent=recent)

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
                WHERE movement_type IN ('opening','stock_in','adjustment_in')),0) total_stock_quantity
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
    try:
        quantity = int(request.form.get("quantity","0"))
        if quantity < 1:
            raise ValueError("Quantity must be a positive whole number.")
        if movement_type not in {"stock_in","stock_out"}:
            raise ValueError("Choose Stock In or Stock Out.")
        with db() as c:
            product = c.execute("SELECT * FROM products WHERE id=?",(product_id,)).fetchone()
            if not product:
                raise ValueError("Select a valid product.")
            if movement_type == "stock_out":
                cur = c.execute("UPDATE products SET stock=stock-? WHERE id=? AND stock>=?",
                    (quantity,product_id,quantity))
                if cur.rowcount != 1:
                    raise ValueError(f"Only {product['stock']} units are available for stock out.")
            else:
                c.execute("UPDATE products SET stock=stock+? WHERE id=?",(quantity,product_id))
            c.execute("""INSERT INTO stock_movements
                (product_id,product_name,product_code,movement_type,quantity,note,created_at)
                VALUES(?,?,?,?,?,?,?)""",(product["id"],product["name"],product["code"],
                movement_type,quantity,note,datetime.now().isoformat(timespec="seconds")))
            movement = c.execute("SELECT * FROM stock_movements WHERE id=?",
                (c.execute("SELECT last_insert_rowid()").fetchone()[0],)).fetchone()
            audit(c,"added","stock movement",movement["id"],product["code"],
                {"movement":dict(movement),"product":dict(product)})
        flash("Stock movement recorded.","success")
    except (ValueError, sqlite3.Error) as e:
        flash("Could not record stock movement: "+str(e),"danger")
    return redirect(url_for("stock_maintenance"))

@app.route("/stock/settings", methods=["POST"])
def stock_settings():
    try:
        minimum_stock = int(request.form.get("minimum_stock",""))
        if minimum_stock < 0:
            raise ValueError("Minimum stock cannot be negative.")
        with db() as c:
            previous = c.execute("SELECT minimum_stock FROM stock_preferences WHERE id=1").fetchone()[0]
            c.execute("UPDATE stock_preferences SET minimum_stock=? WHERE id=1",(minimum_stock,))
            audit(c,"updated","stock alert setting",1,"minimum stock",
                {"before":{"minimum_stock":previous},"after":{"minimum_stock":minimum_stock}})
        flash("Minimum stock alert updated.","success")
    except (ValueError, sqlite3.Error) as e:
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
        image_name=upload_image(request.files.get("image"),"product")
        with db() as c:
            pid=f.get("id")
            stock=int(f.get("stock") or 0)
            if stock < 0:
                raise ValueError("Stock quantity cannot be negative.")
            vals=(f["name"].strip(),f["code"].strip(),f["department"],f["category"],f.get("subcategory",""),
              f.get("gender",""),f.get("age_group",""),f.get("size",""),f.get("colour",""),f.get("brand",""),
              float(f.get("purchase_price") or 0),float(f.get("selling_price") or 0),stock)
            if pid:
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
                    movement = c.execute("SELECT * FROM stock_movements WHERE id=?",
                        (c.execute("SELECT last_insert_rowid()").fetchone()[0],)).fetchone()
                    audit(c,"added","stock movement",movement["id"],f["code"].strip(),
                        {"movement":dict(movement)})
                updated = c.execute("SELECT * FROM products WHERE id=?",(pid,)).fetchone()
                audit(c,"updated","product",pid,updated["code"],
                    {"before":before,"after":dict(updated)})
            else:
                c.execute("""INSERT INTO products(name,code,department,category,subcategory,gender,age_group,size,colour,brand,purchase_price,selling_price,stock,image,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", vals+(image_name,datetime.now().isoformat(timespec="seconds")))
                pid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
                if stock:
                    c.execute("""INSERT INTO stock_movements
                        (product_id,product_name,product_code,movement_type,quantity,note,created_at)
                        VALUES(?,?,?,'opening',?,?,?)""",(pid,f["name"].strip(),f["code"].strip(),
                        stock,"Opening stock entered during product setup",
                        datetime.now().isoformat(timespec="seconds")))
                    movement = c.execute("SELECT * FROM stock_movements WHERE id=?",
                        (c.execute("SELECT last_insert_rowid()").fetchone()[0],)).fetchone()
                    audit(c,"added","stock movement",movement["id"],f["code"].strip(),
                        {"movement":dict(movement)})
                created = c.execute("SELECT * FROM products WHERE id=?",(pid,)).fetchone()
                audit(c,"created","product",pid,created["code"],dict(created))
        flash("Product saved successfully.","success")
    except (ValueError, sqlite3.IntegrityError) as e:
        flash("Could not save product: "+str(e),"danger")
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
    except sqlite3.Error as e:
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
    except sqlite3.Error as e:
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
    except sqlite3.Error as e:
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
    except (ValueError, sqlite3.Error) as e:
        flash("Could not delete stock movement: "+str(e),"danger")
    return redirect(url_for("stock_maintenance"))

@app.route("/api/products")
def api_products():
    q=request.args.get("q","").strip()
    dept=request.args.get("department","")
    with db() as c:
        sql="SELECT * FROM products WHERE stock>0"; args=[]
        if q: sql+=" AND (name LIKE ? OR code LIKE ?)"; args += [f"%{q}%"]*2
        if dept: sql+=" AND department=?"; args.append(dept)
        rows=c.execute(sql+" ORDER BY name LIMIT 100",args).fetchall()
    return jsonify([dict(x) for x in rows])

@app.route("/billing")
def billing():
    return render_template("billing.html")

@app.route("/api/bills", methods=["POST"])
def create_bill():
    data=request.get_json(force=True)
    items=data.get("items",[])
    if not items: return jsonify(error="Cart is empty."),400
    try:
        discount=max(0,float(data.get("discount",0)))
        received=max(0,float(data.get("received",0)))
        method=data.get("payment_method","Cash")
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            subtotal=0; checked=[]
            requested_quantities={}
            for item in items:
                p=c.execute("SELECT * FROM products WHERE id=?",(int(item["id"]),)).fetchone()
                qty=int(item["quantity"])
                if not p or qty<1: raise ValueError("Invalid product or quantity.")
                requested_quantities[p["id"]]=requested_quantities.get(p["id"],0)+qty
                if p["stock"]<requested_quantities[p["id"]]: raise ValueError(f"Insufficient stock for {p['name']} ({p['size']} / {p['colour']}). Available: {p['stock']}")
                price=float(item.get("price",p["selling_price"]))
                if price<0: raise ValueError("Price cannot be negative.")
                subtotal += price*qty
                checked.append((p,qty,price))
            if discount>subtotal: raise ValueError("Discount cannot exceed subtotal.")
            total=round(subtotal-discount,2)
            if received>total: received=total
            balance=round(total-received,2)
            status="Paid" if balance==0 else ("Partially Paid" if received>0 else "Pending")
            cname=(data.get("customer_name") or "").strip()
            phone=(data.get("customer_phone") or "").strip()
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
            bill_no="HI5-"+datetime.now().strftime("%y%m%d-%H%M%S")
            bill_created_at=datetime.now().isoformat(timespec="seconds")
            cur=c.execute("""INSERT INTO bills(bill_no,customer_id,customer_name,customer_phone,subtotal,discount,total,received,balance,payment_method,payment_status,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(bill_no,cid,cname,phone,subtotal,discount,total,received,balance,method,status,bill_created_at))
            bid=cur.lastrowid
            for p,qty,price in checked:
                c.execute("""INSERT INTO bill_items(bill_id,product_id,product_name,code,department,category,size,colour,quantity,price,line_total)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(bid,p["id"],p["name"],p["code"],p["department"],p["subcategory"] or p["category"],p["size"],p["colour"],qty,price,price*qty))
                updated=c.execute("UPDATE products SET stock=stock-? WHERE id=? AND stock>=?",
                    (qty,p["id"],qty))
                if updated.rowcount != 1:
                    raise ValueError(f"Insufficient stock for {p['name']} ({p['size']} / {p['colour']}).")
                c.execute("""INSERT INTO stock_movements
                    (product_id,product_name,product_code,movement_type,quantity,note,bill_id,created_at)
                    VALUES(?,?,?,'sale',?,?,?,?)""",(p["id"],p["name"],p["code"],qty,
                    f"Sale {bill_no}",bid,datetime.now().isoformat(timespec="seconds")))
            if received>0:
                c.execute("INSERT INTO payments(bill_id,amount,method,created_at) VALUES(?,?,?,?)",(bid,received,method,datetime.now().isoformat(timespec="seconds")))
            bill_record=c.execute("SELECT * FROM bills WHERE id=?",(bid,)).fetchone()
            bill_items=c.execute("SELECT * FROM bill_items WHERE bill_id=?",(bid,)).fetchall()
            bill_payments=c.execute("SELECT * FROM payments WHERE bill_id=?",(bid,)).fetchall()
            bill_movements=c.execute("SELECT * FROM stock_movements WHERE bill_id=?",(bid,)).fetchall()
            audit(c,"created","bill",bid,bill_no,
                {"bill":dict(bill_record),"items":[dict(row) for row in bill_items],
                 "payments":[dict(row) for row in bill_payments],
                 "stock_movements":[dict(row) for row in bill_movements]})
        shop = settings()
        return jsonify(ok=True,bill_id=bid,bill_no=bill_no,receipt={
            "shopName":shop["shop_name"],"address":shop["address"],"shopPhone":shop["phone"],
            "logoUrl":url_for("static",filename="uploads/"+shop["logo"]) if shop["logo"] else "",
            "billNumber":bill_no,"date":bill_created_at,
            "customerName":cname or "Walk-in Customer","customerPhone":phone or "—",
            "subtotal":subtotal,"discount":discount,"total":total,"received":received,
            "balance":balance,"paymentMethod":method,"paymentStatus":status,
            "items":[{
                "name":p["name"],"category":p["subcategory"] or p["category"],
                "size":p["size"],"colour":p["colour"],"quantity":qty,
                "price":price,"total":round(price*qty,2)
            } for p,qty,price in checked]
        })
    except (ValueError, KeyError, sqlite3.Error) as e:
        return jsonify(error=str(e)),400

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
    return render_template("receipt.html", bill=b, items=items, payments=payments)

@app.route("/sales")
def sales():
    q=request.args.get("q",""); status=request.args.get("status",""); start=request.args.get("start",""); end=request.args.get("end","")
    dept=request.args.get("department","")
    sql="SELECT * FROM bills WHERE 1=1"; args=[]
    if q: sql+=" AND (bill_no LIKE ? OR customer_name LIKE ? OR customer_phone LIKE ?)"; args += [f"%{q}%"]*3
    if status: sql+=" AND payment_status=?"; args.append(status)
    if start: sql+=" AND date(created_at)>=?"; args.append(start)
    if end: sql+=" AND date(created_at)<=?"; args.append(end)
    if dept: sql+=" AND id IN (SELECT bill_id FROM bill_items WHERE department=?)"; args.append(dept)
    with db() as c: rows=c.execute(sql+" ORDER BY id DESC",args).fetchall()
    return render_template("sales.html", bills=rows, q=q, status=status, start=start, end=end, department=dept)

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
    else:
        try:
            with db() as c:
                cur=c.execute("INSERT INTO customers(name,phone,created_at) VALUES(?,?,?)",
                    (name,phone or None,datetime.now().isoformat(timespec="seconds")))
                customer=c.execute("SELECT * FROM customers WHERE id=?",(cur.lastrowid,)).fetchone()
                audit(c,"created","customer",customer["id"],customer["phone"] or customer["name"],
                    dict(customer))
            flash("Customer added.","success")
        except sqlite3.IntegrityError: flash("That phone number already exists.","warning")
    return redirect(url_for("customers"))

@app.route("/bill/<int:bid>/payment",methods=["POST"])
def add_payment(bid):
    amount=float(request.form.get("amount") or 0); method=request.form.get("method","Cash")
    if amount<=0: flash("Enter a payment amount greater than zero.","danger"); return redirect(url_for("receipt",bid=bid))
    with db() as c:
        b=c.execute("SELECT * FROM bills WHERE id=?",(bid,)).fetchone()
        if not b: abort(404)
        amount=min(amount,b["balance"])
        received=round(b["received"]+amount,2); balance=round(b["total"]-received,2)
        status="Paid" if balance==0 else "Partially Paid"
        c.execute("UPDATE bills SET received=?,balance=?,payment_status=? WHERE id=?",(received,balance,status,bid))
        cur=c.execute("INSERT INTO payments(bill_id,amount,method,created_at) VALUES(?,?,?,?)",
            (bid,amount,method,datetime.now().isoformat(timespec="seconds")))
        payment=c.execute("SELECT * FROM payments WHERE id=?",(cur.lastrowid,)).fetchone()
        updated_bill=c.execute("SELECT * FROM bills WHERE id=?",(bid,)).fetchone()
        audit(c,"created","payment",payment["id"],b["bill_no"],
            {"payment":dict(payment),"bill_after":dict(updated_bill)})
    flash("Payment recorded.","success")
    return redirect(url_for("receipt",bid=bid))

@app.route("/settings",methods=["GET","POST"])
def shop_settings():
    if request.method=="POST":
        f=request.form
        try:
            logo=upload_image(request.files.get("logo"),"logo")
            with db() as c:
                before=dict(c.execute("SELECT * FROM settings WHERE id=1").fetchone())
                if logo: c.execute("UPDATE settings SET shop_name=?,address=?,phone=?,logo=? WHERE id=1",(f["shop_name"],f["address"],f.get("phone",""),logo))
                else: c.execute("UPDATE settings SET shop_name=?,address=?,phone=? WHERE id=1",(f["shop_name"],f["address"],f.get("phone","")))
                after=dict(c.execute("SELECT * FROM settings WHERE id=1").fetchone())
                audit(c,"updated","shop settings",1,after["shop_name"],
                    {"before":before,"after":after})
            flash("Shop settings updated.","success")
        except ValueError as e: flash(str(e),"danger")
        return redirect(url_for("shop_settings"))
    return render_template("settings.html")

@app.route("/settings/reset",methods=["POST"])
def reset_shop_settings():
    with db() as c:
        before=dict(c.execute("SELECT * FROM settings WHERE id=1").fetchone())
        c.execute("""UPDATE settings SET shop_name=?,address=?,phone='',logo='' WHERE id=1""",
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
    except sqlite3.Error as e:
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

@app.route("/export/<kind>/<fmt>")
def export(kind,fmt):
    if kind not in {"sales","products","customers","pending"} or fmt not in {"csv","xlsx"}: abort(404)
    rows=export_rows(kind)
    columns=list(rows[0].keys()) if rows else ["No records"]
    if fmt=="csv":
        s=io.StringIO(); w=csv.DictWriter(s,fieldnames=columns); w.writeheader()
        for r in rows: w.writerow(r)
        data=io.BytesIO(s.getvalue().encode("utf-8-sig")); mime="text/csv"; ext="csv"
    else:
        wb=Workbook(); ws=wb.active; ws.title=kind.title()
        ws.append(columns)
        for cell in ws[1]:
            cell.font=Font(bold=True,color="FFFFFF"); cell.fill=PatternFill("solid",fgColor="B5122B")
        for r in rows: ws.append([r.get(k) for k in columns])
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

@app.route("/reports")
def reports():
    with db() as c:
        categories=[dict(x) for x in c.execute("SELECT category,SUM(line_total) total FROM bill_items GROUP BY category ORDER BY total DESC")]
        tops=[dict(x) for x in c.execute("SELECT product_name,SUM(quantity) qty,SUM(line_total) total FROM bill_items GROUP BY product_name ORDER BY qty DESC LIMIT 8")]
        monthly=[dict(x) for x in c.execute("SELECT strftime('%Y-%m',created_at) month,SUM(total) total FROM bills GROUP BY month ORDER BY month DESC LIMIT 12")]
    return render_template("reports.html",categories=categories,tops=tops,monthly=monthly)

init_db()

if __name__ == "__main__":
    app.run(host="127.0.0.1",port=5000,debug=True)
