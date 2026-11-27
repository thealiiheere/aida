"""How an order's status changes after it is created: paid, or expired unpaid.

Every change is a conditional UPDATE, so when a payment, a retry and the expiry worker
meet, exactly one of them wins.
"""

import asyncio
import logging
from collections import Counter
from collections.abc import Iterator
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from .models import Invoice, Order, OrderItem, Product, utcnow
from .telegram import TelegramNotifier, expired_message

log = logging.getLogger("aida.orders")

HOLD = timedelta(minutes=15)
EXPIRY_CHECK_SECONDS = 60

Payment = Literal["paid", "already_paid", "out_of_stock"]


def reserved_until(order: Order) -> datetime | None:
    return order.created_at + HOLD if order.status == "pending" else None


def take_stock(db: Session, wanted: dict[int, int]) -> list[int]:
    """Take items off the shelf. Returns the products that are short: if any, roll back."""
    short = []
    for product_id, quantity in sorted(wanted.items()):
        # Check and decrement in one statement: two callers can't both take the last one.
        result = db.execute(
            update(Product)
            .where(Product.id == product_id, Product.stock_quantity >= quantity)
            .values(stock_quantity=Product.stock_quantity - quantity)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            short.append(product_id)
    return short


def change_status(db: Session, order_id: int, allowed: tuple[str, ...], new: str) -> bool:
    """False if the order is no longer in one of the allowed statuses."""
    result = db.execute(
        update(Order)
        .where(Order.id == order_id, Order.status.in_(allowed))
        .values(status=new)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def _set_items(db: Session, order_id: int, state: str) -> None:
    db.execute(
        update(OrderItem)
        .where(OrderItem.order_id == order_id)
        .values(state=state)
        .execution_options(synchronize_session=False)
    )


def mark_paid(db: Session, order_id: int, provider: str) -> Payment:
    """Record a payment. Only the first one for an order returns "paid". Doesn't commit.

    Stock was taken when the order was created, so it isn't taken a second time here,
    unless the hold ran out meanwhile and it went back on the shelf.
    """
    if not change_status(db, order_id, ("pending", "escalated"), "paid"):
        if not change_status(db, order_id, ("cancelled_expired",), "paid"):
            return "already_paid"
        items = db.execute(
            select(OrderItem.product_id, OrderItem.quantity).where(OrderItem.order_id == order_id)
        )
        wanted = Counter()
        for product_id, quantity in items:
            wanted[product_id] += quantity
        if take_stock(db, wanted):
            db.rollback()
            return "out_of_stock"
    _set_items(db, order_id, "sold")
    db.execute(
        update(Invoice)
        .where(Invoice.order_id == order_id, Invoice.provider == provider)
        .values(status="paid")
        .execution_options(synchronize_session=False)
    )
    return "paid"


def expire_orders(db: Session, now: datetime) -> Iterator[Order]:
    """Cancel unpaid orders whose hold ran out, and put their stock back."""
    stale = select(Order).where(Order.status == "pending", Order.created_at < now - HOLD)
    for order in db.scalars(stale.order_by(Order.id)).all():
        if not change_status(db, order.id, ("pending",), "cancelled_expired"):
            continue  # paid or escalated a moment ago
        for item in order.items:
            db.execute(
                update(Product)
                .where(Product.id == item.product_id)
                .values(stock_quantity=Product.stock_quantity + item.quantity)
                .execution_options(synchronize_session=False)
            )
        _set_items(db, order.id, "released")
        db.commit()
        yield order


def expire_and_tell(engine: Engine, telegram: TelegramNotifier, now: datetime) -> None:
    expired = []
    with Session(engine) as db:
        try:
            for order in expire_orders(db, now):
                log.info("order #%d expired unpaid, its stock is back on the shelf", order.id)
                expired.append(order)
        finally:
            # All stock first: a slow Telegram mustn't keep items off the shelf.
            for order in expired:
                language = telegram.customer_language(order.customer_telegram_id)
                telegram.tell_customer(
                    order.customer_telegram_id, expired_message(order, language)
                )


async def expiry_worker(engine: Engine, telegram: TelegramNotifier, stop: asyncio.Event) -> None:
    """Runs for as long as the server does."""
    while not stop.is_set():
        try:
            await asyncio.to_thread(expire_and_tell, engine, telegram, utcnow())
        except Exception:
            log.exception("expiring orders failed, trying again in a minute")
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), EXPIRY_CHECK_SECONDS)
