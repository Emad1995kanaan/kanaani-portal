"""Fill an EMPTY development database with mock resellers, customers and transactions.

    python -m scripts.seed_demo            # refuses to run when ENVIRONMENT=production
Demo logins are printed at the end (password for all demo resellers: Demo-Pass-2026).
"""
from __future__ import annotations

import random
from datetime import timedelta

from sqlalchemy import func, select

from app.config import settings
from app.db import SessionLocal
from app.models import ROLE_ADMIN, ROLE_RESELLER, Customer, Plan, Subscription, Transaction, User
from app.security import hash_password
from app.services import credits, customers
from app.timeutil import utcnow

FIRST = ["Omar", "Lina", "Sami", "Rana", "Yousef", "Maya", "Karim", "Nour", "Hadi", "Dina", "Fares", "Salma", "Adam", "Huda", "Ziad", "Reem"]
LAST = ["Haddad", "Khalil", "Nasser", "Saleh", "Mansour", "Darwish", "Aziz", "Hamdan", "Yassin", "Jaber"]


def main() -> int:
    if settings.is_production:
        print("Refusing to seed demo data in production.")
        return 1
    rnd = random.Random(7)
    with SessionLocal() as db:
        if db.scalar(select(func.count()).select_from(Customer)):
            print("Database already has customers — demo seed skipped.")
            return 0
        admin = db.scalar(select(User).where(User.role == ROLE_ADMIN))
        if admin is None:
            admin = User(username="admin", role=ROLE_ADMIN, display_name="Demo Admin", password_hash=hash_password("Admin-Pass-2026"))
            db.add(admin)
            db.commit()
            print("Created demo admin: admin / Admin-Pass-2026")
        plans = db.scalars(select(Plan).where(Plan.is_active.is_(True))).all()
        names = [("alpha", "Alpha Distribution"), ("beta", "Beta Partners"), ("gamma", "Gamma Store"), ("delta", "Delta Sales")]
        for uname, dname in names:
            r = User(username=uname, role=ROLE_RESELLER, display_name=dname, password_hash=hash_password("Demo-Pass-2026"),
                     phone=f"+000 5{rnd.randint(1000000, 9999999)}")
            db.add(r)
            db.commit()
            credits.allocate(db, admin=admin, reseller_id=r.id, amount=rnd.choice([120, 160, 200]), note="Opening balance")
            for _ in range(rnd.randint(8, 16)):
                plan = rnd.choice(plans)
                try:
                    res = customers.create_customer(db, actor=r, reseller_id=r.id, full_name=f"{rnd.choice(FIRST)} {rnd.choice(LAST)}",
                                                    plan_id=plan.id, phone=f"+000 7{rnd.randint(1000000, 9999999)}")
                except credits.InsufficientCredits:
                    break
                # spread history over the last ~5 months so charts and expiry states look realistic
                back = timedelta(days=rnd.randint(0, 150), hours=rnd.randint(0, 23))
                c, sub, tx = res.customer, res.subscription, res.tx
                for obj in (c, sub, tx):
                    obj.created_at = utcnow() - back
                sub.starts_at -= back
                sub.expires_at -= back
                c.expires_at = sub.expires_at
                db.commit()
                if rnd.random() < .3:
                    try:
                        customers.renew(db, actor=r, customer=c, plan_id=rnd.choice(plans).id)
                    except credits.InsufficientCredits:
                        pass
                if rnd.random() < .08:
                    customers.set_status(db, actor=r, customer=c, status="suspended")
        db.commit()
        n_tx = db.scalar(select(func.count()).select_from(Transaction))
        n_sub = db.scalar(select(func.count()).select_from(Subscription))
    print(f"Demo data ready: {len(names)} resellers, {n_sub} subscriptions, {n_tx} transactions.")
    print("Reseller logins: alpha / beta / gamma / delta  —  password Demo-Pass-2026")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
