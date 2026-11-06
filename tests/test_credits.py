import threading
import pytest
from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import Customer, Subscription, Transaction, User
from app.services import credits, customers
from app.services.credits import InsufficientCredits, WalletError
from app.timeutil import add_months


def bal(db, uid):
    db.expire_all()
    return db.get(User, uid).balance


def test_allocate_and_deduct(db, admin, reseller):
    r = credits.allocate(db, admin=admin, reseller_id=reseller.id, amount=5, note="pay")
    assert r.tx.amount == 5 and r.tx.balance_after == 15 and r.tx.ref.startswith("TX-")
    r = credits.deduct(db, admin=admin, reseller_id=reseller.id, amount=3)
    assert r.tx.amount == -3 and bal(db, reseller.id) == 12


def test_rejects_bad_amounts(db, admin, reseller):
    with pytest.raises(WalletError):
        credits.allocate(db, admin=admin, reseller_id=reseller.id, amount=0)
    with pytest.raises(WalletError):
        credits.deduct(db, admin=admin, reseller_id=reseller.id, amount=-4)


def test_no_negative_balance(db, admin, reseller):
    with pytest.raises(InsufficientCredits):
        credits.deduct(db, admin=admin, reseller_id=reseller.id, amount=11)
    assert bal(db, reseller.id) == 10
    assert db.scalar(select(func.count()).select_from(Transaction)) == 0


def test_unknown_reseller(db, admin):
    with pytest.raises(WalletError):
        credits.allocate(db, admin=admin, reseller_id=99999, amount=1)


def test_admin_has_no_wallet(db, admin):
    with pytest.raises(WalletError):
        credits.allocate(db, admin=admin, reseller_id=admin.id, amount=1)


def test_idempotency_key_replay(db, admin, reseller):
    a = credits.allocate(db, admin=admin, reseller_id=reseller.id, amount=7, key="k-1")
    b = credits.allocate(db, admin=admin, reseller_id=reseller.id, amount=7, key="k-1")
    assert not a.replayed and b.replayed and a.tx.id == b.tx.id
    assert bal(db, reseller.id) == 17
    assert db.scalar(select(func.count()).select_from(Transaction)) == 1


def test_refund_restores_credits_and_expiry(db, admin, reseller, plan1):
    first = customers.create_customer(db, actor=reseller, reseller_id=reseller.id, full_name="Refund Me", plan_id=plan1.id)
    c = first.customer
    after_first = c.expires_at
    second = customers.renew(db, actor=reseller, customer=c, plan_id=plan1.id)
    assert c.expires_at == add_months(after_first, 1)
    assert bal(db, reseller.id) == 8
    r = credits.refund_charge(db, admin=admin, tx_id=second.tx.id)
    assert r.tx.kind == "refund" and r.tx.amount == 1 and r.tx.refund_of_id == second.tx.id
    assert bal(db, reseller.id) == 9
    db.expire_all()
    c = db.get(Customer, c.id)
    assert abs((c.expires_at - after_first).total_seconds()) < 2
    assert db.get(Subscription, second.subscription.id).status == "refunded"


def test_double_refund_is_noop(db, admin, reseller, plan1):
    res = customers.create_customer(db, actor=reseller, reseller_id=reseller.id, full_name="Twice", plan_id=plan1.id)
    a = credits.refund_charge(db, admin=admin, tx_id=res.tx.id)
    b = credits.refund_charge(db, admin=admin, tx_id=res.tx.id)
    assert b.replayed and a.tx.id == b.tx.id
    assert bal(db, reseller.id) == 10
    db.expire_all()
    assert db.get(Customer, res.customer.id).expires_at is None


def test_only_charges_refundable(db, admin, reseller):
    t = credits.allocate(db, admin=admin, reseller_id=reseller.id, amount=2).tx
    with pytest.raises(WalletError):
        credits.refund_charge(db, admin=admin, tx_id=t.id)


def test_concurrent_charges_cannot_overspend(db, reseller, plan1):
    """10 credits, 25 parallel 1-credit sales: exactly 10 succeed, balance ends at 0."""
    ok, failed = [], []
    lock = threading.Lock()

    def sell(i):
        with SessionLocal() as s:
            me = s.get(User, reseller.id)
            try:
                customers.create_customer(s, actor=me, reseller_id=me.id, full_name=f"Racer {i}", plan_id=plan1.id)
                with lock:
                    ok.append(i)
            except InsufficientCredits:
                with lock:
                    failed.append(i)

    threads = [threading.Thread(target=sell, args=(i,)) for i in range(25)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ok) == 10 and len(failed) == 15
    assert bal(db, reseller.id) == 0
    assert db.scalar(select(func.count()).select_from(Customer)) == 10
    assert db.scalar(select(func.count()).select_from(Transaction).where(Transaction.kind == "charge")) == 10


def test_ledger_balance_matches_wallet(db, admin, reseller, plan1):
    credits.allocate(db, admin=admin, reseller_id=reseller.id, amount=4)
    customers.create_customer(db, actor=reseller, reseller_id=reseller.id, full_name="Ledger", plan_id=plan1.id)
    credits.deduct(db, admin=admin, reseller_id=reseller.id, amount=2)
    total = db.scalar(select(func.sum(Transaction.amount)).where(Transaction.reseller_id == reseller.id))
    assert 10 + total == bal(db, reseller.id) == 11


def test_admin_credit_form_and_replay(admin, reseller, db):
    from tests.conftest import page_csrf, signed_in
    c = signed_in("boss")
    tok = page_csrf(c, f"/admin/resellers/{reseller.id}")
    data = {"csrf_token": tok, "op": "allocate", "amount": "5", "key": "same-key"}
    assert c.post(f"/admin/resellers/{reseller.id}/credits", data=data, follow_redirects=False).status_code == 303
    r = c.post(f"/admin/resellers/{reseller.id}/credits", data=data)
    assert "not charged twice" in r.text
    assert bal(db, reseller.id) == 15

