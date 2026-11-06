from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import csrf_form, get_session, require_user
from ..models import ROLE_ADMIN, AuthSession, User
from ..security import (
    PRELOGIN_CSRF_COOKIE, SESSION_COOKIE, audit, authenticate, clear_session_cookie, client_ip, create_session,
    csrf_ok, end_session, login_locked, new_prelogin_csrf, purge_expired_sessions, record_login,
    set_prelogin_csrf_cookie, set_session_cookie, verify_password,
)
from ..services.accounts import AccountError, set_password
from ..timeutil import utcnow
from ..web import back, page, templates

router = APIRouter()


def home_for(user: User) -> str:
    if user.must_change_password:
        return "/account/password"
    return "/admin" if user.role == ROLE_ADMIN else "/r"


@router.get("/", include_in_schema=False)
def root(s: AuthSession | None = Depends(get_session)):  # noqa: ANN201
    return RedirectResponse(home_for(s.user) if s else "/login", status_code=303)


def _login_page(request: Request, error: str = "", username: str = "", status: int = 200):  # noqa: ANN202
    token = new_prelogin_csrf()
    resp = templates.TemplateResponse(request, "login.html", {"csrf": token, "error": error, "username": username}, status_code=status)
    set_prelogin_csrf_cookie(resp, token)
    return resp


@router.get("/login", include_in_schema=False)
def login_form(request: Request, s: AuthSession | None = Depends(get_session)):  # noqa: ANN201
    if s is not None:
        return RedirectResponse(home_for(s.user), status_code=303)
    return _login_page(request)


@router.post("/login", include_in_schema=False)
def login(request: Request, username: str = Form(""), password: str = Form(""), csrf_token: str = Form(""),
          db: Session = Depends(get_db)):  # noqa: ANN201
    username = username.strip()[:64]
    if not csrf_ok(request.cookies.get(PRELOGIN_CSRF_COOKIE), csrf_token):
        return _login_page(request, "Your form expired. Please try again.", username, 400)
    ip = client_ip(request)
    if login_locked(db, username, ip):
        audit(db, None, "login.locked", "user", username, ip)
        db.commit()
        return _login_page(request, "Too many failed attempts. Try again in a few minutes.", username, 429)
    user = authenticate(db, username, password[:256])
    record_login(db, username, ip, user is not None)
    if user is None:
        audit(db, None, "login.failed", "user", username, ip)
        db.commit()
        return _login_page(request, "Wrong username or password, or the account is suspended.", username, 401)
    purge_expired_sessions(db)
    token, _ = create_session(db, user, request)
    user.last_login_at = utcnow()
    audit(db, user, "login.ok", "user", user.id, ip)
    db.commit()
    resp = RedirectResponse(home_for(user), status_code=303)
    set_session_cookie(resp, token)
    resp.delete_cookie(PRELOGIN_CSRF_COOKIE, path="/login")
    return resp


@router.post("/logout", include_in_schema=False, dependencies=[Depends(csrf_form)])
def logout(request: Request, db: Session = Depends(get_db), s: AuthSession | None = Depends(get_session)):  # noqa: ANN201
    if s is not None:
        audit(db, s.user, "logout", "user", s.user_id, client_ip(request))
        db.commit()
    end_session(db, request.cookies.get(SESSION_COOKIE))
    resp = RedirectResponse("/login", status_code=303)
    clear_session_cookie(resp)
    return resp


@router.get("/account/password", include_in_schema=False)
def password_form(request: Request, user: User = Depends(require_user)):  # noqa: ANN201
    return page(request, "password.html", forced=user.must_change_password)


@router.post("/account/password", include_in_schema=False, dependencies=[Depends(csrf_form)])
def change_password(request: Request, current: str = Form(""), new: str = Form(""), confirm: str = Form(""),
                    user: User = Depends(require_user), db: Session = Depends(get_db)):  # noqa: ANN201
    if not verify_password(user.password_hash, current[:256]):
        return back("/account/password", err="Current password is wrong.")
    if new != confirm:
        return back("/account/password", err="The two new passwords don't match.")
    if new == current:
        return back("/account/password", err="Choose a different password.")
    try:
        set_password(db, user, new, keep_token=request.cookies.get(SESSION_COOKIE))
    except AccountError as e:
        db.rollback()
        return back("/account/password", err=str(e))
    audit(db, user, "password.changed", "user", user.id, client_ip(request))
    db.commit()
    return back(home_for(user), msg="Password changed. Other devices were signed out.")

