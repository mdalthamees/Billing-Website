# Security audit

## Scope and result

The application uses Flask, Werkzeug password hashes, and the configured MySQL database. The audit covered login and session handling, CSRF, route access, MySQL connection setup, and the new local backup/restore paths. Business records are not included in this document.

| Finding | Severity | Result |
|---|---|---|
| Remote MySQL connections did not verify the server certificate or hostname. | Medium | Fixed: remote MySQL requires `DB_SSL_CA` and Connector/Python certificate and identity verification. Local loopback connections remain local. |
| Login had no throttling against repeated password guesses. | Medium | Fixed: five failures per client address in a 15-minute window trigger a 15-minute lockout; the address is stored as a keyed digest. |
| Production configuration could fall back to a development session cookie, short secret, or root/empty-password database account. | High | Fixed at startup: production requires a 32-character secret, a configured password hash, and a dedicated non-root MySQL account with a password and database name. Production cookies are Secure, HttpOnly, SameSite=Lax, and time-limited. |
| No protected MySQL backup/restore workflow existed. | High | Added an authenticated backup page, integrity-checked backup files, schema-checked transactional restore, and a pre-restore safety backup. Verify with the tests and operating procedures below before relying on recovery. |

## Controls verified in code

- Admin passwords are checked with Werkzeug `check_password_hash`; plaintext passwords are not stored.
- A production deployment must set `APP_ENV=production`, `SECRET_KEY`, `ADMIN_PASSWORD_HASH`, `DB_NAME`, `DB_USER`, and `DB_PASSWORD`. Use a least-privilege database account.
- State-changing requests, including login, logout, backup, and restore, require the session CSRF token.
- Protected pages and backup downloads require an authenticated admin session. Backup files are outside `static/`, and download names are validated against a strict generated-filename pattern.
- Restore accepts only a previously generated, locally recorded file whose SHA-256 digest, database identity, table names, columns, and row counts validate. No uploaded SQL is accepted or executed.
- Restore checks that all configured base tables are InnoDB and schema-compatible before starting. It creates a new safety backup, then replaces rows inside a transaction without dropping or recreating the database.
- Errors returned to the browser are generic; technical exceptions are logged server-side. Do not expose application logs to the public.
- `.env` and `backups/` are ignored by Git. Keep credentials and backup files out of source control.

## Operational limits and remaining checks

- Login throttling is per client IP and is not a substitute for an edge firewall or centralized rate limiter. Configure trusted proxy handling deliberately if deploying behind a reverse proxy; do not trust arbitrary forwarded headers.
- Local filesystem backups are not off-site disaster recovery. Keep an encrypted, access-controlled offline or separate-host copy and periodically test recovery against a disposable database.
- Remote MySQL requires a CA bundle at `DB_SSL_CA`; verify that the certificate matches `DB_HOST`.
- This repository change does not configure a firewall, MySQL grants, operating-system ACLs, TLS termination, or cloud-managed backup retention. Those are deployment responsibilities.
- Before production, run `python -m unittest discover -s tests -v`, create a real backup, verify a downloaded copy, and test restore only against a separate disposable database.
