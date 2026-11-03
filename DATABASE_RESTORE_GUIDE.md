# Database restore guide

Restore replaces rows in the configured MySQL database. Use this only when authorized and after confirming the environment, database, and selected backup. Never test restore against the live business database.

## Preconditions

1. Stop other application activity that could create bills or update inventory. Prevent the scheduled backup from running during recovery.
2. Verify `.env` points to the intended recovery database and inspect the database host/name without printing passwords. For a test, configure a separate disposable MySQL database and run the test suite's marked-database safety check.
3. Ensure a verified backup for that same database exists. Restore rejects a different database, changed/corrupt file, unsupported format, changed schema, or non-InnoDB table.
4. Ensure the backup folder is readable and the operator can create a safety backup.

The web restore does not accept uploads and never executes SQL text. It validates the locally generated file, automatically creates a fresh safety backup, then restores using a MySQL transaction. It does not drop or recreate the database. If any row operation fails, the transaction is rolled back. Foreign-key checks are restored before the connection is returned to the pool.

## Admin restore flow

1. Sign in as administrator and open **Database Backups** at `/admin/backups`.
2. Confirm the selected backup's timestamp, database, filename, and size.
3. In that backup's Restore form, type exactly `RESTORE` and select **Restore**.
4. Wait for the success message, which names both the restored file and the safety backup.
5. Verify product counts, stock, bills, bill items, payments, customers, and application behavior. Check the latest restore audit entry and retain the safety backup until verification is complete.

If validation or safety-backup creation fails, the restore is not started. If the browser loses its connection during a restore, do not retry blindly; inspect application logs, database state, and backup history first.

## Recovery after a failed restore

- If restore reports failure, the InnoDB transaction is rolled back. Verify the current application data before continuing.
- If the application or database connection fails during recovery, keep the application offline, preserve the original and safety backup files, and restore the safety backup through the same validated admin flow after the database is available.
- If schema mismatch is reported, deploy the matching application/schema version to a disposable database first. Do not edit the backup or bypass the validation.
- For a production incident, involve the database administrator and preserve logs and backups. Never use an arbitrary SQL file as a recovery shortcut.

## Safe restore testing

The automated restore test uses only the explicitly marked MySQL test database (`hi5_billing_test_adfe86e7`) configured by `tests/test_pos.py`; it does not connect to the business database. For any additional manual restore test, provision a fresh disposable database, configure `.env` to that database, and confirm its marker/identity before running the restore. Then verify representative records and restart the app against that test database.

Do not run a restore test while `.env` points at the live business database. Back up important data to a separate encrypted/offline location before relying on this local recovery process.
