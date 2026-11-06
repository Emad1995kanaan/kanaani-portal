"""REST API (JSON). Uses the same session cookie as the web pages; every write needs the
``X-CSRF-Token`` header (get it from ``GET /api/v1/me``). Send an ``Idempotency-Key`` header on
charges so a retried request is never charged twice."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import csrf_header, get_session, require_admin, require_user
from ..models import ROLE_ADMIN, ROLE_RESELLER, AuthSession, Customer, Plan, Transaction, User
from ..security import audit, client_ip
from ..services import accounts, credits, customers, stats
from ..services.credits import WalletError

router = APIRouter(prefix="/api/v1", tags=["api"])


class CustomerOut(BaseModel):
    id: int
    reference: str
    full_name: str
    email: str
    phone: str
    status: str
    expires_at: datetime | None
    reseller_id: int


class TxOut(BaseModel):
    ref: str
    kind: str
    amount: int
    balance_after: int
    customer_id: int | None
    note: str
    created_at: datetime


class CustomerIn(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    plan_id: int
    reference: str = ""
    email: str = ""
    phone: str = ""
    notes: str = ""
    reseller_id: int | None = None  # admin only


class RenewIn(BaseModel):
    plan_id: int


class StatusIn(BaseModel):
    status: str


class CreditIn(BaseModel):
    amount: int = Field(gt=0, le=1_000_000)
    note: str = ""


class ResellerIn(BaseModel):
    username: str
    password: str
    display_name: str = ""
    email: str = ""
    phone: str = ""


def _c(c: Customer) -> CustomerOut:
    return CustomerOut(id=c.id, reference=c.reference, full_name=c.full_name, email=c.email, phone=c.phone,
                       status=c.status, expires_at=c.expires_at, reseller_id=c.reseller_id)


def _t(t: Transaction) -> TxOut:
    return TxOut(ref=t.ref, kind=t.kind, amount=t.amount, balance_after=t.balance_after, customer_id=t.customer_id,
                 note=t.note, created_at=t.created_at)


def _customer_for(db: Session, user: User, cid: int) -> Customer:
    c = db.get(Customer, cid)
    if c is None or (user.role != ROLE_ADMIN and c.reseller_id != user.id):
        raise HTTPException(404, "Not found.")
    return c


def _err(e: Exception) -> HTTPException:
    return HTTPException(402 if isinstance(e, credits.InsufficientCredits) else 400, str(e))


@router.get("/me")
def me(user: User = Depends(require_user), s: AuthSession | None = Depends(get_session)) -> dict:
    return {"id": user.id, "username": user.username, "role": user.role, "display_name": user.display_name,
            "balance": user.balance if user.role == ROLE_RESELLER else None, "csrf_token": s.csrf_token if s else None,
            "must_change_password": user.must_change_password}


@router.get("/plans")
def plans(user: User = Depends(require_user), db: Session = Depends(get_db)) -> list[dict]:
    return [{"id": p.id, "name": p.name, "months": p.months, "credit_cost": p.credit_cost}
            for p in db.scalars(select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.sort_order, Plan.months))]


@router.get("/customers")
def list_customers(q: str = "", state: str = "", page: int = Query(1, ge=1), per_page: int = Query(25, ge=1, le=100),
                   user: User = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    if user.must_change_password:
        raise HTTPException(403, "Change your temporary password first.")
    rows, total = customers.search(db, owner=None if user.role == ROLE_ADMIN else user, q=q, state=state, page=page, per_page=per_page)
    return {"total": total, "page": page, "items": [_c(c) for c in rows]}


@router.get("/customers/{cid}")
def get_customer(cid: int, user: User = Depends(require_user), db: Session = Depends(get_db)) -> CustomerOut:
    return _c(_customer_for(db, user, cid))


@router.post("/customers", status_code=201, dependencies=[Depends(csrf_header)])
def create_customer(body: CustomerIn, request: Request, idempotency_key: str | None = Header(None, max_length=80),
                    user: User = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    if user.must_change_password:
        raise HTTPException(403, "Change your temporary password first.")
    rid = (body.reseller_id or 0) if user.role == ROLE_ADMIN else user.id
    if user.role == ROLE_ADMIN:
        r = db.get(User, rid)
        if r is None or r.role != ROLE_RESELLER:
            raise HTTPException(400, "reseller_id is required for admins.")
    try:
        res = customers.create_customer(db, actor=user, reseller_id=rid, full_name=body.full_name, plan_id=body.plan_id,
                                        reference=body.reference, email=body.email, phone=body.phone, notes=body.notes,
                                        key=idempotency_key)
    except WalletError as e:
        raise _err(e) from None
    if not res.replayed:
        audit(db, user, "customer.create", "customer", res.customer.id, client_ip(request), ref=res.tx.ref, via="api")
        db.commit()
    return {"customer": _c(res.customer), "transaction": _t(res.tx), "replayed": res.replayed}


@router.post("/customers/{cid}/renew", dependencies=[Depends(csrf_header)])
def renew_customer(cid: int, body: RenewIn, request: Request, idempotency_key: str | None = Header(None, max_length=80),
                   user: User = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    if user.must_change_password:
        raise HTTPException(403, "Change your temporary password first.")
    c = _customer_for(db, user, cid)
    try:
        res = customers.renew(db, actor=user, customer=c, plan_id=body.plan_id, key=idempotency_key)
    except WalletError as e:
        raise _err(e) from None
    if not res.replayed:
        audit(db, user, "customer.renew", "customer", cid, client_ip(request), ref=res.tx.ref, via="api")
        db.commit()
    return {"customer": _c(c), "transaction": _t(res.tx), "replayed": res.replayed}


@router.post("/customers/{cid}/status", dependencies=[Depends(csrf_header)])
def customer_status(cid: int, body: StatusIn, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)) -> CustomerOut:
    c = _customer_for(db, user, cid)
    try:
        customers.set_status(db, actor=user, customer=c, status=body.status)
    except WalletError as e:
        raise _err(e) from None
    audit(db, user, f"customer.{body.status}", "customer", cid, client_ip(request), via="api")
    db.commit()
    return _c(c)


@router.get("/transactions")
def list_transactions(page: int = Query(1, ge=1), per_page: int = Query(25, ge=1, le=100), user: User = Depends(require_user),
                      db: Session = Depends(get_db)) -> dict:
    stmt = select(Transaction)
    if user.role != ROLE_ADMIN:
        stmt = stmt.where(Transaction.reseller_id == user.id)
    rows = db.scalars(stmt.order_by(Transaction.id.desc()).offset((page - 1) * per_page).limit(per_page)).all()
    return {"page": page, "items": [_t(t) for t in rows]}


# ── admin only ─────────────────────────────────────────────────────────────
@router.get("/admin/stats")
def admin_stats(admin: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    return stats.admin_overview(db)


@router.get("/admin/resellers")
def admin_resellers(admin: User = Depends(require_admin), db: Session = Depends(get_db)) -> list[dict]:
    return [{"id": r.id, "username": r.username, "display_name": r.display_name, "balance": r.balance, "is_active": r.is_active}
            for r in db.scalars(select(User).where(User.role == ROLE_RESELLER).order_by(User.username))]


@router.post("/admin/resellers", status_code=201, dependencies=[Depends(csrf_header)])
def admin_create_reseller(body: ResellerIn, request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    try:
        r = accounts.create_user(db, username=body.username, password=body.password, role=ROLE_RESELLER,
                                 display_name=body.display_name, email=body.email, phone=body.phone, must_change=True)
    except WalletError as e:
        db.rollback()
        raise _err(e) from None
    audit(db, admin, "reseller.create", "user", r.id, client_ip(request), username=r.username, via="api")
    db.commit()
    return {"id": r.id, "username": r.username}


@router.post("/admin/resellers/{rid}/credits/{op}", dependencies=[Depends(csrf_header)])
def admin_credits(rid: int, op: str, body: CreditIn, request: Request, idempotency_key: str | None = Header(None, max_length=80),
                  admin: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    if op not in {"allocate", "deduct"}:
        raise HTTPException(404, "Not found.")
    try:
        fn = credits.allocate if op == "allocate" else credits.deduct
        res = fn(db, admin=admin, reseller_id=rid, amount=body.amount, note=body.note, key=idempotency_key)
    except WalletError as e:
        raise _err(e) from None
    if not res.replayed:
        audit(db, admin, f"credits.{op}", "user", rid, client_ip(request), amount=body.amount, ref=res.tx.ref, via="api")
        db.commit()
    return {"transaction": _t(res.tx), "replayed": res.replayed}
