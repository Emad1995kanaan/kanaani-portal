# KANAANI — Subscription & Reseller Management Portal

A self-hosted web app to manage **resellers**, their **credit wallets**, and their **customers' subscriptions**.
Generic by design: a "subscription" is a period of access to any service you sell (software, memberships, internet, …).
It does not connect to any external service.

## Features
- **Admin dashboard** — resellers, customers, active / expired / expiring subscriptions, credits distributed and spent, 30-day charts, top resellers.
- **Resellers** — create, edit, suspend / activate (suspension signs them out everywhere), temporary passwords that must be changed at first sign-in, activity log.
- **Credit wallet** — add / deduct credits, every movement in an immutable ledger with a unique reference (`TX-YYMMDD-XXXXXXXX`), duplicate-charge protection (idempotency keys), balances can never go negative (enforced by the database), refunds with audit trail.
- **Plans & prices** — 1 / 3 / 6 / 12 months at 1 / 3 / 6 / 12 credits by default; the admin can change, add or disable plans.
- **Customers** — create (with first period), renew (added after the current expiry, or from today if expired), suspend / reactivate, search and filter (active, expiring in 7 days, expired, suspended).
- **Reseller area** — balance, own customers only, subscription history, transactions, profile.
- **Reports** — monthly totals, per-reseller totals, CSV export (spreadsheet-injection safe).
- **REST API** under `/api/v1`.
- **Security** — Argon2id passwords, server-side sessions with idle + absolute expiry, CSRF tokens, login lockout, rate limiting, strict CSP and security headers, role-based access, audit log. See [SECURITY.md](SECURITY.md).

Stack: FastAPI · SQLAlchemy 2 · Alembic · SQLite (default) or PostgreSQL · server-rendered pages (no external CDNs).

## Quick start (local)
```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # for local http set COOKIE_SECURE=false and ENVIRONMENT=development
export $(grep -v '^#' .env | xargs)
alembic upgrade head            # creates the database (and the default plans)
python -m scripts.create_admin --username admin
uvicorn app.main:app --reload   # http://127.0.0.1:8000
```
Optional mock data for development (refuses to run when `ENVIRONMENT=production`):
```bash
python -m scripts.seed_demo     # resellers alpha/beta/gamma/delta, password Demo-Pass-2026
```

## Docker
```bash
cp .env.example .env            # set SECRET_KEY!
docker compose up -d --build
docker compose exec app python -m scripts.create_admin --username admin
```
The app listens on `127.0.0.1:8000` only. Publish it through your own reverse proxy as a **separate site** — e.g. Caddy:
```
portal.example.com {
    reverse_proxy 127.0.0.1:8000
}
```
or Nginx:
```nginx
server {
    server_name portal.example.com;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```
Keep `COOKIE_SECURE=true` (HTTPS) and `TRUSTED_PROXY=true` behind the proxy.

## PostgreSQL
Set `DATABASE_URL=postgresql+psycopg://user:pass@host:5432/kanaani`, add `psycopg[binary]` to `requirements.txt`, then `alembic upgrade head`.

## Tests
```bash
pip install -r requirements-dev.txt
python -m pytest -q
```
The suite builds a fresh database with the real migrations and covers sign-in, lockout, sessions, CSRF, permissions,
wallet rules (no negative balance, idempotency, refunds, concurrent charges) and customer/subscription flows.

## REST API
Uses the same session cookie as the web pages.
- `GET /api/v1/me` — current user and the `csrf_token` to send as `X-CSRF-Token` on every write.
- `GET /api/v1/plans`, `GET /api/v1/customers?q=&state=&page=`, `GET /api/v1/customers/{id}`
- `POST /api/v1/customers` · `POST /api/v1/customers/{id}/renew` · `POST /api/v1/customers/{id}/status`
- `GET /api/v1/transactions`
- Admin: `GET /api/v1/admin/stats`, `GET|POST /api/v1/admin/resellers`, `POST /api/v1/admin/resellers/{id}/credits/allocate|deduct`

Send an `Idempotency-Key` header on charges and credit changes: retrying the same request never charges twice.
Set `ENABLE_API_DOCS=true` to get interactive docs at `/api/docs`.

## Backups
See [docs/BACKUP.md](docs/BACKUP.md).

## License
MIT — see [LICENSE](LICENSE).
