"""Passwords (Argon2id), server-side sessions, CSRF, login lockout, rate limiting and audit logging."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Request
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .config import settings
from .models import AuditLog, AuthSession, LoginAttempt, User
from .timeutil import utcnow

SESSION_COOKIE = "kanaani_session"
PRELOGIN_CSRF_COOKIE = "kanaani_csrf"

_hasher = PasswordHasher()  # Argon2id with library defaults (time_cost=3, memory 64 MiB, parallelism 4)
# Used to keep timing similar when the username does not exist.
_DUMMY_HASH = _hasher.hash("timing-equaliser-" + secrets.token_hex(8))


# ── passwords ──────────────────────────────────────────────────────────────
def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


def password_problems(password: str) -> list[str]:
    problems = []
    if len(password) < 10:
        problems.append("Password must be at least 10 characters.")
    if len(password) > 256:
        problems.append("Password is too long.")
    if password.lower() == password or password.upper() == password:
        problems.append("Use both upper- and lower-case letters.")
    if not any(c.isdigit() for c in password):
        problems.append("Include at least one number.")
    return problems


def generate_password(length: int = 14) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    while True:
        p = "".join(secrets.choice(alphabet) for _ in range(length))
        if not password_problems(p):
            return p


# ── request helpers ────────────────────────────────────────────────────────
def client_ip(request: Request) -> str:
    if settings.trusted_proxy:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ── sessions ───────────────────────────────────────────────────────────────
def create_session(db: Session, user: User, request: Request) -> tuple[str, AuthSession]:
    token = secrets.token_urlsafe(32)
    now = utcnow()
    s = AuthSession(
        token_hash=_sha(token), user_id=user.id, csrf_token=secrets.token_urlsafe(32),
        ip=client_ip(request), user_agent=(request.headers.get("user-agent") or "")[:255],
        created_at=now, last_seen_at=now, expires_at=now + timedelta(hours=settings.session_max_hours),
    )
    db.add(s)
    return token, s


def load_session(db: Session, token: str | None) -> AuthSession | None:
    """Returns a valid session (sliding idle timeout, absolute max age) or None."""
    if not token or len(token) > 200:
        return None
    s = db.scalar(select(AuthSession).where(AuthSession.token_hash == _sha(token)))
    if s is None:
        return None
    now = utcnow()
    idle = timedelta(minutes=settings.session_idle_minutes)
    if s.expires_at <= now or s.last_seen_at + idle <= now or not s.user or not s.user.is_active:
        db.delete(s)
        db.commit()
        return None
    if (now - s.last_seen_at).total_seconds() > 30:
        s.last_seen_at = now
        db.commit()
    return s


def end_session(db: Session, token: str | None) -> None:
    if token:
        db.execute(delete(AuthSession).where(AuthSession.token_hash == _sha(token)))
        db.commit()


def end_all_sessions(db: Session, user_id: int, keep_token: str | None = None) -> None:
    q = delete(AuthSession).where(AuthSession.user_id == user_id)
    if keep_token:
        q = q.where(AuthSession.token_hash != _sha(keep_token))
    db.execute(q)


def purge_expired_sessions(db: Session) -> None:
    db.execute(delete(AuthSession).where(AuthSession.expires_at <= utcnow()))
    db.commit()


def set_session_cookie(response, token: str) -> None:  # noqa: ANN001
    response.set_cookie(
        SESSION_COOKIE, token, max_age=settings.session_max_hours * 3600, httponly=True,
        secure=settings.cookie_secure, samesite="lax", path="/",
    )


def clear_session_cookie(response) -> None:  # noqa: ANN001
    response.delete_cookie(SESSION_COOKIE, path="/", secure=settings.cookie_secure, httponly=True, samesite="lax")


# ── CSRF ───────────────────────────────────────────────────────────────────
def csrf_ok(expected: str | None, given: str | None) -> bool:
    return bool(expected) and bool(given) and hmac.compare_digest(expected, given)


def new_prelogin_csrf() -> str:
    return secrets.token_urlsafe(32)


def set_prelogin_csrf_cookie(response, token: str) -> None:  # noqa: ANN001
    response.set_cookie(PRELOGIN_CSRF_COOKIE, token, max_age=3600, httponly=True,
                        secure=settings.cookie_secure, samesite="strict", path="/login")


# ── login protection ───────────────────────────────────────────────────────
def login_locked(db: Session, username: str, ip: str) -> bool:
    """Locked when the username — or the IP — had too many failures inside the lock window."""
    since = utcnow() - timedelta(minutes=settings.login_lock_minutes)
    base = select(func.count()).select_from(LoginAttempt).where(LoginAttempt.success.is_(False), LoginAttempt.created_at >= since)
    by_user = db.scalar(base.where(LoginAttempt.username == username.lower())) or 0
    by_ip = db.scalar(base.where(LoginAttempt.ip == ip)) or 0
    return by_user >= settings.login_max_failures or by_ip >= settings.login_max_failures * 4


def record_login(db: Session, username: str, ip: str, success: bool) -> None:
    db.add(LoginAttempt(username=username.lower()[:64], ip=ip, success=success))
    if success:  # a good login clears the user's failure streak
        db.execute(delete(LoginAttempt).where(LoginAttempt.username == username.lower(), LoginAttempt.success.is_(False)))
    db.commit()


def authenticate(db: Session, username: str, password: str) -> User | None:
    user = db.scalar(select(User).where(func.lower(User.username) == username.lower()))
    if user is None:
        verify_password(_DUMMY_HASH, password)  # same work either way
        return None
    if not verify_password(user.password_hash, password) or not user.is_active:
        return None
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    return user


# ── audit ──────────────────────────────────────────────────────────────────
def audit(db: Session, actor: User | None, action: str, target_type: str = "", target_id: object = "",
          ip: str = "", **details: object) -> None:
    db.add(AuditLog(
        actor_id=actor.id if actor else None, action=action, target_type=target_type,
        target_id=str(target_id or ""), ip=ip,
        details=json.dumps(details, default=str, ensure_ascii=False) if details else "",
    ))


# ── rate limiting (per IP, in memory, for state-changing requests) ─────────
class RateLimiter:
    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        if self.per_minute <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.per_minute:
                return False
            q.append(now)
            if len(self._hits) > 10_000:  # keep memory bounded
                for k in [k for k, v in self._hits.items() if not v][:5000]:
                    self._hits.pop(k, None)
            return True


rate_limiter = RateLimiter(settings.rate_limit_per_minute)
