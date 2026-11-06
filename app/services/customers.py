"""Customers and their subscription periods. Charging and the period change happen in ONE database transaction."""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..models import ROLE_ADMIN, TX_CHARGE, Customer, Plan, Subscription, Transaction, User
from ..timeutil import add_months, utcnow
from .credits import WalletError, apply_delta, find_by_key

REF_RE = re.compile(r"^[A-Za-z0-9._-]{3,64}$")


class CustomerError(WalletError):
    pass


@dataclass
class PurchaseResult:
    customer: Customer
    subscription: Subscription | None
    tx: Transaction
    replayed: bool = False


def generate_reference(db: Session) -> str:
    while True:
        ref = "C" + secrets.token_hex(4).upper()
        if db.scalar(select(Customer.id).where(Customer.reference == ref)) is None:
            return ref


def can_manage(user: User, customer: Customer) -> bool:
    return user.role == ROLE_ADMIN or customer.reseller_id == user.id


def get_plan(db: Session, plan_id: int) -> Plan:
    plan = db.get(Plan, plan_id)
    if plan is None or not plan.is_active:
        raise CustomerError("Choose an active plan.")
    return plan


def _extend(db: Session, customer: Customer, plan: Plan, actor: User, kind: str) -> Subscription:
    now = utcnow()
    start = customer.expires_at if customer.expires_at and customer.expires_at > now else now
    end = add_months(start, plan.months)
    sub = Subscription(customer_id=customer.id, plan_id=plan.id, plan_name=plan.name, months=plan.months,
                       credits=plan.credit_cost, starts_at=start, expires_at=end, kind=kind, created_by_id=actor.id)
    db.add(sub)
    customer.expires_at = end
    db.flush()
    return sub


def create_customer(db: Session, *, actor: User, reseller_id: int, full_name: str, plan_id: int,
                    reference: str = "", email: str = "", phone: str = "", notes: str = "",
                    key: str | None = None) -> PurchaseResult:
    """New customer + first subscription period, paid from the reseller's wallet."""
    prior = find_by_key(db, key)
    if prior is not None:
        return PurchaseResult(db.get(Customer, prior.customer_id), db.get(Subscription, prior.subscription_id) if prior.subscription_id else None, prior, True)

    full_name = full_name.strip()
    if not 2 <= len(full_name) <= 120:
        raise CustomerError("Name must be 2–120 characters.")
    reference = reference.strip() or generate_reference(db)
    if not REF_RE.match(reference):
        raise CustomerError("Reference: 3–64 letters, numbers, dot, dash or underscore.")
    if db.scalar(select(Customer.id).where(func.lower(Customer.reference) == reference.lower())) is not None:
        raise CustomerError("That reference is already used.")
    if actor.role != ROLE_ADMIN and actor.id != reseller_id:
        raise CustomerError("You can only create customers for your own account.")
    plan = get_plan(db, plan_id)
    try:
        c = Customer(reseller_id=reseller_id, reference=reference, full_name=full_name, email=email.strip()[:160],
                     phone=phone.strip()[:40], notes=notes.strip()[:2000])
        db.add(c)
        db.flush()
        sub = _extend(db, c, plan, actor, "new")
        tx = apply_delta(db, reseller_id=reseller_id, delta=-plan.credit_cost, kind=TX_CHARGE, actor=actor, key=key,
                         note=f"New: {plan.name}", customer_id=c.id, subscription_id=sub.id)
        db.commit()
        return PurchaseResult(c, sub, tx)
    except Exception:
        db.rollback()
        again = find_by_key(db, key)
        if again is not None:
            return PurchaseResult(db.get(Customer, again.customer_id), None, again, True)
        raise


def renew(db: Session, *, actor: User, customer: Customer, plan_id: int, key: str | None = None) -> PurchaseResult:
    prior = find_by_key(db, key)
    if prior is not None:
        return PurchaseResult(customer, None, prior, True)
    if not can_manage(actor, customer):
        raise CustomerError("Not your customer.")
    plan = get_plan(db, plan_id)
    try:
        sub = _extend(db, customer, plan, actor, "renewal")
        tx = apply_delta(db, reseller_id=customer.reseller_id, delta=-plan.credit_cost, kind=TX_CHARGE, actor=actor,
                         key=key, note=f"Renewal: {plan.name}", customer_id=customer.id, subscription_id=sub.id)
        db.commit()
        return PurchaseResult(customer, sub, tx)
    except Exception:
        db.rollback()
        again = find_by_key(db, key)
        if again is not None:
            return PurchaseResult(customer, None, again, True)
        raise


def void_subscription(db: Session, subscription_id: int) -> None:
    """Used by refunds: mark the period refunded and rebuild the customer's expiry from the remaining periods."""
    sub = db.get(Subscription, subscription_id)
    if sub is None or sub.status == "refunded":
        return
    sub.status = "refunded"
    db.flush()
    recompute_expiry(db, sub.customer_id)


def recompute_expiry(db: Session, customer_id: int) -> None:
    c = db.get(Customer, customer_id)
    if c is None:
        return
    subs = db.scalars(select(Subscription).where(Subscription.customer_id == customer_id, Subscription.status == "active")
                      .order_by(Subscription.created_at, Subscription.id)).all()
    end: datetime | None = None
    for s in subs:  # replay the remaining purchases in order
        start = s.created_at if end is None or end < s.created_at else end
        end = add_months(start, s.months)
    c.expires_at = end


def set_status(db: Session, *, actor: User, customer: Customer, status: str) -> None:
    if status not in {"active", "suspended"}:
        raise CustomerError("Unknown status.")
    if not can_manage(actor, customer):
        raise CustomerError("Not your customer.")
    customer.status = status
    db.commit()


def update_details(db: Session, *, actor: User, customer: Customer, full_name: str, email: str, phone: str, notes: str) -> None:
    if not can_manage(actor, customer):
        raise CustomerError("Not your customer.")
    full_name = full_name.strip()
    if not 2 <= len(full_name) <= 120:
        raise CustomerError("Name must be 2–120 characters.")
    customer.full_name, customer.email, customer.phone, customer.notes = full_name, email.strip()[:160], phone.strip()[:40], notes.strip()[:2000]
    db.commit()


def search(db: Session, *, owner: User | None, q: str = "", state: str = "", reseller_id: int | None = None,
           page: int = 1, per_page: int = 25) -> tuple[list[Customer], int]:
    """owner=None → all customers (admin). ``state``: active | expired | expiring | suspended."""
    now = utcnow()
    stmt = select(Customer)
    if owner is not None:
        stmt = stmt.where(Customer.reseller_id == owner.id)
    elif reseller_id:
        stmt = stmt.where(Customer.reseller_id == reseller_id)
    q = q.strip()
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(or_(func.lower(Customer.full_name).like(like), func.lower(Customer.reference).like(like),
                              func.lower(Customer.email).like(like), Customer.phone.like(f"%{q}%")))
    if state == "active":
        stmt = stmt.where(Customer.status == "active", Customer.expires_at > now)
    elif state == "expired":
        stmt = stmt.where(or_(Customer.expires_at.is_(None), Customer.expires_at <= now))
    elif state == "expiring":
        stmt = stmt.where(Customer.expires_at > now, Customer.expires_at <= now + timedelta(days=7))
    elif state == "suspended":
        stmt = stmt.where(Customer.status == "suspended")
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(Customer.expires_at.is_(None), Customer.expires_at, Customer.id)
                      .offset((max(page, 1) - 1) * per_page).limit(per_page)).all()
    return list(rows), int(total)

