from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..deps import Db, LimitsDep, TelegramDep, deliver
from ..models import CallLog, Customer, Order, utcnow
from ..schemas import CallEndedIn, CallEndedOut, ErrorOut
from ..summary import CallSummary, Summariser
from ..telegram import call_summary_message

router = APIRouter(prefix="/api/telephony", tags=["Telephony"])

# Orders are matched to a call by time. This covers hang-up and clock differences.
SLACK = timedelta(minutes=1)


def _summariser(request: Request) -> Summariser:
    return request.app.state.summariser


@router.post("/call-ended", responses={429: {"model": ErrorOut}, 502: {"model": ErrorOut}})
def call_ended(
    body: CallEndedIn,
    db: Db,
    telegram: TelegramDep,
    limits: LimitsDep,
    summariser: Annotated[Summariser, Depends(_summariser)],
) -> CallEndedOut:
    """Log a finished call, summarise it with Claude, and send the admin the summary.

    Sending the same `call_id` again changes nothing, unless the admin's message failed
    the first time. Then it is tried again.
    """
    limits.check(body.caller_telegram_id)
    call = db.get(CallLog, body.call_id) or _save(db, body)
    if call.outcome is None:
        _summarise(db, call, body, summariser)
    if not _claim(db, call):
        return _result(call, duplicate=True)

    phone = None if call.caller_telegram_id else body.caller_phone
    username = _username(db, call.caller_telegram_id)
    message = call_summary_message(call, username, phone, telegram.language)
    # Without a summary, the transcript is all there is to go on.
    document = None if call.customer_intent else call.transcript
    try:
        delivery = deliver(telegram, message, document)
    except HTTPException:
        call.alerted_at = None
        db.commit()
        raise
    return _result(call, delivered=delivery.delivered)


def _save(db: Session, body: CallEndedIn) -> CallLog:
    # First and on its own, so a transcript is kept whatever happens after.
    call = CallLog(
        call_id=body.call_id,
        caller_telegram_id=body.caller_telegram_id,
        transcript=body.raw_audio_transcript,
        duration_seconds=round(body.call_duration),
    )
    db.add(call)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()  # delivered twice at once
        call = db.get(CallLog, body.call_id)
    return call


def _summarise(db: Session, call: CallLog, body: CallEndedIn, summariser: Summariser) -> None:
    if call.transcript.strip():
        summary = summariser.summarise(call.transcript)
    else:
        summary = CallSummary(
            customer_intent="Nobody said anything.",
            items_discussed="None.",
            outcome="Inquiry Only",
        )
    if summary:
        call.customer_intent = summary.customer_intent
        call.items_discussed = summary.items_discussed
    call.outcome = _outcome(db, body, summary)
    db.commit()


def _outcome(db: Session, body: CallEndedIn, summary: CallSummary | None) -> str:
    """From the store's own records where they can tell; a transcript can be wrong."""
    if body.order_ids is not None:
        mine = Order.id.in_(body.order_ids)
    else:
        started = utcnow() - timedelta(seconds=body.call_duration) - SLACK
        mine = (Order.customer_telegram_id == body.caller_telegram_id) & (
            Order.created_at >= started
        )
    statuses = set(db.scalars(select(Order.status).where(mine)))
    if body.escalated or "escalated" in statuses:
        return "Escalated"
    if statuses:
        return "Order Placed"
    return "Escalated" if summary and summary.outcome == "Escalated" else "Inquiry Only"


def _claim(db: Session, call: CallLog) -> bool:
    """True for exactly one of any simultaneous deliveries of a call."""
    result = db.execute(
        update(CallLog)
        .where(CallLog.call_id == call.call_id, CallLog.alerted_at.is_(None))
        .values(alerted_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount == 1


def _username(db: Session, telegram_id: str | None) -> str | None:
    if telegram_id is None:
        return None
    return db.scalar(
        select(Customer.telegram_username)
        .where(Customer.telegram_id == telegram_id, Customer.telegram_username.is_not(None))
        .limit(1)
    ) or db.scalar(
        select(Order.customer_telegram_username)
        .where(
            Order.customer_telegram_id == telegram_id,
            Order.customer_telegram_username.is_not(None),
        )
        .order_by(Order.id.desc())
        .limit(1)
    )


def _result(call: CallLog, delivered: bool = False, duplicate: bool = False) -> CallEndedOut:
    return CallEndedOut(
        call_id=call.call_id,
        customer_intent=call.customer_intent,
        items_discussed=call.items_discussed,
        outcome=call.outcome,
        delivered=delivered,
        duplicate=duplicate,
    )
