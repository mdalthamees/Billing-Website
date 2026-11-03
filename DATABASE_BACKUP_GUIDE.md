# Database backup guide

The application is configured for MySQL. The backup command uses the same environment-based connection settings as the application and does not import `app.py` or run application schema initialization.

## What is backed up

`daily_backup.py` writes a compressed, UTF-8 JSON Lines snapshot to the local `backups\` directory. Each backup includes every base table in the configured database. The snapshot uses a consistent InnoDB read transaction, includes table/column metadata and row counts, and is verified with SHA-256 metadata recorded in `backup_history.jsonl`.

The backup directory is outside Flask's static files and is excluded from Git. The admin-only page at `/admin/backups` can create a manual backup, show the local history, and download a generated backup. Do not copy the history file without its matching backup files.

By default, files are stored in the project's `backups\` folder. Set `BACKUP_DIRECTORY` in `.env` to an absolute, access-controlled local path if the project directory is cloud-synced; the scheduled command and web app use the same setting.

## Create and verify a first backup

From the project directory in PowerShell, with the application's `.env` configured:

```powershell
python .\daily_backup.py
Get-ChildItem .\backups
```

The command prints the generated filename and size on success and exits nonzero on failure. Confirm the file exists and the admin page shows a successful record. Keep a second, encrypted copy on a separate device or protected storage. A same-computer backup does not protect against disk loss, ransomware, or theft.

Manual backup:

1. Sign in as administrator.
2. Open **Database Backups** in the sidebar, or visit `/admin/backups`.
3. Select **Create backup now**.
4. Confirm the success notice and record in Backup history.

## Schedule daily backups on Windows

Run this once as the Windows user that has read access to `.env`, write access to `backups\`, and network access to MySQL:

```powershell
powershell -ExecutionPolicy Bypass -File .\register_daily_backup.ps1
```

The task is named **HI5 POS Daily MySQL Backup** and runs daily at 2:00 AM, using the current user's restricted task principal. Confirm it exists and run a test invocation:

```powershell
Get-ScheduledTask -TaskName "HI5 POS Daily MySQL Backup"
Start-ScheduledTask -TaskName "HI5 POS Daily MySQL Backup"
Start-Sleep -Seconds 10
Get-ScheduledTaskInfo -TaskName "HI5 POS Daily MySQL Backup"
```

Check `backups\backup_history.jsonl` and the admin page for a new successful record. Task Scheduler history may need to be enabled to inspect execution details. The backup manager retains at most 30 verified files and never removes the last valid backup. Retention applies to the local directory; move offline copies separately.

If the database is remote, set `DB_SSL_CA` to a trusted CA file. Remote MySQL connections fail closed unless certificate and hostname verification are configured. Never put passwords in the task arguments or scripts; `daily_backup.py` loads the ignored `.env` file.

## File access and monitoring

Keep the backup directory readable/writable only by the application operator and administrators. Review inherited Windows ACLs on the project and backup directory; use an encrypted volume or encrypted offline media for copies. The web application serves only individually named backup downloads after authentication. It does not expose the directory listing.

Review the Backup history after each scheduled run. A failure is recorded when the backup process can write to the history directory; connection failures are also written when possible. Check local application/task logs for the exception type and repair the database, CA, or filesystem configuration before assuming a backup succeeded.

## Dependencies and commands

No separate database dump executable is required. Existing Flask, MySQL Connector/Python, and python-dotenv dependencies are reused.

```powershell
python .\app.py
```

The existing application port remains **5001**. See [DATABASE_RESTORE_GUIDE.md](./DATABASE_RESTORE_GUIDE.md) before performing any restore.
