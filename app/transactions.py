"""Payment attempts at Click and Payme: the parts both webhooks share."""

import time

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import PaymentTransaction


def now_ms() -> int:
    return int(time.time() * 1000)


def whole_number(value) -> int | None:
    """An id that arrived as text or a number. None if it can't be one."""
    text = str(value).strip()
    return int(text) if text.isascii() and text.isdigit() and len(text) <= 15 else None


def find(db: Session, provider: str, external_id: str) -> PaymentTransaction | None:
    return db.scalar(
        select(PaymentTransaction).where(
            PaymentTransaction.provider == provider,
            PaymentTransaction.external_id == external_id,
        )
    )


def start(
    db: Session,
    provider: str,
    external_id: str,
    order_id: int,
    amount_tiyin: int,
    requested_ms: int | None = None,
) -> PaymentTransaction:
    now = now_ms()
    transaction = PaymentTransaction(
        provider=provider,
        external_id=external_id,
        order_id=order_id,
        amount_tiyin=amount_tiyin,
        requested_ms=now if requested_ms is None else requested_ms,
        created_ms=now,
    )
    db.add(transaction)
    try:
        db.commit()
    except IntegrityError:
        # The same request arrived twice at once; both get the first one's answer.
        db.rollback()
        transaction = find(db, provider, external_id)
    return transaction


def move(
    db: Session, transaction: PaymentTransaction, new: str, reason: int | None = None
) -> bool:
    """Settle an attempt as paid or cancelled. False if it already was. Doesn't commit."""
    result = db.execute(
        update(PaymentTransaction)
        .where(PaymentTransaction.id == transaction.id, PaymentTransaction.state == "created")
        .values({"state": new, "reason": reason, f"{new}_ms": now_ms()})
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1
