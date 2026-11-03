import gzip
import hashlib
import io
import json
import os
import re
import tempfile
import threading
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path


BACKUP_FORMAT_VERSION = 1
BACKUP_FILENAME = re.compile(r"^hi5_backup_\d{8}_\d{6}_\d{6}_[a-f0-9]{8}\.hi5bak$")
IDENTIFIER = re.compile(r"^[A-Za-z0-9_$]+$")
HISTORY_FILENAME = "backup_history.jsonl"
_history_lock = threading.Lock()


class BackupError(Exception):
    pass


def _json_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        raise BackupError("Binary database values are not supported by this backup format.")
    raise TypeError(f"Unsupported database value type: {type(value).__name__}")


def _append_history(directory, record):
    path = Path(directory) / HISTORY_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    with _history_lock:
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, line)
        finally:
            os.close(descriptor)


def _read_history(directory):
    path = Path(directory) / HISTORY_FILENAME
    if not path.exists():
        return []
    records = []
    with path.open("r", encoding="utf-8") as history:
        for line in history:
            if line.strip():
                record = json.loads(line)
                if isinstance(record, dict):
                    records.append(record)
    return records


def _table_metadata(connection, database):
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            """SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES
               WHERE TABLE_SCHEMA=%s AND TABLE_TYPE='BASE TABLE'
               ORDER BY TABLE_NAME""",
            (database,),
        )
        tables = cursor.fetchall()
        if not tables:
            raise BackupError("The configured database has no base tables.")
        result = []
        for table in tables:
            name = table["TABLE_NAME"]
            if not IDENTIFIER.fullmatch(name):
                raise BackupError("A database table name is not supported by the backup format.")
            if table["ENGINE"] != "InnoDB":
                raise BackupError(f"Table {name} is not InnoDB; a consistent backup cannot be guaranteed.")
            cursor.execute(
                """SELECT COLUMN_NAME FROM information_schema.COLUMNS
                   WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION""",
                (database, name),
            )
            columns = [row["COLUMN_NAME"] for row in cursor.fetchall()]
            if not columns or any(not IDENTIFIER.fullmatch(column) for column in columns):
                raise BackupError(f"Table {name} has an unsupported column definition.")
            cursor.execute(f"SELECT COUNT(*) AS row_count FROM `{name}`")
            result.append({"name": name, "columns": columns, "row_count": cursor.fetchone()["row_count"]})
        return result
    finally:
        cursor.close()


def _write_backup(connection, database, target, created_at, tables):
    connection.start_transaction(isolation_level="REPEATABLE READ", readonly=True, consistent_snapshot=True)
    cursor = connection.cursor(dictionary=True)
    try:
        tables = _table_metadata(connection, database)
        with target.open("wb") as raw_file:
            with gzip.GzipFile(fileobj=raw_file, mode="wb", mtime=0) as compressed:
                with io.TextIOWrapper(compressed, encoding="utf-8", newline="\n") as output:
                    output.write(json.dumps({
                        "kind": "header",
                        "data": {
                            "format_version": BACKUP_FORMAT_VERSION,
                            "database": database,
                            "created_at": created_at,
                            "tables": tables,
                        },
                    }, ensure_ascii=False, separators=(",", ":"), default=_json_value) + "\n")
                    for table in tables:
                        name = table["name"]
                        cursor.execute(f"SELECT * FROM `{name}`")
                        for row in cursor:
                            output.write(json.dumps(
                                {"kind": "row", "table": name, "data": row},
                                ensure_ascii=False,
                                separators=(",", ":"),
                                default=_json_value,
                            ) + "\n")
        connection.commit()
        return tables
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()


def _file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _prune_backups(directory, keep):
    directory = Path(directory)
    candidates = sorted(
        (path for path in directory.iterdir() if path.is_file() and BACKUP_FILENAME.fullmatch(path.name)),
        key=lambda path: path.name,
        reverse=True,
    )
    valid = []
    history = _read_history(directory)
    digests = {
        record.get("filename"): record.get("sha256")
        for record in history
        if record.get("status") == "success"
    }
    for path in candidates:
        expected_digest = digests.get(path.name)
        if expected_digest and _file_digest(path) == expected_digest:
            valid.append(path)
    for path in candidates:
        if path not in valid or valid.index(path) >= keep:
            if len(valid) <= 1 and path in valid:
                continue
            path.unlink(missing_ok=True)


def create_backup(connection, database, directory, keep=30):
    directory = Path(directory)
    created_at = datetime.now().astimezone().isoformat(timespec="seconds")
    filename = datetime.now().strftime("hi5_backup_%Y%m%d_%H%M%S_%f_") + os.urandom(4).hex() + ".hi5bak"
    target = directory / filename
    temporary = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(directory, 0o700)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".hi5_backup_", suffix=".tmp", dir=directory)
        os.close(descriptor)
        temporary = Path(temporary_name)
        tables = _write_backup(connection, database, temporary, created_at, [])
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        digest = _file_digest(target)
        record = {
            "created_at": created_at,
            "filename": filename,
            "size_bytes": target.stat().st_size,
            "status": "success",
            "database": database,
            "sha256": digest,
            "table_count": len(tables),
            "row_count": sum(table["row_count"] for table in tables),
        }
        _append_history(directory, record)
        _prune_backups(directory, max(1, int(keep)))
        return record
    except Exception as error:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        failure = {
            "created_at": created_at,
            "filename": filename,
            "size_bytes": target.stat().st_size if target.exists() else 0,
            "status": "failure",
            "database": database,
            "message": f"Backup operation failed ({type(error).__name__}).",
        }
        try:
            _append_history(directory, failure)
        except OSError:
            pass
        raise


def record_backup_failure(directory, database, error):
    created_at = datetime.now().astimezone().isoformat(timespec="seconds")
    _append_history(directory, {
        "created_at": created_at,
        "filename": "",
        "size_bytes": 0,
        "status": "failure",
        "database": database,
        "message": f"Backup operation failed ({type(error).__name__}).",
    })


def _resolve_backup(directory, filename):
    if not isinstance(filename, str) or not BACKUP_FILENAME.fullmatch(filename):
        raise BackupError("Select a valid backup file.")
    root = Path(directory).resolve()
    path = (root / filename).resolve()
    if path.parent != root or not path.is_file():
        raise BackupError("The selected backup file does not exist.")
    return path


def _verified_header(path, directory, database):
    records = _read_history(directory)
    expected = next(
        (record.get("sha256") for record in reversed(records)
         if record.get("filename") == path.name and record.get("status") == "success"),
        None,
    )
    if not expected or _file_digest(path) != expected:
        raise BackupError("The selected backup is not recorded as valid or has changed.")

    counts = {}
    with gzip.open(path, "rt", encoding="utf-8") as source:
        first = source.readline()
        try:
            header_record = json.loads(first)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise BackupError("The backup file is damaged.") from error
        header = header_record.get("data") if isinstance(header_record, dict) else None
        if not isinstance(header, dict) or header_record.get("kind") != "header":
            raise BackupError("The backup file has an invalid header.")
        if header.get("format_version") != BACKUP_FORMAT_VERSION:
            raise BackupError("This backup format version is not supported.")
        if header.get("database") != database:
            raise BackupError("This backup belongs to a different configured database.")
        tables = header.get("tables")
        if not isinstance(tables, list) or not tables:
            raise BackupError("The backup does not contain database tables.")
        expected_tables = {}
        for table in tables:
            if (not isinstance(table, dict) or not IDENTIFIER.fullmatch(str(table.get("name", "")))
                    or not isinstance(table.get("columns"), list)
                    or not table["columns"]
                    or any(not IDENTIFIER.fullmatch(str(column)) for column in table["columns"])):
                raise BackupError("The backup contains an invalid table definition.")
            expected_tables[table["name"]] = table
            counts[table["name"]] = 0
        for line in source:
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise BackupError("The backup contains damaged data.") from error
            if not isinstance(record, dict) or record.get("kind") != "row":
                raise BackupError("The backup contains an invalid record.")
            table_name = record.get("table")
            row = record.get("data")
            table = expected_tables.get(table_name)
            if (table is None or not isinstance(row, dict)
                    or set(row) != set(table["columns"])):
                raise BackupError("The backup contains an invalid row.")
            counts[table_name] += 1
        for table in tables:
            if counts[table["name"]] != table.get("row_count"):
                raise BackupError(f"Backup row count does not match for table {table['name']}.")
    return header


def validate_backup(connection, database, directory, filename):
    path = _resolve_backup(directory, filename)
    header = _verified_header(path, directory, database)
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            """SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES
               WHERE TABLE_SCHEMA=%s AND TABLE_TYPE='BASE TABLE' ORDER BY TABLE_NAME""",
            (database,),
        )
        live_tables = cursor.fetchall()
        if sorted(table["TABLE_NAME"] for table in live_tables) != sorted(
            table["name"] for table in header["tables"]
        ):
            raise BackupError("Database schema differs from this backup; restore was not started.")
        for live in live_tables:
            if live["ENGINE"] != "InnoDB":
                raise BackupError(f"Table {live['TABLE_NAME']} is not InnoDB; restore cannot be guaranteed.")
        for table in header["tables"]:
            cursor.execute(
                """SELECT COLUMN_NAME FROM information_schema.COLUMNS
                   WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION""",
                (database, table["name"]),
            )
            if [row["COLUMN_NAME"] for row in cursor.fetchall()] != table["columns"]:
                raise BackupError(f"Database schema differs for table {table['name']}; restore was not started.")
    finally:
        cursor.close()
    return path, header


def restore_backup(connection, database, directory, filename):
    path, header = validate_backup(connection, database, directory, filename)
    tables = header["tables"]
    connection.commit()
    cursor = connection.cursor()
    try:
        cursor.execute("SET FOREIGN_KEY_CHECKS=0")
        connection.start_transaction()
        for table in reversed(tables):
            cursor.execute(f"DELETE FROM `{table['name']}`")
        table_by_name = {table["name"]: table for table in tables}
        with gzip.open(path, "rt", encoding="utf-8") as source:
            source.readline()
            insert_cache = {}
            batches = {table["name"]: [] for table in tables}
            for line in source:
                record = json.loads(line)
                name = record["table"]
                table = table_by_name[name]
                columns = table["columns"]
                statement = insert_cache.get(name)
                if statement is None:
                    quoted_columns = ",".join(f"`{column}`" for column in columns)
                    placeholders = ",".join(["%s"] * len(columns))
                    statement = f"INSERT INTO `{name}` ({quoted_columns}) VALUES ({placeholders})"
                    insert_cache[name] = statement
                batches[name].append(tuple(record["data"][column] for column in columns))
                if len(batches[name]) >= 500:
                    cursor.executemany(statement, batches[name])
                    batches[name].clear()
            for name, rows in batches.items():
                if rows:
                    cursor.executemany(insert_cache[name], rows)
        cursor.execute("SET FOREIGN_KEY_CHECKS=1")
        connection.commit()
        return {
            "filename": filename,
            "table_count": len(tables),
            "row_count": sum(table["row_count"] for table in tables),
        }
    except Exception:
        connection.rollback()
        try:
            cursor.execute("SET FOREIGN_KEY_CHECKS=1")
        except Exception:
            pass
        raise
    finally:
        cursor.close()


def list_backup_history(directory):
    records = _read_history(directory)
    available = {}
    for record in records:
        filename = record.get("filename", "")
        if record.get("status") == "success" and BACKUP_FILENAME.fullmatch(filename):
            path = Path(directory).resolve() / filename
            if not path.is_file():
                continue
            record["size_bytes"] = path.stat().st_size
            available[filename] = record
    ordered = []
    seen = set()
    for record in reversed(records):
        filename = record.get("filename")
        if filename in available and filename not in seen:
            ordered.append(available[filename])
            seen.add(filename)
        elif record.get("status") == "failure":
            ordered.append(record)
    return ordered


def backup_path(directory, filename):
    path = _resolve_backup(directory, filename)
    expected = next(
        (record.get("sha256") for record in reversed(_read_history(directory))
         if record.get("filename") == filename and record.get("status") == "success"),
        None,
    )
    if not expected or _file_digest(path) != expected:
        raise BackupError("The selected backup is not recorded as valid or has changed.")
    return path
