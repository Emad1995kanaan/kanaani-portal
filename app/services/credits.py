"""Wallet operations.

Every balance change is one database transaction that does two things together:
  1. a *conditional* UPDATE (``balance + delta >= 0``) — the database itself refuses to go negative,
     so two concurrent requests can never both spend the same credit;
  2. an INSERT into the ledger with a unique reference and an optional idempotency key —
     replaying the same request (double click, network retry) returns the first result instead of charging twice.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import ROLE_RESELLER, TX_ALLOCATE, TX_CHARGE, TX_DEDUCT, TX_REFUND, Transaction, User
from ..timeutil import utcnow


class WalletError(Exception):
    """A business-rule refusal (insufficient credits, wrong account…) with a user-facing message."""


class InsufficientCredits(WalletError):
    pass


@dataclass
class TxResult:
    tx: Transaction
    replayed: bool = False


def new_ref() -> str:
    return "TX-" + utcnow().strftime("%y%m%d") + "-" + secrets.token_hex(4).upper()


def find_by_key(db: Session, key: str | None) -> Transaction | None:
    if not key:
        return None
    return db.scalar(select(Transaction).where(Transaction.idempotency_key == key))


def apply_delta(db: Session, *, reseller_id: int, delta: int, kind: str, actor: User, key: str | None = None,
                note: str = "", customer_id: int | None = None, subscription_id: int | None = None,
                refund_of_id: int | None = None) -> Transaction:
    """Change a balance and write the ledger row. Runs inside the caller's transaction (no commit here)."""
    res = db.execute(
        update(User)
        .where(User.id == reseller_id, User.role == ROLE_RESELLER, User.balance + delta >= 0)
        .values(balance=User.balance + delta)
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:
        exists = db.scalar(select(User.id).where(User.id == reseller_id, User.role == ROLE_RESELLER))
        if exists is None:
            raise WalletError("Reseller not found.")
        raise InsufficientCredits("Not enough credits for this operation.")
    for obj in list(db.identity_map.values()):  # loaded copies must not show the old balance
        if isinstance(obj, User) and obj.id == reseller_id:
            db.expire(obj, ["balance"])
    balance = db.scalar(select(User.balance).where(User.id == reseller_id))
    tx = Transaction(
        ref=new_ref(), idempotency_key=key or None, kind=kind, reseller_id=reseller_id, amount=delta,
        balance_after=int(balance or 0), customer_id=customer_id, subscription_id=subscription_id,
        refund_of_id=refund_of_id, actor_id=actor.id, note=note[:255],
    )
    db.add(tx)
    db.flush()  # raises IntegrityError on a duplicate idempotency key → whole transaction rolls back
    return tx


def _run(db: Session, key: str | None, fn) -> TxResult:  # noqa: ANN001
    prior = find_by_key(db, key)
    if prior is not None:
        return TxResult(prior, replayed=True)
    try:
        tx = fn()
        db.commit()
        return TxResult(tx)
    except IntegrityError:
        db.rollback()
        prior = find_by_key(db, key)
        if prior is not None:  # a concurrent duplicate won the race
            return TxResult(prior, replayed=True)
        raise
    except Exception:
        db.rollback()
        raise


def allocate(db: Session, *, admin: User, reseller_id: int, amount: int, note: str = "", key: str | None = None) -> TxResult:
    if amount <= 0:
        raise WalletError("Amount must be a positive whole number.")
    return _run(db, key, lambda: apply_delta(db, reseller_id=reseller_id, delta=amount, kind=TX_ALLOCATE,
                                             actor=admin, key=key, note=note))


def deduct(db: Session, *, admin: User, reseller_id: int, amount: int, note: str = "", key: str | None = None) -> TxResult:
    if amount <= 0:
        raise WalletError("Amount must be a positive whole number.")
    return _run(db, key, lambda: apply_delta(db, reseller_id=reseller_id, delta=-amount, kind=TX_DEDUCT,
                                             actor=admin, key=key, note=note))


def refund_charge(db: Session, *, admin: User, tx_id: int, note: str = "") -> TxResult:
    """Reverse a subscription charge: credits go back and the purchased period is removed from the customer."""
    from .customers import void_subscription  # local import: avoids a cycle

    charge = db.get(Transaction, tx_id)
    if charge is None or charge.kind != TX_CHARGE:
        raise WalletError("Only subscription charges can be refunded.")
    existing = db.scalar(select(Transaction).where(Transaction.refund_of_id == charge.id))
    if existing is not None:
        return TxResult(existing, replayed=True)

    def work() -> Transaction:
        if charge.subscription_id:
            void_subscription(db, charge.subscription_id)
        return apply_delta(db, reseller_id=charge.reseller_id, delta=-charge.amount, kind=TX_REFUND, actor=admin,
                           key=f"refund:{charge.id}", note=note or f"Refund of {charge.ref}",
                           customer_id=charge.customer_id, subscription_id=charge.subscription_id,
                           refund_of_id=charge.id)

    return _run(db, f"refund:{charge.id}", work)
