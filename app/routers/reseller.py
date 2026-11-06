from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import csrf_form, require_reseller
from ..models import Customer, Plan, Subscription, Transaction, User
from ..security import audit, client_ip
from ..services import customers, stats
from ..services.credits import WalletError
from ..web import back, page, to_int

router = APIRouter(prefix="/r", include_in_schema=False)
PER_PAGE = 25


def _own(db: Session, me: User, cid: int) -> Customer:
    c = db.get(Customer, cid)
    if c is None or c.reseller_id != me.id:  # other resellers' customers look like "not found"
        raise HTTPException(404)
    return c


def _plans(db: Session) -> list[Plan]:
    return list(db.scalars(select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.sort_order, Plan.months)).all())


@router.get("")
def dashboard(request: Request, me: User = Depends(require_reseller), db: Session = Depends(get_db)):  # noqa: ANN201
    expiring, _ = customers.search(db, owner=me, state="expiring", per_page=8)
    recent = db.scalars(select(Transaction).where(Transaction.reseller_id == me.id).order_by(Transaction.id.desc()).limit(8)).all()
    return page(request, "reseller/dashboard.html", s=stats.reseller_overview(db, me), expiring=expiring, recent=recent)


@router.get("/customers")
def my_customers(request: Request, q: str = "", state: str = "", page_no: int = 1, me: User = Depends(require_reseller), db: Session = Depends(get_db)):  # noqa: ANN201
    rows, total = customers.search(db, owner=me, q=q, state=state, page=page_no, per_page=PER_PAGE)
    return page(request, "customers.html", rows=rows, total=total, q=q, state=state, page_no=page_no, per_page=PER_PAGE,
                plans=_plans(db), base="/r/customers", admin_view=False, reseller=0, resellers=[])


@router.post("/customers", dependencies=[Depends(csrf_form)])
def create(request: Request, full_name: str = Form(""), reference: str = Form(""), email: str = Form(""), phone: str = Form(""),
           notes: str = Form(""), plan_id: str = Form("0"), key: str = Form(""), me: User = Depends(require_reseller), db: Session = Depends(get_db)):  # noqa: ANN201
    try:
        res = customers.create_customer(db, actor=me, reseller_id=me.id, full_name=full_name, plan_id=to_int(plan_id),
                                        reference=reference, email=email, phone=phone, notes=notes, key=key or None)
    except WalletError as e:
        return back("/r/customers", err=str(e))
    if not res.replayed:
        audit(db, me, "customer.create", "customer", res.customer.id, client_ip(request), ref=res.tx.ref)
        db.commit()
    return back(f"/r/customers/{res.customer.id}", msg=f"Customer created. {abs(res.tx.amount)} credits used ({res.tx.ref}).")


@router.get("/customers/{cid}")
def detail(request: Request, cid: int, me: User = Depends(require_reseller), db: Session = Depends(get_db)):  # noqa: ANN201
    c = _own(db, me, cid)
    txs = db.scalars(select(Transaction).where(Transaction.customer_id == cid, Transaction.reseller_id == me.id).order_by(Transaction.id.desc())).all()
    return page(request, "customer_detail.html", c=c, txs=txs, plans=_plans(db), base="/r/customers", admin_view=False)


@router.post("/customers/{cid}/renew", dependencies=[Depends(csrf_form)])
def renew(request: Request, cid: int, plan_id: str = Form("0"), key: str = Form(""), me: User = Depends(require_reseller), db: Session = Depends(get_db)):  # noqa: ANN201
    c = _own(db, me, cid)
    try:
        res = customers.renew(db, actor=me, customer=c, plan_id=to_int(plan_id), key=key or None)
    except WalletError as e:
        return back(f"/r/customers/{cid}", err=str(e))
    if not res.replayed:
        audit(db, me, "customer.renew", "customer", cid, client_ip(request), ref=res.tx.ref)
        db.commit()
    return back(f"/r/customers/{cid}", msg=f"Renewed. {abs(res.tx.amount)} credits used ({res.tx.ref}).")


@router.post("/customers/{cid}/status", dependencies=[Depends(csrf_form)])
def status(request: Request, cid: int, status: str = Form("active"), me: User = Depends(require_reseller), db: Session = Depends(get_db)):  # noqa: ANN201
    c = _own(db, me, cid)
    try:
        customers.set_status(db, actor=me, customer=c, status=status)
    except WalletError as e:
        return back(f"/r/customers/{cid}", err=str(e))
    audit(db, me, f"customer.{status}", "customer", cid, client_ip(request))
    db.commit()
    return back(f"/r/customers/{cid}", msg="Status updated.")


@router.post("/customers/{cid}/edit", dependencies=[Depends(csrf_form)])
def edit(request: Request, cid: int, full_name: str = Form(""), email: str = Form(""), phone: str = Form(""), notes: str = Form(""),
         me: User = Depends(require_reseller), db: Session = Depends(get_db)):  # noqa: ANN201
    c = _own(db, me, cid)
    try:
        customers.update_details(db, actor=me, customer=c, full_name=full_name, email=email, phone=phone, notes=notes)
    except WalletError as e:
        return back(f"/r/customers/{cid}", err=str(e))
    audit(db, me, "customer.edit", "customer", cid, client_ip(request))
    db.commit()
    return back(f"/r/customers/{cid}", msg="Saved.")


@router.get("/transactions")
def my_transactions(request: Request, kind: str = "", q: str = "", page_no: int = 1, me: User = Depends(require_reseller), db: Session = Depends(get_db)):  # noqa: ANN201
    stmt = select(Transaction).where(Transaction.reseller_id == me.id)
    if kind:
        stmt = stmt.where(Transaction.kind == kind)
    if q.strip():
        stmt = stmt.where(Transaction.ref.ilike(f"%{q.strip()}%"))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(Transaction.id.desc()).offset((max(page_no, 1) - 1) * PER_PAGE).limit(PER_PAGE)).all()
    return page(request, "transactions.html", rows=rows, total=total, kind=kind, q=q, page_no=page_no, per_page=PER_PAGE,
                admin_view=False, refunded=set(), resellers=[], reseller=0, base="/r/transactions")


@router.get("/subscriptions")
def my_subscriptions(request: Request, page_no: int = 1, me: User = Depends(require_reseller), db: Session = Depends(get_db)):  # noqa: ANN201
    stmt = select(Subscription).join(Customer).where(Customer.reseller_id == me.id)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(Subscription.id.desc()).offset((max(page_no, 1) - 1) * PER_PAGE).limit(PER_PAGE)).all()
    return page(request, "reseller/subscriptions.html", rows=rows, total=total, page_no=page_no, per_page=PER_PAGE)


@router.get("/settings")
def settings_page(request: Request, me: User = Depends(require_reseller)):  # noqa: ANN201
    return page(request, "reseller/settings.html")


@router.post("/settings", dependencies=[Depends(csrf_form)])
def save_settings(request: Request, display_name: str = Form(""), email: str = Form(""), phone: str = Form(""),
                  me: User = Depends(require_reseller), db: Session = Depends(get_db)):  # noqa: ANN201
    me.display_name, me.email, me.phone = display_name.strip()[:120], email.strip()[:160], phone.strip()[:40]
    audit(db, me, "profile.edit", "user", me.id, client_ip(request))
    db.commit()
    return back("/r/settings", msg="Saved.")
