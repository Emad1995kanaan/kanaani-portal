from __future__ import annotations

import csv
import io
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import csrf_form, require_admin
from ..models import ROLE_RESELLER, TX_CHARGE, AuditLog, Customer, Plan, Transaction, User
from ..security import audit, client_ip, generate_password
from ..services import accounts, credits, customers, stats
from ..services.credits import WalletError
from ..timeutil import utcnow
from ..web import back, page, to_int

router = APIRouter(prefix="/admin", include_in_schema=False)
PER_PAGE = 25


def _reseller(db: Session, rid: int) -> User:
    r = db.get(User, rid)
    if r is None or r.role != ROLE_RESELLER:
        raise HTTPException(404)
    return r


def _customer(db: Session, cid: int) -> Customer:
    c = db.get(Customer, cid)
    if c is None:
        raise HTTPException(404)
    return c


def _plans(db: Session, active_only: bool = True) -> list[Plan]:
    q = select(Plan).order_by(Plan.sort_order, Plan.months)
    if active_only:
        q = q.where(Plan.is_active.is_(True))
    return list(db.scalars(q).all())


# ── dashboard ──────────────────────────────────────────────────────────────
@router.get("")
def dashboard(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):  # noqa: ANN201
    recent = db.scalars(select(Transaction).order_by(Transaction.id.desc()).limit(10)).all()
    return page(request, "admin/dashboard.html", s=stats.admin_overview(db), recent=recent)


# ── resellers ──────────────────────────────────────────────────────────────
@router.get("/resellers")
def resellers(request: Request, q: str = "", admin: User = Depends(require_admin), db: Session = Depends(get_db)):  # noqa: ANN201
    stmt = select(User, func.count(Customer.id)).outerjoin(Customer, Customer.reseller_id == User.id).where(User.role == ROLE_RESELLER)
    if q.strip():
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(func.lower(User.username).like(like) | func.lower(User.display_name).like(like))
    rows = db.execute(stmt.group_by(User.id).order_by(User.created_at.desc())).all()
    return page(request, "admin/resellers.html", rows=rows, q=q, suggested=generate_password())


@router.post("/resellers", dependencies=[Depends(csrf_form)])
def create_reseller(request: Request, username: str = Form(""), password: str = Form(""), display_name: str = Form(""),
                    email: str = Form(""), phone: str = Form(""), credits_start: str = Form("0"), key: str = Form(""),
                    admin: User = Depends(require_admin), db: Session = Depends(get_db)):  # noqa: ANN201
    try:
        r = accounts.create_user(db, username=username, password=password, role=ROLE_RESELLER, display_name=display_name,
                                 email=email, phone=phone, must_change=True)
        audit(db, admin, "reseller.create", "user", r.id, client_ip(request), username=r.username)
        db.commit()
    except WalletError as e:
        db.rollback()
        return back("/admin/resellers", err=str(e))
    start = to_int(credits_start)
    if start > 0:
        credits.allocate(db, admin=admin, reseller_id=r.id, amount=start, note="Opening balance", key=key or None)
        audit(db, admin, "credits.allocate", "user", r.id, client_ip(request), amount=start)
        db.commit()
    return back(f"/admin/resellers/{r.id}", msg=f"Reseller {r.username} created. They must change the password at first sign-in.")


@router.get("/resellers/{rid}")
def reseller_detail(request: Request, rid: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):  # noqa: ANN201
    r = _reseller(db, rid)
    txs = db.scalars(select(Transaction).where(Transaction.reseller_id == rid).order_by(Transaction.id.desc()).limit(15)).all()
    activity = db.scalars(select(AuditLog).where(AuditLog.actor_id == rid).order_by(AuditLog.id.desc()).limit(15)).all()
    custs, total = customers.search(db, owner=None, reseller_id=rid, per_page=10)
    return page(request, "admin/reseller_detail.html", r=r, ov=stats.reseller_overview(db, r), txs=txs, activity=activity,
                custs=custs, total=total, suggested=generate_password())


@router.post("/resellers/{rid}/edit", dependencies=[Depends(csrf_form)])
def edit_reseller(request: Request, rid: int, display_name: str = Form(""), email: str = Form(""), phone: str = Form(""),
                  admin: User = Depends(require_admin), db: Session = Depends(get_db)):  # noqa: ANN201
    r = _reseller(db, rid)
    accounts.update_reseller(db, r, display_name=display_name, email=email, phone=phone)
    audit(db, admin, "reseller.edit", "user", rid, client_ip(request))
    db.commit()
    return back(f"/admin/resellers/{rid}", msg="Saved.")


@router.post("/resellers/{rid}/status", dependencies=[Depends(csrf_form)])
def reseller_status(request: Request, rid: int, active: str = Form("1"), admin: User = Depends(require_admin), db: Session = Depends(get_db)):  # noqa: ANN201
    r = _reseller(db, rid)
    accounts.set_active(db, r, active == "1")
    audit(db, admin, "reseller.activate" if r.is_active else "reseller.suspend", "user", rid, client_ip(request))
    db.commit()
    return back(f"/admin/resellers/{rid}", msg="Account activated." if r.is_active else "Account suspended and signed out.")


@router.post("/resellers/{rid}/password", dependencies=[Depends(csrf_form)])
def reseller_password(request: Request, rid: int, password: str = Form(""), admin: User = Depends(require_admin), db: Session = Depends(get_db)):  # noqa: ANN201
    r = _reseller(db, rid)
    try:
        accounts.set_password(db, r, password, must_change=True)
    except WalletError as e:
        db.rollback()
        return back(f"/admin/resellers/{rid}", err=str(e))
    audit(db, admin, "reseller.password_reset", "user", rid, client_ip(request))
    db.commit()
    return back(f"/admin/resellers/{rid}", msg="Temporary password set. The reseller must change it at next sign-in.")


@router.post("/resellers/{rid}/credits", dependencies=[Depends(csrf_form)])
def reseller_credits(request: Request, rid: int, op: str = Form("allocate"), amount: str = Form("0"), note: str = Form(""),
                     key: str = Form(""), admin: User = Depends(require_admin), db: Session = Depends(get_db)):  # noqa: ANN201
    _reseller(db, rid)
    n = to_int(amount)
    try:
        fn = credits.allocate if op == "allocate" else credits.deduct
        res = fn(db, admin=admin, reseller_id=rid, amount=n, note=note, key=key or None)
    except WalletError as e:
        return back(f"/admin/resellers/{rid}", err=str(e))
    if not res.replayed:
        audit(db, admin, f"credits.{'allocate' if op == 'allocate' else 'deduct'}", "user", rid, client_ip(request), amount=n, ref=res.tx.ref)
        db.commit()
    return back(f"/admin/resellers/{rid}", msg=f"{res.tx.ref}: {'+' if res.tx.amount > 0 else ''}{res.tx.amount} credits. New balance {res.tx.balance_after}."
                + (" (already applied — not charged twice)" if res.replayed else ""))


# ── customers ──────────────────────────────────────────────────────────────
@router.get("/customers")
def all_customers(request: Request, q: str = "", state: str = "", reseller: int = 0, page_no: int = 1,
                  admin: User = Depends(require_admin), db: Session = Depends(get_db)):  # noqa: ANN201
    rows, total = customers.search(db, owner=None, q=q, state=state, reseller_id=reseller or None, page=page_no, per_page=PER_PAGE)
    rs = db.scalars(select(User).where(User.role == ROLE_RESELLER).order_by(User.username)).all()
    return page(request, "customers.html", rows=rows, total=total, q=q, state=state, reseller=reseller, page_no=page_no,
                per_page=PER_PAGE, resellers=rs, plans=_plans(db), base="/admin/customers", admin_view=True)


@router.post("/customers", dependencies=[Depends(csrf_form)])
def admin_create_customer(request: Request, reseller_id: str = Form("0"), full_name: str = Form(""), reference: str = Form(""),
                          email: str = Form(""), phone: str = Form(""), notes: str = Form(""), plan_id: str = Form("0"),
                          key: str = Form(""), admin: User = Depends(require_admin), db: Session = Depends(get_db)):  # noqa: ANN201
    rid = to_int(reseller_id)
    try:
        _reseller(db, rid)
        res = customers.create_customer(db, actor=admin, reseller_id=rid, full_name=full_name, plan_id=to_int(plan_id),
                                        reference=reference, email=email, phone=phone, notes=notes, key=key or None)
    except HTTPException:
        return back("/admin/customers", err="Choose a reseller.")
    except WalletError as e:
        return back("/admin/customers", err=str(e))
    if not res.replayed:
        audit(db, admin, "customer.create", "customer", res.customer.id, client_ip(request), reseller=rid, ref=res.tx.ref)
        db.commit()
    return back(f"/admin/customers/{res.customer.id}", msg=f"Customer created ({res.tx.ref}).")


@router.get("/customers/{cid}")
def admin_customer(request: Request, cid: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):  # noqa: ANN201
    c = _customer(db, cid)
    txs = db.scalars(select(Transaction).where(Transaction.customer_id == cid).order_by(Transaction.id.desc())).all()
    return page(request, "customer_detail.html", c=c, txs=txs, plans=_plans(db), base="/admin/customers", admin_view=True)


@router.post("/customers/{cid}/renew", dependencies=[Depends(csrf_form)])
def admin_renew(request: Request, cid: int, plan_id: str = Form("0"), key: str = Form(""), admin: User = Depends(require_admin), db: Session = Depends(get_db)):  # noqa: ANN201
    c = _customer(db, cid)
    try:
        res = customers.renew(db, actor=admin, customer=c, plan_id=to_int(plan_id), key=key or None)
    except WalletError as e:
        return back(f"/admin/customers/{cid}", err=str(e))
    if not res.replayed:
        audit(db, admin, "customer.renew", "customer", cid, client_ip(request), ref=res.tx.ref)
        db.commit()
    return back(f"/admin/customers/{cid}", msg=f"Renewed ({res.tx.ref}).")


@router.post("/customers/{cid}/status", dependencies=[Depends(csrf_form)])
def admin_customer_status(request: Request, cid: int, status: str = Form("active"), admin: User = Depends(require_admin), db: Session = Depends(get_db)):  # noqa: ANN201
    c = _customer(db, cid)
    try:
        customers.set_status(db, actor=admin, customer=c, status=status)
    except WalletError as e:
        return back(f"/admin/customers/{cid}", err=str(e))
    audit(db, admin, f"customer.{status}", "customer", cid, client_ip(request))
    db.commit()
    return back(f"/admin/customers/{cid}", msg="Status updated.")


@router.post("/customers/{cid}/edit", dependencies=[Depends(csrf_form)])
def admin_customer_edit(request: Request, cid: int, full_name: str = Form(""), email: str = Form(""), phone: str = Form(""),
                        notes: str = Form(""), admin: User = Depends(require_admin), db: Session = Depends(get_db)):  # noqa: ANN201
    c = _customer(db, cid)
    try:
        customers.update_details(db, actor=admin, customer=c, full_name=full_name, email=email, phone=phone, notes=notes)
    except WalletError as e:
        return back(f"/admin/customers/{cid}", err=str(e))
    audit(db, admin, "customer.edit", "customer", cid, client_ip(request))
    db.commit()
    return back(f"/admin/customers/{cid}", msg="Saved.")


# ── plans (prices) ─────────────────────────────────────────────────────────
@router.get("/plans")
def plans(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):  # noqa: ANN201
    return page(request, "admin/plans.html", plans=_plans(db, active_only=False))


@router.post("/plans", dependencies=[Depends(csrf_form)])
def save_plan(request: Request, plan_id: str = Form("0"), name: str = Form(""), months: str = Form("1"), credit_cost: str = Form("1"),
              is_active: str = Form(""), sort_order: str = Form("0"), admin: User = Depends(require_admin), db: Session = Depends(get_db)):  # noqa: ANN201
    m, cost = to_int(months), to_int(credit_cost, -1)
    name = name.strip()[:80]
    if not name or not 1 <= m <= 120 or not 0 <= cost <= 100000:
        return back("/admin/plans", err="Name is required, months 1–120, credits 0–100000.")
    pid = to_int(plan_id)
    p = db.get(Plan, pid) if pid else Plan()
    if p is None:
        raise HTTPException(404)
    before = {"name": p.name, "months": p.months, "credit_cost": p.credit_cost, "active": p.is_active} if pid else None
    p.name, p.months, p.credit_cost, p.is_active, p.sort_order = name, m, cost, is_active == "1", to_int(sort_order)
    if not pid:
        db.add(p)
    db.flush()
    audit(db, admin, "plan.update" if pid else "plan.create", "plan", p.id, client_ip(request), before=before,
          after={"name": name, "months": m, "credit_cost": cost, "active": p.is_active})
    db.commit()
    return back("/admin/plans", msg="Plan saved.")


# ── transactions & reports ─────────────────────────────────────────────────
@router.get("/transactions")
def transactions(request: Request, kind: str = "", reseller: int = 0, q: str = "", page_no: int = 1,
                 admin: User = Depends(require_admin), db: Session = Depends(get_db)):  # noqa: ANN201
    stmt = select(Transaction)
    if kind:
        stmt = stmt.where(Transaction.kind == kind)
    if reseller:
        stmt = stmt.where(Transaction.reseller_id == reseller)
    if q.strip():
        stmt = stmt.where(Transaction.ref.ilike(f"%{q.strip()}%"))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(Transaction.id.desc()).offset((max(page_no, 1) - 1) * PER_PAGE).limit(PER_PAGE)).all()
    refunded = set(db.scalars(select(Transaction.refund_of_id).where(Transaction.refund_of_id.is_not(None))).all())
    rs = db.scalars(select(User).where(User.role == ROLE_RESELLER).order_by(User.username)).all()
    return page(request, "transactions.html", rows=rows, total=total, kind=kind, reseller=reseller, q=q, page_no=page_no,
                per_page=PER_PAGE, resellers=rs, refunded=refunded, admin_view=True, base="/admin/transactions")


@router.post("/transactions/{tid}/refund", dependencies=[Depends(csrf_form)])
def refund(request: Request, tid: int, note: str = Form(""), admin: User = Depends(require_admin), db: Session = Depends(get_db)):  # noqa: ANN201
    try:
        res = credits.refund_charge(db, admin=admin, tx_id=tid, note=note)
    except WalletError as e:
        return back("/admin/transactions", err=str(e))
    if not res.replayed:
        audit(db, admin, "credits.refund", "transaction", tid, client_ip(request), ref=res.tx.ref, amount=res.tx.amount)
        db.commit()
    return back("/admin/transactions", msg=f"Refunded: {res.tx.ref} (+{res.tx.amount} credits)" + (" — already refunded." if res.replayed else "."))


@router.get("/reports")
def reports(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):  # noqa: ANN201
    since = utcnow() - timedelta(days=365)
    month = func.strftime("%Y-%m", Transaction.created_at) if db.bind.dialect.name == "sqlite" else func.to_char(Transaction.created_at, "YYYY-MM")
    monthly = db.execute(
        select(month, Transaction.kind, func.sum(Transaction.amount), func.count())
        .where(Transaction.created_at >= since).group_by(month, Transaction.kind).order_by(month.desc())
    ).all()
    table: dict[str, dict[str, int]] = {}
    for m, k, total, n in monthly:
        row = table.setdefault(m, {"allocate": 0, "deduct": 0, "charge": 0, "refund": 0, "sales": 0})
        row[k] = int(total or 0)
        if k == TX_CHARGE:
            row["sales"] = int(n)
    per_reseller = db.execute(
        select(User.id, User.username, User.display_name, User.balance,
               func.coalesce(func.sum(Transaction.amount).filter(Transaction.kind == "allocate"), 0),
               func.coalesce(func.sum(Transaction.amount).filter(Transaction.kind.in_(["charge", "refund"])), 0),
               func.count(Transaction.id).filter(Transaction.kind == "charge"))
        .outerjoin(Transaction, Transaction.reseller_id == User.id).where(User.role == ROLE_RESELLER)
        .group_by(User.id).order_by(User.username)
    ).all()
    return page(request, "admin/reports.html", monthly=table, per_reseller=per_reseller)


@router.get("/transactions.csv")
def transactions_csv(admin: User = Depends(require_admin), db: Session = Depends(get_db)):  # noqa: ANN201
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ref", "date_utc", "kind", "reseller", "amount", "balance_after", "customer", "actor", "note"])
    for t in db.scalars(select(Transaction).order_by(Transaction.id)).yield_per(500):
        w.writerow([t.ref, t.created_at.isoformat(), t.kind, t.reseller.username, t.amount, t.balance_after,
                    t.customer.reference if t.customer else "", t.actor.username, _csv_safe(t.note)])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=transactions.csv"})


def _csv_safe(v: str) -> str:
    """Stop spreadsheet formula injection."""
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


@router.get("/audit")
def audit_log(request: Request, action: str = "", page_no: int = 1, admin: User = Depends(require_admin), db: Session = Depends(get_db)):  # noqa: ANN201
    stmt = select(AuditLog)
    if action:
        stmt = stmt.where(AuditLog.action.like(f"{action}%"))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(AuditLog.id.desc()).offset((max(page_no, 1) - 1) * 50).limit(50)).all()
    return page(request, "admin/audit.html", rows=rows, total=total, action=action, page_no=page_no, per_page=50)
