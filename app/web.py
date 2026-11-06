"""Shared helpers for HTML pages."""
from __future__ import annotations

import secrets
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import __version__
from .config import settings
from .timeutil import utcnow

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def _fmt_dt(d: datetime | None) -> str:
    return d.strftime("%Y-%m-%d %H:%M") if d else "—"


def _fmt_date(d: datetime | None) -> str:
    return d.strftime("%Y-%m-%d") if d else "—"


def _days_left(d: datetime | None) -> int | None:
    if d is None:
        return None
    return int((d - utcnow()).total_seconds() // 86400)


def _state(c) -> str:  # noqa: ANN001
    if c.status == "suspended":
        return "suspended"
    if c.expires_at is None or c.expires_at <= utcnow():
        return "expired"
    if (c.expires_at - utcnow()).days < 7:
        return "expiring"
    return "active"


def _signed(n: int) -> str:
    return f"+{n}" if n > 0 else str(n)


templates.env.globals.update(app_name=settings.app_name, version=__version__, new_key=lambda: secrets.token_urlsafe(16))
templates.env.filters.update(dt=_fmt_dt, date=_fmt_date, days_left=_days_left, cstate=_state, signed=_signed)


def back(path: str, msg: str = "", err: str = "") -> RedirectResponse:
    """Post/Redirect/Get with a one-line notice (shown escaped)."""
    q = {k: v for k, v in (("msg", msg), ("err", err)) if v}
    return RedirectResponse(path + ("?" + urlencode(q) if q else ""), status_code=303)


def page(request: Request, name: str, **ctx: object):  # noqa: ANN201
    ctx.setdefault("msg", request.query_params.get("msg", "")[:200])
    ctx.setdefault("err", request.query_params.get("err", "")[:300])
    ctx.setdefault("user", getattr(request.state, "user", None))
    ctx.setdefault("csrf", getattr(request.state, "csrf", ""))
    return templates.TemplateResponse(request, name, ctx)


def to_int(v: object, default: int = 0) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default
