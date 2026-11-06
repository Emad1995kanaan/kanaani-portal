"""Each test session uses its own temporary SQLite file, built with the real Alembic migrations."""
from __future__ import annotations

import os
import re
import tempfile

_tmp = tempfile.mkdtemp(prefix="kanaani-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["COOKIE_SECURE"] = "false"
os.environ["RATE_LIMIT_PER_MINUTE"] = "0"
os.environ["ENVIRONMENT"] = "test"

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402

from app.db import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    ROLE_ADMIN, ROLE_RESELLER, AuditLog, AuthSession, Customer, LoginAttempt, Plan, Subscription, Transaction, User,
)
from app.security import hash_password  # noqa: E402

PASSWORD = "Strong-Pass-123"


def _alembic_cfg() -> Config:
    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "alembic"))
    return cfg


@pytest.fixture(scope="session", autouse=True)
def _schema():
    command.upgrade(_alembic_cfg(), "head")
    yield
    engine.dispose()


@pytest.fixture(autouse=True)
def _clean():
    yield
    with SessionLocal() as db:
        for t in (Transaction, Subscription, Customer, AuditLog, AuthSession, LoginAttempt):
            db.execute(delete(t))
        db.execute(delete(User))
        for p in db.scalars(select(Plan)).all():
            p.is_active = True
            p.credit_cost = p.months
        db.commit()


@pytest.fixture
def db():
    s = SessionLocal()
    yield s
    s.close()


def make_user(db, username: str, role: str, balance: int = 0, must_change: bool = False, active: bool = True) -> User:
    u = User(username=username, role=role, password_hash=hash_password(PASSWORD), balance=balance,
             must_change_password=must_change, is_active=active, display_name=username.title())
    db.add(u)
    db.commit()
    return u


@pytest.fixture
def admin(db):
    return make_user(db, "boss", ROLE_ADMIN)


@pytest.fixture
def reseller(db):
    return make_user(db, "seller1", ROLE_RESELLER, balance=10)


@pytest.fixture
def reseller2(db):
    return make_user(db, "seller2", ROLE_RESELLER, balance=10)


@pytest.fixture
def plan1(db) -> Plan:
    return db.scalar(select(Plan).where(Plan.months == 1))


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "no csrf token in page"
    return m.group(1)


def login(client: TestClient, username: str, password: str = PASSWORD):
    tok = csrf_of(client.get("/login").text)
    return client.post("/login", data={"username": username, "password": password, "csrf_token": tok}, follow_redirects=False)


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def signed_in(username: str) -> TestClient:
    c = TestClient(app)
    r = login(c, username)
    assert r.status_code == 303, r.text
    return c


def page_csrf(client: TestClient, path: str) -> str:
    return csrf_of(client.get(path).text)
