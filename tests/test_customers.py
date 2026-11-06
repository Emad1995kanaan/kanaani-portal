from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import Customer, Transaction, User
from app.services import customers
from app.services.customers import CustomerError
from app.timeutil import add_months, utcnow
from tests.conftest import page_csrf, signed_in


def test_add_months_clamps():
    assert add_months(datetime(2026, 1, 31), 1) == datetime(2026, 2, 28)
    assert add_months(datetime(2028, 1, 31), 1) == datetime(2028, 2, 29)
    assert add_months(datetime(2026, 11, 15), 3) == datetime(2027, 2, 15)
    assert add_months(datetime(2026, 5, 31), 12) == datetime(2027, 5, 31)


def test_create_charges_plan_cost(db, reseller):
    plan3 = db.scalar(select(customers.Plan).where(customers.Plan.months == 3))
    res = customers.create_customer(db, actor=reseller, reseller_id=reseller.id, full_name="Three Months", plan_id=plan3.id)
    db.expire_all()
    assert db.get(User, reseller.id).balance == 7
    assert res.tx.amount == -3 and res.tx.kind == "charge"
    assert abs((res.customer.expires_at - add_months(utcnow(), 3)).total_seconds()) < 5
    assert res.customer.reference.startswith("C")


def test_insufficient_credits_creates_nothing(db, reseller):
    plan12 = db.scalar(select(customers.Plan).where(customers.Plan.months == 12))
    with pytest.raises(customers.WalletError):
        customers.create_customer(db, actor=reseller, reseller_id=reseller.id, full_name="Too Big", plan_id=plan12.id)
    assert db.scalar(select(Customer)) is None


def test_validation(db, reseller, plan1):
    with pytest.raises(CustomerError):
        customers.create_customer(db, actor=reseller, reseller_id=reseller.id, full_name="x", plan_id=plan1.id)
    with pytest.raises(CustomerError):
        customers.create_customer(db, actor=reseller, reseller_id=reseller.id, full_name="Bad Ref", plan_id=plan1.id, reference="a b")
    customers.create_customer(db, actor=reseller, reseller_id=reseller.id, full_name="First", plan_id=plan1.id, reference="REF-1")
    with pytest.raises(CustomerError):
        customers.create_customer(db, actor=reseller, reseller_id=reseller.id, full_name="Dup", plan_id=plan1.id, reference="ref-1")


def test_reseller_cannot_create_for_another(db, reseller, reseller2, plan1):
    with pytest.raises(CustomerError):
        customers.create_customer(db, actor=reseller, reseller_id=reseller2.id, full_name="Not Mine", plan_id=plan1.id)


def test_inactive_plan_refused(db, reseller, plan1):
    plan1.is_active = False
    db.commit()
    with pytest.raises(CustomerError):
        customers.create_customer(db, actor=reseller, reseller_id=reseller.id, full_name="No Plan", plan_id=plan1.id)


def test_renew_extends_from_current_expiry(db, reseller, plan1):
    c = customers.create_customer(db, actor=reseller, reseller_id=reseller.id, full_name="Keep Going", plan_id=plan1.id).customer
    before = c.expires_at
    customers.renew(db, actor=reseller, customer=c, plan_id=plan1.id)
    assert c.expires_at == add_months(before, 1)


def test_renew_expired_starts_now(db, reseller, plan1):
    c = customers.create_customer(db, actor=reseller, reseller_id=reseller.id, full_name="Lapsed", plan_id=plan1.id).customer
    c.expires_at = utcnow() - timedelta(days=40)
    db.commit()
    res = customers.renew(db, actor=reseller, customer=c, plan_id=plan1.id)
    assert abs((c.expires_at - add_months(utcnow(), 1)).total_seconds()) < 5
    assert res.subscription.kind == "renewal"


def test_renew_idempotent(db, reseller, plan1):
    c = customers.create_customer(db, actor=reseller, reseller_id=reseller.id, full_name="Once", plan_id=plan1.id).customer
    customers.renew(db, actor=reseller, customer=c, plan_id=plan1.id, key="r-1")
    again = customers.renew(db, actor=reseller, customer=c, plan_id=plan1.id, key="r-1")
    assert again.replayed
    assert len(db.scalars(select(Transaction).where(Transaction.customer_id == c.id)).all()) == 2


def test_search_filters(db, reseller, reseller2, plan1):
    def mk(name, delta_days=None, status="active", owner=reseller):
        c = customers.create_customer(db, actor=owner, reseller_id=owner.id, full_name=name, plan_id=plan1.id).customer
        if delta_days is not None:
            c.expires_at = utcnow() + timedelta(days=delta_days)
        c.status = status
        db.commit()
        return c
    mk("Alpha Active", 60)
    mk("Beta Expiring", 3)
    mk("Gamma Expired", -2)
    mk("Delta Suspended", 60, "suspended")
    mk("Other Owner", 60, owner=reseller2)
    names = lambda state: {c.full_name for c in customers.search(db, owner=reseller, state=state)[0]}  # noqa: E731
    assert names("active") == {"Alpha Active", "Beta Expiring"}
    assert names("expiring") == {"Beta Expiring"}
    assert names("expired") == {"Gamma Expired"}
    assert names("suspended") == {"Delta Suspended"}
    assert names("") == {"Alpha Active", "Beta Expiring", "Gamma Expired", "Delta Suspended"}
    rows, total = customers.search(db, owner=reseller, q="gamma")
    assert total == 1 and rows[0].full_name == "Gamma Expired"
    assert customers.search(db, owner=None)[1] == 5


def test_reseller_pages_flow(db, reseller, plan1):
    c = signed_in("seller1")
    for p in ["/r", "/r/customers", "/r/transactions", "/r/subscriptions", "/r/settings"]:
        assert c.get(p).status_code == 200, p
    tok = page_csrf(c, "/r/customers")
    r = c.post("/r/customers", data={"csrf_token": tok, "full_name": "Web Person", "plan_id": plan1.id, "key": "web-1"}, follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"].split("?")[0]
    cid = int(loc.rsplit("/", 1)[1])
    assert "Web Person" in c.get(loc).text
    # double submit with the same form key: still one customer
    c.post("/r/customers", data={"csrf_token": tok, "full_name": "Web Person", "plan_id": plan1.id, "key": "web-1"})
    assert len(db.scalars(select(Customer)).all()) == 1
    tok = page_csrf(c, loc)
    c.post(f"/r/customers/{cid}/renew", data={"csrf_token": tok, "plan_id": plan1.id, "key": "web-2"})
    c.post(f"/r/customers/{cid}/status", data={"csrf_token": tok, "status": "suspended"})
    c.post(f"/r/customers/{cid}/edit", data={"csrf_token": tok, "full_name": "Web Person 2", "email": "a@b.c", "phone": "1", "notes": "n"})
    db.expire_all()
    cu = db.get(Customer, cid)
    assert cu.status == "suspended" and cu.full_name == "Web Person 2"
    assert db.get(User, reseller.id).balance == 8
    html = c.get("/r/transactions").text
    assert "charge" in html


def test_api_flow(db, reseller, plan1):
    c = signed_in("seller1")
    me = c.get("/api/v1/me").json()
    assert me["balance"] == 10
    h = {"X-CSRF-Token": me["csrf_token"], "Idempotency-Key": "api-1"}
    r = c.post("/api/v1/customers", json={"full_name": "Api One", "plan_id": plan1.id}, headers=h)
    assert r.status_code == 201
    cid = r.json()["customer"]["id"]
    r2 = c.post("/api/v1/customers", json={"full_name": "Api One", "plan_id": plan1.id}, headers=h)
    assert r2.json()["replayed"] is True
    r = c.post(f"/api/v1/customers/{cid}/renew", json={"plan_id": plan1.id}, headers={"X-CSRF-Token": me["csrf_token"]})
    assert r.status_code == 200 and r.json()["transaction"]["amount"] == -1
    assert c.get("/api/v1/customers").json()["total"] == 1
    assert len(c.get("/api/v1/transactions").json()["items"]) == 2
    assert c.get("/api/v1/plans").status_code == 200


def test_api_insufficient_is_402(db, reseller):
    plan12 = db.scalar(select(customers.Plan).where(customers.Plan.months == 12))
    c = signed_in("seller1")
    tok = c.get("/api/v1/me").json()["csrf_token"]
    r = c.post("/api/v1/customers", json={"full_name": "Pricey", "plan_id": plan12.id}, headers={"X-CSRF-Token": tok})
    assert r.status_code == 402


def test_admin_pages_and_reports(db, admin, reseller, plan1):
    customers.create_customer(db, actor=reseller, reseller_id=reseller.id, full_name="Report Me", plan_id=plan1.id)
    c = signed_in("boss")
    cid = db.scalar(select(Customer.id))
    for p in ["/admin", "/admin/resellers", f"/admin/resellers/{reseller.id}", "/admin/customers", f"/admin/customers/{cid}",
              "/admin/customers?state=active", "/admin/transactions", "/admin/reports", "/admin/plans", "/admin/audit"]:
        assert c.get(p).status_code == 200, p
    csv = c.get("/admin/transactions.csv")
    assert csv.status_code == 200 and "charge" in csv.text


def test_admin_creates_reseller_with_opening_credits(db, admin):
    c = signed_in("boss")
    tok = page_csrf(c, "/admin/resellers")
    r = c.post("/admin/resellers", data={"csrf_token": tok, "username": "newshop", "password": "Temp-Pass-12345",
                                         "credits_start": "25", "key": "rk-1"}, follow_redirects=False)
    assert r.status_code == 303
    u = db.scalar(select(User).where(User.username == "newshop"))
    assert u.balance == 25 and u.must_change_password


def test_admin_edits_plan_price(db, admin, plan1):
    c = signed_in("boss")
    tok = page_csrf(c, "/admin/plans")
    c.post("/admin/plans", data={"csrf_token": tok, "plan_id": plan1.id, "name": "Monthly", "months": "1", "credit_cost": "2",
                                 "is_active": "1", "sort_order": "1"})
    db.expire_all()
    assert db.get(customers.Plan, plan1.id).credit_cost == 2
