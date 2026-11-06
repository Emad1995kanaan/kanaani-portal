# Backup & restore

## SQLite (default)
The database lives in `data/kanaani.db` (Docker: the `./data` volume).

**Backup while running** (safe, uses SQLite's online backup):
```bash
mkdir -p backups
sqlite3 data/kanaani.db ".backup 'backups/kanaani-$(date +%F-%H%M).db'"
# Docker:
docker compose exec app python -c "import sqlite3; s=sqlite3.connect('/app/data/kanaani.db'); d=sqlite3.connect('/app/data/backup.db'); s.backup(d); d.close()"
cp data/backup.db backups/kanaani-$(date +%F-%H%M).db
```
Daily cron example:
```
15 3 * * * cd /opt/kanaani && sqlite3 data/kanaani.db ".backup 'backups/kanaani-$(date +\%F).db'" && find backups -mtime +30 -delete
```

**Restore:**
```bash
docker compose stop app            # or stop uvicorn
cp backups/kanaani-YYYY-MM-DD.db data/kanaani.db
rm -f data/kanaani.db-wal data/kanaani.db-shm
docker compose start app           # migrations run automatically on start
```

## PostgreSQL
```bash
pg_dump -Fc -d "$DATABASE_URL_PLAIN" -f backups/kanaani-$(date +%F).dump
# restore into an empty database:
pg_restore --clean --if-exists -d "$DATABASE_URL_PLAIN" backups/kanaani-YYYY-MM-DD.dump
```
(`DATABASE_URL_PLAIN` = the URL without the `+psycopg` part, e.g. `postgresql://user:pass@host/kanaani`.)

Test a restore regularly on a separate machine — a backup you have never restored is not a backup.
