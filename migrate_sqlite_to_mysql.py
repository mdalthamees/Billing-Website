import argparse
import hashlib
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path

import mysql.connector
from dotenv import load_dotenv

from database import MYSQL_TABLE_DDL, TABLE_ORDER, ensure_mysql_constraints


ROOT = Path(__file__).resolve().parent
MIGRATION_ID = "sqlite-import-v1"
BACKUP_DIR = ROOT / ".migration-backups"


def sqlite_backup(source_path):
    if not source_path.is_file():
        raise FileNotFoundError(f"SQLite source database not found: {source_path}")
    BACKUP_DIR.mkdir(exist_ok=True)
    backup_path = BACKUP_DIR / (
        "hi5_pos_before_mysql_"
        + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        + ".db"
    )
    source = sqlite3.connect(str(source_path), timeout=30)
    backup = sqlite3.connect(str(backup_path))
    try:
        source.backup(backup)
    finally:
        backup.close()
        source.close()
    with closing(
        sqlite3.connect(f"file:{backup_path.as_posix()}?mode=ro", uri=True)
    ) as check:
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"SQLite backup integrity check failed: {integrity}")
    return backup_path


def inspect_sqlite(connection):
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    missing = sorted(set(TABLE_ORDER) - set(tables))
    if missing:
        raise RuntimeError("SQLite source is missing required tables: " + ", ".join(missing))

    counts = {}
    source_columns = {}
    for table in TABLE_ORDER:
        escaped = table.replace('"', '""')
        counts[table] = connection.execute(
            f'SELECT COUNT(*) FROM "{escaped}"'
        ).fetchone()[0]
        column_info = [
            tuple(row)
            for row in connection.execute(f'PRAGMA table_info("{escaped}")')
        ]
        source_columns[table] = [row[1] for row in column_info]
        index_info = list(connection.execute(f'PRAGMA index_list("{escaped}")'))
        indexes = [
            {
                "name": index[1],
                "unique": bool(index[2]),
                "columns": [
                    column[2]
                    for column in connection.execute(
                        f'PRAGMA index_info("{index[1].replace(chr(34), chr(34) * 2)}")'
                    )
                ],
            }
            for index in index_info
        ]
        foreign_keys = [
            {
                "table": row[2],
                "from": row[3],
                "to": row[4],
                "on_update": row[5],
                "on_delete": row[6],
            }
            for row in connection.execute(f'PRAGMA foreign_key_list("{escaped}")')
        ]
        create_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()[0]
        print(
            f"SQLite table={table} rows={counts[table]} "
            f"columns={json.dumps(column_info, ensure_ascii=False)} "
            f"indexes={json.dumps(indexes, ensure_ascii=False)} "
            f"foreign_keys={json.dumps(foreign_keys, ensure_ascii=False)} "
            f"constraints={create_sql}"
        )
    violations = list(connection.execute("PRAGMA foreign_key_check"))
    if violations:
        raise RuntimeError(
            "SQLite foreign-key violations detected; no records were imported."
        )
    return tables, counts, source_columns


def source_fingerprint(connection, source_columns):
    digest = hashlib.sha256()
    for table in TABLE_ORDER:
        columns = source_columns[table]
        escaped_table = table.replace('"', '""')
        escaped_columns = ",".join('"' + name.replace('"', '""') + '"' for name in columns)
        digest.update(table.encode("utf-8"))
        for row in connection.execute(
            f'SELECT {escaped_columns} FROM "{escaped_table}" ORDER BY rowid'
        ):
            digest.update(
                json.dumps(
                    list(row),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            )
            digest.update(b"\n")
    return digest.hexdigest()


def mysql_connection():
    config = {
        "host": os.environ.get("DB_HOST", "localhost"),
        "port": int(os.environ.get("DB_PORT", "3306")),
        "database": os.environ.get("DB_NAME", ""),
        "user": os.environ.get("DB_USER", ""),
        "password": os.environ.get("DB_PASSWORD", ""),
        "charset": "utf8mb4",
        "use_unicode": True,
        "autocommit": False,
        "connection_timeout": int(os.environ.get("DB_CONNECT_TIMEOUT", "5")),
    }
    if not config["database"] or not config["user"] or not config["password"]:
        raise RuntimeError("Set DB_NAME, DB_USER, and DB_PASSWORD in the root .env file.")
    if config["password"] in {"YOUR_MYSQL_PASSWORD", "your_mysql_password"}:
        raise RuntimeError("Replace the DB_PASSWORD placeholder in the root .env file.")
    return mysql.connector.connect(**config)


def mysql_table_columns(cursor, table):
    cursor.execute(f"SHOW COLUMNS FROM `{table}`")
    return [row[0] for row in cursor.fetchall()]


def target_counts(cursor):
    counts = {}
    for table in TABLE_ORDER:
        cursor.execute(f"SELECT COUNT(*) FROM `{table}`")
        counts[table] = cursor.fetchone()[0]
    return counts


def normalized_value(value):
    if isinstance(value, float):
        return round(value, 2)
    if hasattr(value, "as_tuple"):
        return round(float(value), 2)
    return value


def verify_table_contents(source, cursor, table, columns):
    primary_key = {
        "inventory_migrations": "migration_key",
        "bill_requests": "idempotency_key",
    }.get(table, "id")
    quoted_columns = ",".join(f"`{column}`" for column in columns)
    cursor.execute(
        f"SELECT {quoted_columns} FROM `{table}` ORDER BY `{primary_key}`"
    )
    source_columns = ",".join(
        '"' + column.replace('"', '""') + '"' for column in columns
    )
    source_rows = source.execute(
        f'SELECT {source_columns} FROM "{table}" ORDER BY "{primary_key}"'
    )
    while True:
        expected = source_rows.fetchmany(500)
        if not expected:
            break
        actual = cursor.fetchmany(500)
        if len(actual) != len(expected):
            return False
        if any(
            tuple(normalized_value(value) for value in source_row)
            != tuple(normalized_value(value) for value in target_row)
            for source_row, target_row in zip(expected, actual)
        ):
            return False
    return not cursor.fetchmany(1)


def import_snapshot(backup_path):
    with closing(
        sqlite3.connect(f"file:{backup_path.as_posix()}?mode=ro", uri=True)
    ) as source:
        source.row_factory = sqlite3.Row
        _, source_counts, source_columns = inspect_sqlite(source)
        fingerprint = source_fingerprint(source, source_columns)

        target = mysql_connection()
        try:
            cursor = target.cursor()
            try:
                for statement in MYSQL_TABLE_DDL:
                    cursor.execute(statement)
                ensure_mysql_constraints(cursor)
                target.commit()

                for table in TABLE_ORDER:
                    target_columns = mysql_table_columns(cursor, table)
                    if target_columns != source_columns[table]:
                        raise RuntimeError(
                            f"MySQL table {table} has an incompatible column layout; "
                            "no records were imported."
                        )

                cursor.execute(
                    "SELECT source_fingerprint FROM hi5_migration_history "
                    "WHERE migration_id=%s",
                    (MIGRATION_ID,),
                )
                previous = cursor.fetchone()
                if previous:
                    if previous[0] != fingerprint:
                        raise RuntimeError(
                            "A SQLite import was already recorded, but the source "
                            "database has changed. No rows were modified."
                        )
                    actual_counts = target_counts(cursor)
                    print("Migration already completed for this SQLite snapshot.")
                    print("MySQL row counts:", json.dumps(actual_counts, sort_keys=True))
                    return actual_counts

                existing_counts = target_counts(cursor)
                populated = {
                    table: count for table, count in existing_counts.items() if count
                }
                if populated:
                    raise RuntimeError(
                        "Target MySQL tables already contain records; refusing to "
                        "merge or overwrite them: " + json.dumps(populated, sort_keys=True)
                    )

                target.rollback()
                cursor.close()
                target.start_transaction()
                cursor = target.cursor()
                for table in TABLE_ORDER:
                    columns = source_columns[table]
                    quoted_columns = ",".join(f"`{column}`" for column in columns)
                    placeholders = ",".join(["%s"] * len(columns))
                    insert = (
                        f"INSERT INTO `{table}` ({quoted_columns}) "
                        f"VALUES ({placeholders})"
                    )
                    escaped_table = table.replace('"', '""')
                    selected_columns = ",".join(
                        '"' + name.replace('"', '""') + '"' for name in columns
                    )
                    rows = source.execute(
                        f'SELECT {selected_columns} '
                        f'FROM "{escaped_table}" ORDER BY rowid'
                    )
                    while True:
                        batch = rows.fetchmany(500)
                        if not batch:
                            break
                        cursor.executemany(insert, [tuple(row) for row in batch])

                cursor.execute(
                    "INSERT INTO hi5_migration_history "
                    "(migration_id,source_fingerprint,row_counts_json,completed_at) "
                    "VALUES (%s,%s,%s,%s)",
                    (
                        MIGRATION_ID,
                        fingerprint,
                        json.dumps(source_counts, sort_keys=True),
                        datetime.now().isoformat(timespec="seconds"),
                    ),
                )
                actual_counts = target_counts(cursor)
                mismatched = {
                    table: {"sqlite": source_counts[table], "mysql": actual_counts[table]}
                    for table in TABLE_ORDER
                    if source_counts[table] != actual_counts[table]
                }
                different_contents = [
                    table
                    for table in TABLE_ORDER
                    if not verify_table_contents(
                        source, cursor, table, source_columns[table]
                    )
                ]
                if mismatched:
                    raise RuntimeError(
                        "Post-migration row counts do not match: "
                        + json.dumps(mismatched, sort_keys=True)
                    )
                if different_contents:
                    raise RuntimeError(
                        "Post-migration record verification failed for: "
                        + ", ".join(different_contents)
                    )
                target.commit()
                print("Migration committed and row counts verified.")
                print("SQLite row counts:", json.dumps(source_counts, sort_keys=True))
                print("MySQL row counts:", json.dumps(actual_counts, sort_keys=True))
                return actual_counts
            except Exception:
                target.rollback()
                raise
            finally:
                cursor.close()
        finally:
            target.close()


def main():
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(
        description="Back up and safely import the HI-5 SQLite database to MySQL."
    )
    parser.add_argument(
        "--sqlite",
        default=str(ROOT / "instance" / "hi5_pos.db"),
        help="SQLite database path (default: instance/hi5_pos.db)",
    )
    args = parser.parse_args()
    source_path = Path(args.sqlite).resolve()
    backup_path = sqlite_backup(source_path)
    print(
        "SQLite backup verified:",
        str(backup_path.relative_to(ROOT)),
        f"({backup_path.stat().st_size} bytes)",
    )
    with closing(
        sqlite3.connect(f"file:{backup_path.as_posix()}?mode=ro", uri=True)
    ) as source:
        integrity = source.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError("SQLite backup failed integrity verification.")
    import_snapshot(backup_path)


if __name__ == "__main__":
    try:
        main()
    except (mysql.connector.Error, OSError, RuntimeError, sqlite3.Error) as error:
        print(
            "SQLite-to-MySQL migration stopped safely:",
            type(error).__name__,
            "errno=" + str(getattr(error, "errno", "n/a")),
            file=sys.stderr,
        )
        raise SystemExit(1)
