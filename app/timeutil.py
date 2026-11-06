from __future__ import annotations

import calendar
from datetime import datetime, timezone


def utcnow() -> datetime:
    """Naive UTC 'now' (stored the same way on SQLite and PostgreSQL)."""
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def add_months(d: datetime, months: int) -> datetime:
    """Calendar month arithmetic; clamps the day (Jan 31 + 1 month = Feb 28/29)."""
    m = d.month - 1 + months
    year = d.year + m // 12
    month = m % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return d.replace(year=year, month=month, day=day)
