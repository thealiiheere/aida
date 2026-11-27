"""Click's SHOP-API callbacks: Prepare before the customer is charged, Complete after.

Field order in the signature, the error codes and the order of checks follow Click's
own integration library, github.com/click-llc/click-integration-django (click/utils.py).
"""

import hashlib
import hmac
import logging
from decimal import Decimal, InvalidOperation
from typing import Annotated
from urllib.parse import parse_qsl

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlalchemy.orm import Session

from ..config import Settings
from ..deps import Db, SettingsDep, TelegramDep, api_error
from ..lifecycle import Payment, mark_paid
from ..models import Order, PaymentTransaction
from ..telegram import payment_refused_message
from ..transactions import find, move, start, whole_number
from .payments import announce_payment

log = logging.getLogger("aida.payments")
router = APIRouter(tags=["Payment webhooks"])

PREPARE, COMPLETE = "0", "1"

NOTES = {
    0: "Success",
    -1: "SIGN CHECK FAILED!",
    -2: "Incorrect parameter amount",
    -3: "Action not found",
    -4: "Already paid",
    -5: "Order does not exist",
    -6: "Transaction not found",
    -8: "Error in request from click",
    -9: "Transaction cancelled",
}


class Refused(Exception):
    def __init__(self, code: int):
        self.code = code


async def _form(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8", "replace")
    return dict(parse_qsl(body, keep_blank_values=True))


def click_signature(form: dict[str, str], secret: str) -> str:
    parts = [
        form.get("click_trans_id", ""),
        form.get("service_id", ""),
        secret,
        form.get("merchant_trans_id", ""),
        form.get("merchant_prepare_id", "") if form.get("action") == COMPLETE else "",
        form.get("amount", ""),  # exactly as sent
        form.get("action", ""),
        form.get("sign_time", ""),
    ]
    return hashlib.md5("".join(parts).encode()).hexdigest()


@router.post("/api/webhooks/click")
def click_webhook(
    form: Annotated[dict, Depends(_form)],
    db: Db,
    settings: SettingsDep,
    telegram: TelegramDep,
    background: BackgroundTasks,
) -> dict:
    """Prepare (action 0) and Complete (action 1). Set both of Click's URLs to this one.

    A repeated Complete answers -4 (already paid) and changes nothing.
    """
    if not (settings.click_secret_key and settings.click_service_id and settings.uzs_per_usd):
        raise api_error(
            503, "click_not_set_up", "Set CLICK_SERVICE_ID, CLICK_SECRET_KEY and UZS_PER_USD."
        )
    number, code = whole_number(form.get("merchant_prepare_id")), 0
    try:
        order = _check(form, db, settings)
        if form["action"] == PREPARE:
            number = _prepare(form, db, settings, order).id
        else:
            result = _complete(form, db, order)
            if result == "out_of_stock":
                refused = payment_refused_message(order, "click", telegram.language)
                background.add_task(telegram.tell_admin, refused)
            if result != "paid":
                raise Refused(-9)
            announce_payment(background, telegram, order, "click")
    except Refused as refused:
        code = refused.code
        log.warning(
            "Click payment for order %r refused: %s", form.get("merchant_trans_id"), NOTES[code]
        )
    return {
        "click_trans_id": form.get("click_trans_id"),
        "merchant_trans_id": form.get("merchant_trans_id"),
        "merchant_prepare_id": number,
        "merchant_confirm_id": number,
        "error": code,
        "error_note": NOTES[code],
    }


def _check(form: dict[str, str], db: Session, settings: Settings) -> Order:
    expected = click_signature(form, settings.click_secret_key.get_secret_value())
    if not hmac.compare_digest(expected.encode(), form.get("sign_string", "").encode()):
        raise Refused(-1)
    if form.get("service_id") != settings.click_service_id or not form.get("click_trans_id"):
        raise Refused(-8)
    if form.get("action") not in (PREPARE, COMPLETE):
        raise Refused(-3)
    number = whole_number(form.get("merchant_trans_id"))
    order = db.get(Order, number) if number is not None else None
    if order is None:
        raise Refused(-5)
    if not _same_amount(form.get("amount", ""), settings.tiyin(order.total_amount)):
        raise Refused(-2)
    return order


def _same_amount(soum: str, tiyin: int) -> bool:
    try:
        amount = Decimal(soum)
    except InvalidOperation:
        return False
    return amount.is_finite() and amount * 100 == tiyin


def _prepare(
    form: dict[str, str], db: Session, settings: Settings, order: Order
) -> PaymentTransaction:
    if order.status == "paid":
        raise Refused(-4)
    if order.status == "cancelled_expired":
        raise Refused(-9)
    click_id = form["click_trans_id"]
    transaction = find(db, "click", click_id) or start(
        db, "click", click_id, order.id, settings.tiyin(order.total_amount)
    )
    if transaction.order_id != order.id:
        raise Refused(-8)
    if transaction.state == "cancelled":
        raise Refused(-9)
    return transaction


def _complete(form: dict[str, str], db: Session, order: Order) -> Payment:
    """Returns mark_paid's verdict. Anything but "paid" makes Click return the money.

    "Already paid" (-4) is only for this same payment, confirmed before. A second payment
    for a paid order is cancelled (-9), so that the customer isn't charged twice.
    """
    number = whole_number(form.get("merchant_prepare_id"))
    transaction = db.get(PaymentTransaction, number) if number is not None else None
    if (
        transaction is None
        or transaction.provider != "click"
        or transaction.external_id != form["click_trans_id"]
        or transaction.order_id != order.id
    ):
        raise Refused(-6)
    charged = not form.get("error", "0").strip().startswith("-")
    if not move(db, transaction, "paid" if charged else "cancelled"):
        db.rollback()  # settled before, maybe by a second delivery of this same request
        db.refresh(transaction)
        raise Refused(-4 if transaction.state == "paid" else -9)
    if not charged:
        db.commit()
        raise Refused(-9)
    result = mark_paid(db, order.id, "click")
    if result == "paid":
        db.commit()
    else:
        db.rollback()
        move(db, transaction, "cancelled")
        db.commit()
    return result
