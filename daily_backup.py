import logging
import os
from pathlib import Path

import mysql.connector
from dotenv import load_dotenv

from backup_manager import BackupError, create_backup, record_backup_failure
from database import configure_mysql, mysql_pool


BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")


def main():
    database = os.environ.get("DB_NAME", "").strip()
    user = os.environ.get("DB_USER", "").strip()
    if not database or not user:
        raise RuntimeError("DB_NAME and DB_USER must be configured in the environment.")
    config = {
        "DB_HOST": os.environ.get("DB_HOST", "localhost"),
        "DB_PORT": int(os.environ.get("DB_PORT", "3306")),
        "DB_NAME": database,
        "DB_USER": user,
        "DB_PASSWORD": os.environ.get("DB_PASSWORD", ""),
        "DB_SSL_CA": os.environ.get("DB_SSL_CA", ""),
        "DB_CONNECT_TIMEOUT": int(os.environ.get("DB_CONNECT_TIMEOUT", "5")),
        "MYSQL_POOL_SIZE": 1,
    }
    directory = Path(os.environ.get("BACKUP_DIRECTORY", BASE / "backups"))
    configure_mysql(config)
    connection = None
    try:
        connection = mysql_pool().get_connection()
        record = create_backup(connection, database, directory)
    except (BackupError, mysql.connector.Error, OSError, RuntimeError, ValueError) as error:
        logging.error("Scheduled database backup failed (%s).", type(error).__name__)
        if connection is None:
            try:
                record_backup_failure(directory, database or "unconfigured", error)
            except OSError:
                logging.error("Could not write the backup failure history (filesystem error).")
        return 1
    finally:
        if connection is not None:
            connection.close()
    print(f"Backup succeeded: {record['filename']} ({record['size_bytes']:,} bytes).")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
