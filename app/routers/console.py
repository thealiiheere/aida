"""The store console: one page showing what Aida and the store are doing right now."""

from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, selectinload

from ..chats import ChatRow, ChatsUnavailable, read_chat, read_chats
from ..config import Settings
from ..deps import Db, SettingsDep, TelegramDep, api_error, require_api_key
from ..lifecycle import EXPIRY_CHECK_SECONDS, HOLD
from ..models import CallLog, Customer, Order, OrderItem, PaymentTransaction, Product, utcnow
from ..schemas import (
    CallDetailOut,
    CallOut,
    ChatDetailOut,
    ChatEntryOut,
    ChatOut,
    ChatsOut,
    ConsoleOrderOut,
    ConsoleOut,
    ErrorOut,
    ShelfOut,
    StatusCountOut,
    StoreStatusOut,
    TransactionOut,
)
from ..telegram import TelegramNotifier
from .orders import order_out

PAGE = Path(__file__).resolve().parent.parent / "web" / "console.html"
STATUSES = ("pending", "escalated", "paid", "cancelled_expired")

router = APIRouter(
    prefix="/api/console", tags=["Console"], dependencies=[Depends(require_api_key)]
)
# The page itself holds no data, so it opens without a key and asks for one if needed.
page = APIRouter(include_in_schema=False)


@page.get("/console", response_class=HTMLResponse)
def console_page(settings: SettingsDep) -> HTMLResponse:
    # The page opens in the store's language; each viewer can switch, and it's remembered.
    html = PAGE.read_text(encoding="utf-8").replace(
        '<html lang="en">', f'<html lang="{settings.store_language}">', 1
    )
    return HTMLResponse(
        html,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@router.get("")
def overview(
    db: Db,
    settings: SettingsDep,
    telegram: TelegramDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> ConsoleOut:
    """Everything the console shows: setup, shelf, newest orders, calls and chats."""
    orders = list(
        db.scalars(
            select(Order)
            .options(
                selectinload(Order.items).selectinload(OrderItem.product),
                selectinload(Order.invoices),
            )
            .order_by(Order.id.desc())
            .limit(limit)
        )
    )
    calls = list(db.scalars(select(CallLog).order_by(CallLog.created_at.desc()).limit(50)))
    chats = _chats(settings)
    people = _people(
        db,
        {c.caller_telegram_id for c in calls if c.caller_telegram_id}
        | {str(chat.chat_id) for chat in chats.items},
    )
    for chat in chats.items:
        chat.customer_name = people.get(str(chat.chat_id), (None, None))[0]
    return ConsoleOut(
        now=utcnow(),
        store=_status(settings, telegram),
        counts=_counts(db),
        orders_total=db.scalar(select(func.count()).select_from(Order)) or 0,
        orders=_with_transactions(db, orders),
        shelf=_shelf(db),
        calls=[_call(call, people) for call in calls],
        chats=chats,
    )


@router.get("/calls/{call_id}", responses={404: {"model": ErrorOut}})
def call_detail(call_id: str, db: Db) -> CallDetailOut:
    """One finished call, with its transcript."""
    call = db.get(CallLog, call_id)
    if call is None:
        raise api_error(404, "call_not_found", f"There is no call {call_id}.")
    people = _people(db, {call.caller_telegram_id} if call.caller_telegram_id else set())
    return CallDetailOut(**_call(call, people).model_dump(), transcript=call.transcript)


@router.get(
    "/chats/{chat_id}", responses={404: {"model": ErrorOut}, 503: {"model": ErrorOut}}
)
def chat_detail(chat_id: int, db: Db, settings: SettingsDep) -> ChatDetailOut:
    """One Telegram conversation: what each side said, Aida's tool calls and the notes."""
    try:
        chat = read_chat(settings.chat_database, chat_id)
    except ChatsUnavailable as exc:
        raise api_error(503, "chats_unavailable", str(exc)) from None
    if chat is None:
        raise api_error(404, "chat_not_found", f"There is no chat with {chat_id}.")
    name = _people(db, {str(chat_id)}).get(str(chat_id), (None, None))[0]
    return ChatDetailOut(
        **_chat(chat, name).model_dump(),
        entries=[ChatEntryOut(**vars(entry)) for entry in chat.entries],
    )


# --- Parts ---------------------------------------------------------------------


def _status(settings: Settings, telegram: TelegramNotifier) -> StoreStatusOut:
    gateways = []
    if settings.click_secret_key:
        gateways.append("click")
    if settings.payme_key:
        gateways.append("payme")
    return StoreStatusOut(
        name=settings.store_name,
        telegram="live" if telegram.configured else "dry_run",
        customer_messages=telegram.can_message_customers,
        payments="real" if settings.real_payments else "demo",
        gateways=gateways,
        summaries=(
            {"openai": settings.openai_api_key, "gemini": settings.gemini_api_key}.get(
                settings.llm_provider, settings.anthropic_api_key
            )
        ) is not None,
        ai={"openai": "openai", "gemini": "gemini"}.get(settings.llm_provider, "claude"),
        language=settings.store_language,
        api_key=settings.agent_api_key is not None,
        hold_minutes=int(HOLD.total_seconds() // 60),
        expiry_check_seconds=EXPIRY_CHECK_SECONDS,
    )


def _counts(db: Session) -> dict[str, StatusCountOut]:
    counts = {status: StatusCountOut(count=0, total=0) for status in STATUSES}
    rows = db.execute(
        select(Order.status, func.count(), func.sum(Order.total_amount)).group_by(Order.status)
    )
    for status, count, total in rows:
        counts[status] = StatusCountOut(count=count, total=total or 0)
    return counts


def _shelf(db: Session) -> list[ShelfOut]:
    held = func.coalesce(
        func.sum(case((OrderItem.state == "reserved", OrderItem.quantity), else_=0)), 0
    )
    sold = func.coalesce(
        func.sum(case((OrderItem.state == "sold", OrderItem.quantity), else_=0)), 0
    )
    rows = db.execute(
        select(Product, held, sold)
        .outerjoin(OrderItem, OrderItem.product_id == Product.id)
        .group_by(Product.id)
        .order_by(Product.id)
    )
    return [
        ShelfOut(
            product_id=product.id,
            title=product.title,
            category=product.category,
            price=product.price,
            on_shelf=product.stock_quantity,
            held=held,
            sold=sold,
        )
        for product, held, sold in rows
    ]


def _with_transactions(db: Session, orders: list[Order]) -> list[ConsoleOrderOut]:
    found = defaultdict(list)
    if orders:
        rows = db.scalars(
            select(PaymentTransaction)
            .where(PaymentTransaction.order_id.in_([order.id for order in orders]))
            .order_by(PaymentTransaction.id)
        )
        for row in rows:
            found[row.order_id].append(
                TransactionOut(
                    provider=row.provider,
                    external_id=row.external_id,
                    state=row.state,
                    amount_uzs=Decimal(row.amount_tiyin) / 100,
                    requested_at=_from_ms(row.requested_ms),
                )
            )
    return [
        ConsoleOrderOut(**order_out(order).model_dump(), transactions=found[order.id])
        for order in orders
    ]


def _from_ms(ms: int) -> datetime | None:
    return datetime.fromtimestamp(ms / 1000, UTC) if ms else None


def _people(db: Session, telegram_ids: set[str]) -> dict[str, tuple[str | None, str | None]]:
    """Name and @username for each Telegram id: from a linked phone, else the newest order."""
    people: dict[str, tuple[str | None, str | None]] = {}
    if not telegram_ids:
        return people
    orders = db.execute(
        select(Order.customer_telegram_id, Order.customer_name, Order.customer_telegram_username)
        .where(Order.customer_telegram_id.in_(telegram_ids))
        .order_by(Order.id)
    )
    for telegram_id, name, username in orders:
        people[telegram_id] = (name, username or people.get(telegram_id, (None, None))[1])
    linked = db.execute(
        select(Customer.telegram_id, Customer.name, Customer.telegram_username).where(
            Customer.telegram_id.in_(telegram_ids)
        )
    )
    for telegram_id, name, username in linked:
        old_name, old_username = people.get(telegram_id, (None, None))
        people[telegram_id] = (name or old_name, username or old_username)
    return people


def _call(call: CallLog, people: dict) -> CallOut:
    name, username = people.get(call.caller_telegram_id, (None, None))
    return CallOut(
        call_id=call.call_id,
        caller_telegram_id=call.caller_telegram_id,
        caller_name=name,
        caller_username=username,
        duration_seconds=call.duration_seconds,
        outcome=call.outcome,
        customer_intent=call.customer_intent,
        items_discussed=call.items_discussed,
        created_at=call.created_at,
        reported=call.alerted_at is not None,
    )


def _chats(settings: Settings) -> ChatsOut:
    try:
        chats = read_chats(settings.chat_database)
    except ChatsUnavailable as exc:
        return ChatsOut(available=False, note=str(exc), items=[])
    return ChatsOut(
        available=True,
        note=None if chats else "Nobody has written to the bot yet.",
        items=[_chat(chat, None) for chat in chats],
    )


def _chat(chat: ChatRow, name: str | None) -> ChatOut:
    last = chat.last
    return ChatOut(
        chat_id=chat.chat_id,
        username=chat.username,
        customer_name=name,
        state=chat.state,
        active_at=chat.active_at,
        paused_at=chat.paused_at,
        failures=chat.failures,
        waiting=chat.waiting,
        messages=sum(entry.kind in ("customer", "aida") for entry in chat.entries),
        last_text=last.text if last else None,
        last_from=last.kind if last else None,
    )
