# Security

| Area | How it is handled |
|---|---|
| Passwords | Argon2id (`argon2-cffi`), rehashed automatically when parameters change. Minimum 10 characters with upper/lower case and a number. Unknown usernames take the same time as wrong passwords. |
| Sessions | Random 256-bit token in an `HttpOnly`, `SameSite=Lax`, `Secure` cookie. Only its SHA-256 is stored. Idle timeout (default 30 min) and absolute lifetime (default 12 h). Changing a password or suspending an account ends the other sessions. |
| CSRF | Per-session token in every form (`csrf_token`) and `X-CSRF-Token` header for the API. Sign-in uses a separate double-submit token. |
| Brute force | 5 failed sign-ins for a username (or 20 from one IP) within 15 minutes lock sign-in for that window. Every attempt is recorded. |
| Rate limiting | Per-IP limit on all state-changing requests (default 120/min) plus a separate limit on `/login`. |
| Access control | Admin and reseller roles checked on every route. Resellers only ever see their own customers and transactions (others look like "not found"). Temporary passwords must be changed before anything else. |
| Money integrity | Balance changes are a single conditional `UPDATE … WHERE balance + delta >= 0` plus a ledger row in the same transaction; a `CHECK (balance >= 0)` constraint backs it up. Idempotency keys are unique in the database. |
| Injection | All database access goes through SQLAlchemy with bound parameters. Templates auto-escape. CSV exports neutralise spreadsheet formulas. |
| Browser hardening | `Content-Security-Policy` without inline scripts or styles, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy`, `Permissions-Policy`, HSTS when cookies are secure, `Cache-Control: no-store` on pages. |
| Audit | Sign-ins (ok / failed / locked), password changes, reseller changes, credit operations, refunds, customer changes and plan/price changes are logged with actor, IP and details. |
| Secrets | Only from environment variables (`.env`, never committed). The app refuses to start in production without a strong `SECRET_KEY`. |

## Reporting a problem
Please report security issues privately to the maintainer instead of opening a public issue.
