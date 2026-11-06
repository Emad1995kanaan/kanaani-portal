"""Dashboard numbers and chart series."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import ROLE_RESELLER, TX_ALLOCATE, TX_CHARGE, TX_DEDUCT, TX_REFUND, Customer, Transaction, User
from ..timeutil import utcnow


def _day_series(db: Session, kind_filter, days: int, reseller_id: int | None, sign: int) -> list[dict]:  # noqa: ANN001
    now = utcnow()
    start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0)
    day = func.date(Transaction.created_at)
    stmt = select(day, func.sum(Transaction.amount)).where(Transaction.created_at >= start, kind_filter)
    if reseller_id:
        stmt = stmt.where(Transaction.reseller_id == reseller_id)
    totals = {str(d): int(v or 0) * sign for d, v in db.execute(stmt.group_by(day)).all()}
    out = []
    for i in range(days):
        d = (start + timedelta(days=i)).date().isoformat()
        out.append({"label": d[5:], "value": totals.get(d, 0)})
    return out


def admin_overview(db: Session) -> dict:
    now = utcnow()
    resellers = db.scalar(select(func.count()).where(User.role == ROLE_RESELLER)) or 0
    active_resellers = db.scalar(select(func.count()).where(User.role == ROLE_RESELLER, User.is_active.is_(True))) or 0
    customers = db.scalar(select(func.count()).select_from(Customer)) or 0
    active = db.scalar(select(func.count()).select_from(Customer).where(Customer.status == "active", Customer.expires_at > now)) or 0
    expired = db.scalar(select(func.count()).select_from(Customer).where((Customer.expires_at.is_(None)) | (Customer.expires_at <= now))) or 0
    expiring = db.scalar(select(func.count()).select_from(Customer).where(Customer.expires_at > now, Customer.expires_at <= now + timedelta(days=7))) or 0
    allocated = db.scalar(select(func.coalesce(func.sum(Transaction.amount), 0)).where(Transaction.kind == TX_ALLOCATE)) or 0
    deducted = -(db.scalar(select(func.coalesce(func.sum(Transaction.amount), 0)).where(Transaction.kind == TX_DEDUCT)) or 0)
    spent = -(db.scalar(select(func.coalesce(func.sum(Transaction.amount), 0)).where(Transaction.kind.in_([TX_CHARGE, TX_REFUND]))) or 0)
    outstanding = db.scalar(select(func.coalesce(func.sum(User.balance), 0)).where(User.role == ROLE_RESELLER)) or 0
    top = db.execute(
        select(User.display_name, User.username, func.count(Customer.id))
        .join(Customer, Customer.reseller_id == User.id).group_by(User.id).order_by(func.count(Customer.id).desc()).limit(5)
    ).all()
    return {
        "resellers": resellers, "active_resellers": active_resellers, "customers": customers, "active": active,
        "expired": expired, "expiring": expiring, "allocated": int(allocated), "deducted": int(deducted),
        "spent": int(spent), "outstanding": int(outstanding),
        "spend_series": _day_series(db, Transaction.kind.in_([TX_CHARGE, TX_REFUND]), 30, None, -1),
        "alloc_series": _day_series(db, Transaction.kind == TX_ALLOCATE, 30, None, 1),
        "top_resellers": [{"name": n or u, "count": c} for n, u, c in top],
    }


def reseller_overview(db: Session, reseller: User) -> dict:
    now = utcnow()
    base = select(func.count()).select_from(Customer).where(Customer.reseller_id == reseller.id)
    return {
        "balance": reseller.balance,
        "customers": db.scalar(base) or 0,
        "active": db.scalar(base.where(Customer.status == "active", Customer.expires_at > now)) or 0,
        "expired": db.scalar(base.where((Customer.expires_at.is_(None)) | (Customer.expires_at <= now))) or 0,
        "expiring": db.scalar(base.where(Customer.expires_at > now, Customer.expires_at <= now + timedelta(days=7))) or 0,
        "spend_series": _day_series(db, Transaction.kind.in_([TX_CHARGE, TX_REFUND]), 30, reseller.id, -1),
    }
