from datetime import timedelta

from sqlalchemy import select

from app.models import AuthSession, ROLE_RESELLER
from app.timeutil import utcnow
from tests.conftest import PASSWORD, login, make_user, page_csrf, signed_in


def test_login_ok_sets_secure_cookie(client, admin):
    r = login(client, "boss")
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    cookie = r.headers["set-cookie"].lower()
    assert "kanaani_session=" in cookie and "httponly" in cookie and "samesite=lax" in cookie
    assert client.get("/admin").status_code == 200


def test_login_is_case_insensitive_on_username(client, admin):
    assert login(client, "BOSS").status_code == 303


def test_wrong_password(client, admin):
    r = login(client, "boss", "nope")
    assert r.status_code == 401 and "Wrong username or password" in r.text


def test_unknown_user_same_message(client):
    r = login(client, "ghost", "whatever")
    assert r.status_code == 401 and "Wrong username or password" in r.text


def test_login_requires_csrf(client, admin):
    client.get("/login")
    r = client.post("/login", data={"username": "boss", "password": PASSWORD, "csrf_token": "forged"})
    assert r.status_code == 400


def test_lockout_after_failures(client, admin):
    for _ in range(5):
        assert login(client, "boss", "bad").status_code == 401
    r = login(client, "boss")  # right password, but locked
    assert r.status_code == 429


def test_logout_kills_session(admin, db):
    c = signed_in("boss")
    tok = page_csrf(c, "/admin")
    assert c.post("/logout", data={"csrf_token": tok}, follow_redirects=False).status_code == 303
    assert c.get("/admin", follow_redirects=False).headers["location"] == "/login"
    assert db.scalar(select(AuthSession)) is None


def test_logout_needs_csrf(admin):
    c = signed_in("boss")
    assert c.post("/logout", data={"csrf_token": "x"}).status_code == 403


def test_idle_session_expires(admin, db):
    c = signed_in("boss")
    s = db.scalar(select(AuthSession))
    s.last_seen_at = utcnow() - timedelta(hours=2)
    db.commit()
    assert c.get("/admin", follow_redirects=False).status_code == 303


def test_temporary_password_must_be_changed(db):
    make_user(db, "newbie", ROLE_RESELLER, must_change=True)
    c = signed_in("newbie")
    assert c.get("/r", follow_redirects=False).headers["location"] == "/account/password"
    assert c.get("/api/v1/customers").status_code == 403
    tok = page_csrf(c, "/account/password")
    r = c.post("/account/password", data={"csrf_token": tok, "current": PASSWORD, "new": "Brand-New-Pass-9", "confirm": "Brand-New-Pass-9"}, follow_redirects=False)
    assert r.status_code == 303
    assert c.get("/r").status_code == 200


def test_weak_password_rejected(admin):
    c = signed_in("boss")
    tok = page_csrf(c, "/account/password")
    r = c.post("/account/password", data={"csrf_token": tok, "current": PASSWORD, "new": "short", "confirm": "short"})
    assert "at least 10 characters" in r.text


def test_suspended_reseller_cannot_sign_in_and_is_signed_out(admin, reseller, db):
    rc = signed_in("seller1")
    ac = signed_in("boss")
    tok = page_csrf(ac, f"/admin/resellers/{reseller.id}")
    ac.post(f"/admin/resellers/{reseller.id}/status", data={"csrf_token": tok, "active": "0"})
    assert rc.get("/r", follow_redirects=False).status_code == 303  # existing session is gone
    from fastapi.testclient import TestClient
    from app.main import app
    assert login(TestClient(app), "seller1").status_code == 401


def test_password_is_argon2(admin):
    assert admin.password_hash.startswith("$argon2id$")
