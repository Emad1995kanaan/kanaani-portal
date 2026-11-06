"""Reseller and admin accounts."""
from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import ROLE_ADMIN, ROLE_RESELLER, User
from ..security import end_all_sessions, hash_password, password_problems
from .credits import WalletError

USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,64}$")


class AccountError(WalletError):
    pass


def _check_username(db: Session, username: str, exclude_id: int | None = None) -> str:
    username = username.strip()
    if not USERNAME_RE.match(username):
        raise AccountError("Username: 3–64 letters, numbers, dot, dash or underscore.")
    q = select(User.id).where(func.lower(User.username) == username.lower())
    if exclude_id:
        q = q.where(User.id != exclude_id)
    if db.scalar(q) is not None:
        raise AccountError("That username is already taken.")
    return username


def _check_password(password: str) -> None:
    problems = password_problems(password)
    if problems:
        raise AccountError(" ".join(problems))


def create_user(db: Session, *, username: str, password: str, role: str, display_name: str = "", email: str = "",
                phone: str = "", must_change: bool = True) -> User:
    if role not in {ROLE_ADMIN, ROLE_RESELLER}:
        raise AccountError("Unknown role.")
    username = _check_username(db, username)
    _check_password(password)
    u = User(username=username, password_hash=hash_password(password), role=role, display_name=display_name.strip()[:120],
             email=email.strip()[:160], phone=phone.strip()[:40], must_change_password=must_change)
    db.add(u)
    db.flush()
    return u


def update_reseller(db: Session, reseller: User, *, display_name: str, email: str, phone: str) -> None:
    reseller.display_name, reseller.email, reseller.phone = display_name.strip()[:120], email.strip()[:160], phone.strip()[:40]


def set_active(db: Session, user: User, active: bool) -> None:
    user.is_active = active
    if not active:
        end_all_sessions(db, user.id)  # suspended accounts are signed out everywhere


def set_password(db: Session, user: User, password: str, *, keep_token: str | None = None, must_change: bool = False) -> None:
    _check_password(password)
    user.password_hash = hash_password(password)
    user.must_change_password = must_change
    end_all_sessions(db, user.id, keep_token=keep_token)
