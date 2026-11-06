"""Request dependencies: who is signed in, role checks, CSRF checks."""
from __future__ import annotations

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from .db import get_db
from .models import ROLE_ADMIN, ROLE_RESELLER, AuthSession, User
from .security import SESSION_COOKIE, csrf_ok, load_session


class AuthRequired(Exception):
    """Not signed in (HTML → redirect to /login, API → 401)."""


class Forbidden(Exception):
    """Signed in but not allowed (→ 403)."""


class PasswordChangeRequired(Exception):
    """Temporary password: only the change-password page is reachable."""


class CsrfFailed(Exception):
    """Missing or wrong CSRF token (→ 403)."""


def get_session(request: Request, db: Session = Depends(get_db)) -> AuthSession | None:
    if not hasattr(request.state, "auth"):
        request.state.auth = load_session(db, request.cookies.get(SESSION_COOKIE))
    return request.state.auth


def require_user(request: Request, s: AuthSession | None = Depends(get_session)) -> User:
    if s is None:
        raise AuthRequired()
    request.state.user = s.user
    request.state.csrf = s.csrf_token
    return s.user


def require_admin(user: User = Depends(require_user)) -> User:
    if user.role != ROLE_ADMIN:
        raise Forbidden()
    if user.must_change_password:
        raise PasswordChangeRequired()
    return user


def require_reseller(user: User = Depends(require_user)) -> User:
    if user.role != ROLE_RESELLER:
        raise Forbidden()
    if user.must_change_password:
        raise PasswordChangeRequired()
    return user


async def csrf_form(request: Request, s: AuthSession | None = Depends(get_session)) -> None:
    """For HTML form posts: hidden field ``csrf_token`` must match the session's token."""
    form = await request.form()
    if s is None or not csrf_ok(s.csrf_token, str(form.get("csrf_token") or "")):
        raise CsrfFailed()


def csrf_header(request: Request, s: AuthSession | None = Depends(get_session)) -> None:
    """For JSON API writes: header ``X-CSRF-Token`` must match the session's token."""
    if s is None or not csrf_ok(s.csrf_token, request.headers.get("x-csrf-token")):
        raise CsrfFailed()
