"""Payme's Merchant API (developer.help.paycom.uz): JSON-RPC over POST.

Payme asks before it charges a customer, so each answer here decides whether a payment
happens. Every answer is HTTP 200, including errors; Payme ignores the body otherwise.
"""

import base64
import hmac
import json
import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..deps import Db, SettingsDep, TelegramDep, api_error
from ..lifecycle import mark_paid
from ..models import Order, PaymentTransaction
from ..telegram import TelegramNotifier, payment_refused_message
from ..transactions import find, move, now_ms, start, whole_number
from .payments import announce_payment

log = logging.getLogger("aida.payments")
router = APIRouter(tags=["Payment webhooks"])

# Payme gives a customer 12 hours to finish a payment.
TIMEOUT_MS = 12 * 60 * 60 * 1000
REASON_TIMEOUT, REASON_UNKNOWN = 4, 10
STATES = {"created": 1, "paid": 2, "cancelled": -1}


class RpcError(Exception):
    def __init__(self, code: int, en: str, ru: str, uz: str, data: str | None = None):
        self.body = {"code": code, "message": {"ru": ru, "uz": uz, "en": en}}
        if data is not None:
            self.body["data"] = data


def _not_authorised() -> RpcError:
    return RpcError(-32504, "Not authorised", "Недостаточно привилегий", "Ruxsat berilmagan")


def _bad_json() -> RpcError:
    return RpcError(-32700, "Invalid JSON", "Ошибка разбора JSON", "JSON noto‘g‘ri")


def _bad_request() -> RpcError:
    return RpcError(-32600, "Invalid request", "Неверный запрос", "Noto‘g‘ri so‘rov")


def _no_such_method(name: str) -> RpcError:
    return RpcError(-32601, "Method not found", "Метод не найден", "Metod topilmadi", name)


def _order_not_found() -> RpcError:
    return RpcError(
        -31050, "Order not found", "Заказ не найден", "Buyurtma topilmadi", "order_id"
    )


def _order_paid() -> RpcError:
    return RpcError(
        -31051,
        "The order is already paid",
        "Заказ уже оплачен",
        "Buyurtma allaqachon to‘langan",
        "order_id",
    )


def _order_expired() -> RpcError:
    return RpcError(
        -31052,
        "The order was not paid in time",
        "Срок оплаты заказа истёк",
        "Buyurtmaning to‘lov muddati tugagan",
        "order_id",
    )


def _order_busy() -> RpcError:
    return RpcError(
        -31053,
        "Another payment for this order is in progress",
        "По заказу уже идёт другая оплата",
        "Buyurtma bo‘yicha boshqa to‘lov kutilmoqda",
        "order_id",
    )


def _wrong_amount() -> RpcError:
    return RpcError(-31001, "Wrong amount", "Неверная сумма", "Noto‘g‘ri summa")


def _not_found() -> RpcError:
    return RpcError(
        -31003, "Transaction not found", "Транзакция не найдена", "Tranzaksiya topilmadi"
    )


def _cannot_cancel() -> RpcError:
    return RpcError(
        -31007,
        "The order is already paid and being shipped",
        "Заказ уже оплачен и готовится к отправке",
        "Buyurtma to‘langan va jo‘natishga tayyorlanmoqda",
    )


def _cannot_perform() -> RpcError:
    return RpcError(
        -31008,
        "This payment can't be completed",
        "Невозможно выполнить операцию",
        "Amalni bajarib bo‘lmaydi",
    )


# --- Request parameters -------------------------------------------------------

TransactionId = Annotated[str, Field(min_length=1, max_length=64)]
Milliseconds = Annotated[int, Field(ge=0, le=10**15)]


class CheckPerform(BaseModel):
    amount: int
    account: dict


class Create(CheckPerform):
    id: TransactionId
    time: Milliseconds


class ById(BaseModel):
    id: TransactionId


class Cancel(ById):
    reason: int = Field(default=REASON_UNKNOWN, ge=0, le=1000)


class Statement(BaseModel):
    start: Milliseconds = Field(alias="from")
    end: Milliseconds = Field(alias="to")


# --- The endpoint -------------------------------------------------------------


async def _body(request: Request) -> bytes:
    return await request.body()


@router.post("/api/webhooks/payme")
def payme_webhook(
    body: Annotated[bytes, Depends(_body)],
    db: Db,
    settings: SettingsDep,
    telegram: TelegramDep,
    background: BackgroundTasks,
    authorization: Annotated[str, Header()] = "",
) -> JSONResponse:
    """All of Payme's methods. A repeated request gets the same answer and changes nothing."""
    if not (settings.payme_key and settings.uzs_per_usd):
        raise api_error(503, "payme_not_set_up", "Set PAYME_KEY and UZS_PER_USD.")
    try:
        call = json.loads(body)
    except (ValueError, RecursionError):
        call = None
    try:
        if not _authorised(authorization, settings):
            raise _not_authorised()
        if call is None:
            raise _bad_json()
        answer = {"result": _Payme(db, settings, telegram, background).run(call)}
    except RpcError as error:
        answer = {"error": error.body}
        log.warning("Payme refused: %s", error.body["message"]["en"])
    call_id = call.get("id") if isinstance(call, dict) else None
    return JSONResponse({**answer, "id": call_id}, media_type="text/json; charset=UTF-8")


def _authorised(header: str, settings: Settings) -> bool:
    scheme, _, given = header.partition(" ")
    pair = f"{settings.payme_login}:{settings.payme_key.get_secret_value()}"
    expected = base64.b64encode(pair.encode())
    return scheme.lower() == "basic" and hmac.compare_digest(expected, given.strip().encode())


class _Payme:
    def __init__(
        self,
        db: Session,
        settings: Settings,
        telegram: TelegramNotifier,
        background: BackgroundTasks,
    ):
        self._db = db
        self._settings = settings
        self._telegram = telegram
        self._background = background

    def run(self, call) -> dict:
        methods = {
            "CheckPerformTransaction": (CheckPerform, self.check_perform),
            "CreateTransaction": (Create, self.create),
            "PerformTransaction": (ById, self.perform),
            "CancelTransaction": (Cancel, self.cancel),
            "CheckTransaction": (ById, self.check),
            "GetStatement": (Statement, self.statement),
        }
        if not isinstance(call, dict) or not isinstance(call.get("method"), str):
            raise _bad_request()
        if call["method"] not in methods:
            raise _no_such_method(call["method"])
        model, method = methods[call["method"]]
        try:
            params = model.model_validate(call.get("params"))
        except ValidationError:
            raise _bad_request() from None
        return method(params)

    def check_perform(self, params: CheckPerform) -> dict:
        self._payable(params)
        return {"allow": True}

    def create(self, params: Create) -> dict:
        transaction = find(self._db, "payme", params.id)
        if transaction is None:
            order = self._payable(params)
            if any(not self._timed_out(other) for other in self._unfinished(order)):
                raise _order_busy()
            transaction = start(
                self._db, "payme", params.id, order.id, params.amount, params.time
            )
        if transaction.state != "created" or self._timed_out(transaction):
            raise _cannot_perform()
        return {
            "create_time": transaction.created_ms,
            "transaction": str(transaction.id),
            "state": STATES[transaction.state],
        }

    def perform(self, params: ById) -> dict:
        transaction = self._get(params.id)
        if transaction.state == "created" and not self._timed_out(transaction):
            self._take_payment(transaction)
        if transaction.state != "paid":
            raise _cannot_perform()
        return {
            "transaction": str(transaction.id),
            "perform_time": transaction.paid_ms,
            "state": STATES[transaction.state],
        }

    def _take_payment(self, transaction: PaymentTransaction) -> None:
        db = self._db
        order = db.get(Order, transaction.order_id)
        # Not moved if a second delivery of this same request got here first.
        result = mark_paid(db, order.id, "payme") if move(db, transaction, "paid") else None
        if result == "paid":
            db.commit()
            announce_payment(self._background, self._telegram, order, "payme")
        else:
            db.rollback()
            move(db, transaction, "cancelled", REASON_UNKNOWN)
            db.commit()
        if result == "out_of_stock":
            message = payment_refused_message(order, "payme", self._telegram.language)
            self._background.add_task(self._telegram.tell_admin, message)
        db.refresh(transaction)

    def cancel(self, params: Cancel) -> dict:
        transaction = self._get(params.id)
        if transaction.state == "created":
            move(self._db, transaction, "cancelled", params.reason)
            self._db.commit()
            self._db.refresh(transaction)
        if transaction.state == "paid":
            raise _cannot_cancel()
        return {
            "transaction": str(transaction.id),
            "cancel_time": transaction.cancelled_ms,
            "state": STATES[transaction.state],
        }

    def check(self, params: ById) -> dict:
        return _details(self._get(params.id))

    def statement(self, params: Statement) -> dict:
        rows = self._db.scalars(
            select(PaymentTransaction)
            .where(
                PaymentTransaction.provider == "payme",
                PaymentTransaction.requested_ms.between(params.start, params.end),
            )
            .order_by(PaymentTransaction.requested_ms, PaymentTransaction.id)
        )
        return {
            "transactions": [
                {
                    "id": row.external_id,
                    "time": row.requested_ms,
                    "amount": row.amount_tiyin,
                    "account": {"order_id": str(row.order_id)},
                    **_details(row),
                }
                for row in rows
            ]
        }

    def _payable(self, params: CheckPerform) -> Order:
        number = whole_number(params.account.get("order_id"))
        order = self._db.get(Order, number) if number is not None else None
        if order is None:
            raise _order_not_found()
        if order.status == "paid":
            raise _order_paid()
        if order.status == "cancelled_expired":
            raise _order_expired()
        if params.amount != self._settings.tiyin(order.total_amount):
            raise _wrong_amount()
        return order

    def _get(self, external_id: str) -> PaymentTransaction:
        transaction = find(self._db, "payme", external_id)
        if transaction is None:
            raise _not_found()
        return transaction

    def _unfinished(self, order: Order) -> list[PaymentTransaction]:
        return list(
            self._db.scalars(
                select(PaymentTransaction).where(
                    PaymentTransaction.provider == "payme",
                    PaymentTransaction.order_id == order.id,
                    PaymentTransaction.state == "created",
                )
            )
        )

    def _timed_out(self, transaction: PaymentTransaction) -> bool:
        """Cancels the payment if its 12 hours are up."""
        if now_ms() - transaction.created_ms <= TIMEOUT_MS:
            return False
        move(self._db, transaction, "cancelled", REASON_TIMEOUT)
        self._db.commit()
        self._db.refresh(transaction)
        return True


def _details(transaction: PaymentTransaction) -> dict:
    return {
        "create_time": transaction.created_ms,
        "perform_time": transaction.paid_ms,
        "cancel_time": transaction.cancelled_ms,
        "transaction": str(transaction.id),
        "state": STATES[transaction.state],
        "reason": transaction.reason,
    }
