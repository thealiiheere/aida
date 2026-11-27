from dataclasses import asdict

from fastapi import APIRouter
from sqlalchemy.orm import Session

from ..deps import Db, LimitsDep, TelegramDep, api_error, deliver, get_order
from ..lifecycle import change_status
from ..models import Invoice, Order
from ..schemas import ErrorOut, EscalateIn, EscalateOut, NotifyOrderIn, NotifyOrderOut
from ..telegram import call_message, escalation_message, order_message

router = APIRouter(prefix="/api/admin", tags=["Admin"])


def _pick_invoice(order: Order, payment_link: str | None) -> Invoice:
    """The paid invoice, else the one the agent named, else the newest."""
    if not order.invoices:
        raise api_error(
            409, "invoice_missing", f"Order #{order.id} has no invoice yet. Create one first."
        )
    for invoice in order.invoices:
        if invoice.status == "paid":
            return invoice
    for invoice in order.invoices:
        if payment_link and invoice.payment_url == payment_link.strip():
            return invoice
    return order.invoices[-1]


@router.post(
    "/notify-order",
    responses={
        404: {"model": ErrorOut},
        409: {"model": ErrorOut},
        429: {"model": ErrorOut},
        502: {"model": ErrorOut},
    },
)
def notify_order(
    body: NotifyOrderIn, db: Db, telegram: TelegramDep, limits: LimitsDep
) -> NotifyOrderOut:
    """Send an order to the store admin on Telegram. Tool: `notify_admin_order`.

    With `call` instead of `order_id`, sends a finished call's report: its orders
    and transcript.
    """
    if body.call:
        # A missing order must not cost the admin the rest of the report.
        orders = [order for i in body.call.order_ids if (order := db.get(Order, i))]
        report = call_message(body.call, orders, telegram.language)
        return NotifyOrderOut(**asdict(deliver(telegram, *report)))
    order = get_order(db, body.order_id)
    limits.check(order.customer_telegram_id)
    if order.status == "cancelled_expired":
        raise api_error(409, "order_expired", f"Order #{order.id} was not paid in time.")
    invoice = _pick_invoice(order, body.payment_link)
    message = order_message(order, invoice, body.customer_summary, telegram.language)
    delivery = deliver(telegram, message)
    return NotifyOrderOut(
        order_id=order.id, payment_link=invoice.payment_url, **asdict(delivery)
    )


def _order_concerned(db: Session, body: EscalateIn) -> Order | None:
    if body.order_id is None:
        return None
    order = get_order(db, body.order_id)
    # A caller may name any number. Someone else's order stays out of it, but the
    # caller still gets a person.
    identified = body.customer_telegram_id or body.customer_phone
    if identified and order.customer_telegram_id != body.customer_telegram_id:
        return None
    return order


@router.post(
    "/escalate",
    responses={404: {"model": ErrorOut}, 429: {"model": ErrorOut}, 502: {"model": ErrorOut}},
)
def escalate(
    body: EscalateIn, db: Db, telegram: TelegramDep, limits: LimitsDep
) -> EscalateOut:
    """Tag the admin with a call summary. Tool: `escalate_to_human`.

    A pending order becomes "escalated", which keeps its stock held until a person
    decides. An order that was paid or has expired stays as it is.
    """
    order = _order_concerned(db, body)
    limits.check(body.customer_telegram_id or (order and order.customer_telegram_id))
    if order and change_status(db, order.id, ("pending",), "escalated"):
        db.commit()
        db.refresh(order)
    message = escalation_message(
        admin_tag=telegram.admin_tag,
        reason=body.reason,
        summary=body.context_summary,
        order=order,
        name=body.customer_name,
        username=body.customer_telegram_username,
        telegram_id=body.customer_telegram_id,
        phone=body.customer_phone,
        last_spoken=body.last_spoken,
        language=telegram.language,
    )
    delivery = deliver(telegram, message)
    return EscalateOut(
        order_id=order.id if order else None,
        order_status=order.status if order else None,
        **asdict(delivery),
    )
