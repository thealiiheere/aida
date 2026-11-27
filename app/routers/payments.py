"""Payment links, and the demo checkout page they open."""

import secrets
from html import escape

from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import Settings
from ..deps import (
    Db,
    LimitsDep,
    SettingsDep,
    TelegramDep,
    api_error,
    get_order,
    require_api_key,
)
from ..i18n import tr
from ..lifecycle import mark_paid
from ..models import Invoice, Order
from ..schemas import CreatedInvoiceOut, ErrorOut, InvoiceIn
from ..telegram import (
    PROVIDER_NAMES,
    TelegramError,
    TelegramNotifier,
    paid_message,
    payment_confirmed_message,
    payment_message,
)
from .orders import invoice_out

router = APIRouter(tags=["Payments"])


def announce_payment(
    background: BackgroundTasks, telegram: TelegramNotifier, order: Order, provider: str
) -> None:
    """Tell the customer and the admin, once the gateway has its answer."""
    customer = order.customer_telegram_id
    paid = paid_message(order.id, telegram.customer_language(customer))
    background.add_task(telegram.tell_customer, customer, paid)
    confirmed = payment_confirmed_message(order, provider, telegram.language)
    background.add_task(telegram.tell_admin, confirmed)


def _find(db: Session, order_id: int, provider: str) -> Invoice | None:
    return db.scalar(
        select(Invoice).where(Invoice.order_id == order_id, Invoice.provider == provider)
    )


@router.post(
    "/api/payments/create-invoice",
    dependencies=[Depends(require_api_key)],
    responses={404: {"model": ErrorOut}, 409: {"model": ErrorOut}, 429: {"model": ErrorOut}},
)
def create_invoice(
    body: InvoiceIn, db: Db, settings: SettingsDep, telegram: TelegramDep, limits: LimitsDep
) -> CreatedInvoiceOut:
    """Create a payment link. Tool: `generate_invoice`.

    Calling again with the same order and provider returns the same link. With
    `send_to_customer`, the link also goes to the customer's Telegram chat; if that
    fails the invoice still stands and `sent_to_customer` is false.
    """
    order = get_order(db, body.order_id)
    limits.check(order.customer_telegram_id)
    if order.status == "paid":
        raise api_error(409, "already_paid", f"Order #{order.id} is already paid.")
    if order.status == "cancelled_expired":
        raise api_error(
            409, "order_expired", f"Order #{order.id} was not paid in time. Create a new order."
        )
    invoice = _find(db, order.id, body.provider) or _create(db, order, body.provider, settings)
    result = CreatedInvoiceOut(**invoice_out(invoice).model_dump())
    if body.send_to_customer:
        result.delivery_note = _send_link(telegram, settings, order, invoice)
        result.sent_to_customer = result.delivery_note is None
    return result


def _send_link(
    telegram: TelegramNotifier, settings: Settings, order: Order, invoice: Invoice
) -> str | None:
    """Returns why the link could not be delivered, or None if it was."""
    if not telegram.can_message_customers:
        return "Telegram is not set up, so the customer has not received the link."
    try:
        language = telegram.customer_language(order.customer_telegram_id)
        telegram.send_customer(
            order.customer_telegram_id, payment_message(settings.store_name, invoice, language)
        )
    except TelegramError as exc:
        return f"The customer has not received the link: {exc}."
    return None


def _create(db: Session, order: Order, provider: str, settings: Settings) -> Invoice:
    # The link alone is enough to "pay", so the id has to be unguessable.
    invoice_id = f"inv_{secrets.token_urlsafe(12)}"
    invoice = Invoice(
        id=invoice_id,
        order_id=order.id,
        provider=provider,
        amount=order.total_amount,
        payment_url=f"{settings.payment_base_url.rstrip('/')}/{invoice_id}",
    )
    db.add(invoice)
    try:
        db.commit()
    except IntegrityError:
        # A simultaneous retry got there first; hand back its invoice.
        db.rollback()
        invoice = _find(db, order.id, provider)
    return invoice


# --- Mock checkout. Opened by customers, so it takes no API key. --------------

PAGE = """<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{title}</title>
<style>
:root {{ color-scheme: light dark; --bg: #f4f5f4; --card: #fff; --text: #1b1f1d; --muted: #5d6662;
  --line: #dfe3e1; --accent: #14694a; --on-accent: #fff; --note: #fdf3d7; --on-note: #6a4b00; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg: #121614; --card: #1b211e; --text: #edf1ef;
  --muted: #a1aca7; --line: #313a36; --accent: #58c596; --on-accent: #08251a; --note: #3a2f10;
  --on-note: #f6dc97; }} }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 16px;
  background: var(--bg); color: var(--text); font: 16px/1.5 system-ui, sans-serif; }}
main {{ width: 100%; max-width: 400px; background: var(--card); border: 1px solid var(--line);
  border-radius: 12px; padding: 24px; }}
h1 {{ margin: 16px 0 4px; font-size: 20px; line-height: 1.3; overflow-wrap: anywhere; }}
p {{ margin: 0; color: var(--muted); }}
.note {{ padding: 8px 12px; border-radius: 8px; background: var(--note); color: var(--on-note);
  font-size: 14px; }}
.total {{ margin: 20px 0; font-size: 34px; font-weight: 650; line-height: 1.2; color: var(--text);
  font-variant-numeric: tabular-nums; }}
button {{ width: 100%; min-height: 48px; border: 0; border-radius: 8px; background: var(--accent);
  color: var(--on-accent); font: inherit; font-weight: 600; cursor: pointer; }}
button:focus-visible {{ outline: 3px solid var(--accent); outline-offset: 3px; }}
.langs {{ display: flex; gap: 4px; justify-content: flex-end; margin: -8px -8px 8px 0;
  font-size: 14px; }}
.langs a {{ min-width: 44px; min-height: 32px; display: grid; place-items: center;
  border-radius: 6px; color: var(--muted); text-decoration: none; }}
.langs a[aria-current] {{ color: var(--text); font-weight: 600; background: var(--bg); }}
</style>
</head>
<body><main>
<nav class="langs" aria-label="{language}">{langs}</nav>
<p class="note">{demo}</p>
{body}
</main></body>
</html>"""


LANGUAGE_NAMES = {"uz": "O‘zbekcha", "ru": "Русский", "en": "English"}


def _page(title: str, body: str, status: int = 200, lang: str = "en") -> HTMLResponse:
    links = "".join(
        f'<a href="?lang={code}" hreflang="{code}" lang="{code}"'
        f'{" aria-current=\"true\"" if code == lang else ""}>{code.upper()}</a>'
        for code in LANGUAGE_NAMES
    )
    page = PAGE.format(
        lang=lang,
        title=escape(title),
        body=body,
        langs=links,
        language=tr(lang, "Language"),
        demo=tr(lang, "Demo checkout. No real payment is taken."),
    )
    return HTMLResponse(
        page,
        status_code=status,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


def _not_found(lang: str = "en") -> HTMLResponse:
    title = tr(lang, "Payment link not found")
    hint = tr(lang, "Check the link in your Telegram chat.")
    body = f"<h1>{escape(title, quote=False)}</h1><p>{escape(hint, quote=False)}</p>"
    return _page(title, body, 404, lang)


def _language(lang: str | None, settings: Settings) -> str:
    """?lang= if it names one of the three, else the store's own language."""
    return lang if lang in LANGUAGE_NAMES else settings.store_language


@router.get("/pay/{invoice_id}", response_class=HTMLResponse)
def checkout_page(
    invoice_id: str, db: Db, settings: SettingsDep, lang: str | None = None
) -> HTMLResponse:
    """Mock checkout page the customer opens from Telegram. `lang`: uz, ru or en."""
    lang = _language(lang, settings)
    t = lambda english, **v: escape(tr(lang, english, **v), quote=False)  # noqa: E731
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        return _not_found(lang)
    order = invoice.order
    heading = f"<h1>{t('Order #{id}', id=order.id)}</h1><p>{escape(settings.store_name)}</p>"
    total = f'<p class="total">${invoice.amount:.2f}</p>'
    if order.status == "paid":
        return _page(
            tr(lang, "Order #{id} paid", id=order.id),
            f"{heading}{total}<p>{t('This order is paid. Thank you!')}</p>",
            lang=lang,
        )
    if order.status == "cancelled_expired":
        expired = t("This payment link has expired. Message us to order again.")
        return _page(
            tr(lang, "Order #{id} expired", id=order.id),
            f"{heading}{total}<p>{expired}</p>",
            lang=lang,
        )
    if settings.real_payments:
        refused = t(
            "This demo page can't take payments here, because the store has real ones "
            "switched on."
        )
        return _page(tr(lang, "Order #{id}", id=order.id), f"{heading}{total}<p>{refused}</p>",
                     lang=lang)  # fmt: skip
    pay = t("Pay with {provider} (demo)", provider=PROVIDER_NAMES[invoice.provider])
    return _page(
        tr(lang, "Pay for order #{id}", id=order.id),
        f'{heading}{total}<form method="post"><button>{pay}</button></form>',
        lang=lang,
    )


@router.post("/pay/{invoice_id}", response_model=None)
def complete_checkout(
    invoice_id: str,
    db: Db,
    settings: SettingsDep,
    telegram: TelegramDep,
    background: BackgroundTasks,
    lang: str | None = None,
) -> HTMLResponse | RedirectResponse:
    """Mark the order paid, as a real gateway's webhook would."""
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        return _not_found(_language(lang, settings))
    order = invoice.order
    # Never with real payments on: this would be a way to get things for free.
    if not settings.real_payments and order.status != "cancelled_expired":
        if mark_paid(db, order.id, invoice.provider) == "paid":
            db.commit()
            announce_payment(background, telegram, order, invoice.provider)
    # Relative, so it still works when a proxy serves this page under another path.
    back = f"{invoice_id}?lang={lang}" if lang in LANGUAGE_NAMES else invoice_id
    return RedirectResponse(back, status_code=303)
