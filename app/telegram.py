"""Telegram messages: formatting, and delivery through the Bot API."""

import logging
from contextlib import suppress
from dataclasses import dataclass
from html import escape

import httpx2

from agent.language import language_of

from .chats import ChatsUnavailable, read_chat
from .config import Settings
from .i18n import tr
from .models import CallLog, Invoice, Order
from .schemas import CallReport

log = logging.getLogger("aida.telegram")

# httpx2 logs every request URL at INFO, and Telegram URLs contain the bot token.
logging.getLogger("httpx2").setLevel(logging.WARNING)

PROVIDER_NAMES = {"click": "Click", "payme": "Payme"}

# Telegram rejects messages over 4096 characters. These caps keep the longest
# possible message under that, so a long order or summary is cut, not lost.
MAX_ITEMS_CHARS = 1500
MAX_SUMMARY_CHARS = 1000
MAX_REASON_CHARS = 300
MAX_CALL_MESSAGE_CHARS = 3800


class TelegramError(Exception):
    """Safe to show: never contains the request URL or bot token."""


@dataclass(frozen=True)
class Delivery:
    delivered: bool
    mode: str
    message: str
    telegram_message_id: int | None = None


class TelegramNotifier:
    def __init__(self, settings: Settings, transport: httpx2.BaseTransport | None = None):
        token = settings.telegram_bot_token
        self._token = token.get_secret_value() if token else None
        self._url = settings.telegram_api_url.rstrip("/")
        self._chat_id = settings.admin_chat_id
        self._admin_username = (settings.admin_username or "").removeprefix("@")
        self._client = httpx2.Client(timeout=10, transport=transport)
        # What the admin reads, and what customers read when their own language isn't known.
        self.language = settings.store_language
        self._chats = settings.chat_database

    @property
    def configured(self) -> bool:
        return bool(self._token and self._chat_id)

    @property
    def can_message_customers(self) -> bool:
        return bool(self._token)

    @property
    def admin_tag(self) -> str:
        if self._admin_username:
            return f"@{escape(self._admin_username)}"
        # A positive chat id is a person's own id, so it can be mentioned directly.
        # Group ids are negative and need ADMIN_USERNAME to tag anyone.
        if self._chat_id and self._chat_id.isdigit():
            return f'<a href="tg://user?id={self._chat_id}">Admin</a>'
        return "Admin"

    def customer_language(self, telegram_id: str | None) -> str:
        """The language the customer last wrote to the bot in, else the store's."""
        if not telegram_id or not telegram_id.lstrip("-").isdigit():
            return self.language
        try:
            chat = read_chat(self._chats, int(telegram_id))
        except ChatsUnavailable:
            return self.language
        said = [e.text for e in chat.entries if e.kind == "customer"] if chat else []
        return language_of(said[-1], self.language) if said else self.language

    def close(self) -> None:
        self._client.close()

    def send_admin(self, html: str, document: str | None = None) -> Delivery:
        """Message the admin, with an optional text file sent after it."""
        if not self.configured:
            log.info("Telegram not configured, message not sent:\n%s", html)
            return Delivery(delivered=False, mode="dry_run", message=html)
        message_id = self._send_message(self._chat_id, html)
        if document:
            self._send_transcript(document)
        return Delivery(
            delivered=True, mode="telegram", message=html, telegram_message_id=message_id
        )

    def _send_transcript(self, text: str) -> None:
        try:
            self._call(
                "sendDocument",
                data={"chat_id": self._chat_id},
                files={"document": ("call-transcript.txt", text.encode(), "text/plain")},
            )
        except TelegramError as exc:
            # The message it belongs to has arrived, so failing the request now would
            # only make the caller send that message again.
            log.error("transcript not delivered: %s", exc)
            with suppress(TelegramError):
                note = tr(self.language, "⚠️ The transcript could not be attached.")
                self._send_message(self._chat_id, note)

    def send_customer(self, telegram_id: str, html: str) -> None:
        """Message a customer. Telegram refuses unless they have started the bot."""
        self._send_message(telegram_id, html)

    def tell_customer(self, telegram_id: str, html: str) -> None:
        """Message a customer if possible. Nothing depends on it arriving."""
        if not self.can_message_customers:
            log.info("Telegram not configured, customer not told:\n%s", html)
            return
        try:
            self.send_customer(telegram_id, html)
        except TelegramError as exc:
            log.error("customer not told: %s", exc)

    def tell_admin(self, html: str) -> None:
        """Message the admin if possible. Nothing depends on it arriving."""
        try:
            self.send_admin(html)
        except TelegramError as exc:
            log.error("admin not told: %s\n%s", exc, html)

    def _send_message(self, chat_id: str, html: str) -> int:
        result = self._call(
            "sendMessage",
            json={
                "chat_id": chat_id,
                "text": html,
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
            },
        )
        return result["message_id"]

    def _call(self, method: str, **payload) -> dict:
        try:
            response = self._client.post(
                f"{self._url}/bot{self._token}/{method}", **payload
            )
            body = response.json()
        except (httpx2.HTTPError, httpx2.InvalidURL) as exc:
            # "from None": the original error can carry the URL, and so the token.
            raise TelegramError(f"could not reach Telegram ({type(exc).__name__})") from None
        except ValueError:
            raise TelegramError(
                f"unexpected reply from Telegram (HTTP {response.status_code})"
            ) from None
        if not body.get("ok"):
            raise TelegramError(body.get("description") or f"HTTP {response.status_code}")
        return body["result"]


def _cut(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _contact(
    name: str | None,
    username: str | None,
    telegram_id: str | None,
    phone: str | None = None,
    language: str = "en",
) -> str:
    if username:
        handle = f"@{username}"
    elif telegram_id:
        handle = f'<a href="tg://user?id={telegram_id}">ID {telegram_id}</a>'
    else:
        handle = tr(language, "no Telegram contact given")
    contact = f"{escape(name)} ({handle})" if name else handle
    return f"{contact}, {phone}" if phone else contact


def _items(order: Order, language: str = "en") -> str:
    parts: list[str] = []
    length = 0
    for index, item in enumerate(order.items):
        variant = f" ({item.variant})" if item.variant else ""
        part = f"{item.product.title}{variant} × {item.quantity}"
        length += len(part) + 2
        if length > MAX_ITEMS_CHARS:
            parts.append(tr(language, "+{n} more", n=len(order.items) - index))
            break
        parts.append(part)
    return escape(", ".join(parts))


def _money(amount) -> str:
    return f"{amount:.2f}"


def order_message(
    order: Order, invoice: Invoice, summary: str | None, language: str = "en"
) -> str:
    t = lambda english, **v: tr(language, english, **v)  # noqa: E731
    contact = _contact(
        order.customer_name,
        order.customer_telegram_username,
        order.customer_telegram_id,
        language=language,
    )
    lines = [
        t("🛍️ NEW ORDER CONFIRMED #{id}", id=order.id),
        t("👤 Customer: {contact}", contact=contact),
        t("📍 Address: {address}", address=escape(order.delivery_address)),
        t("📦 Items: {items}", items=_items(order, language)),
        t("💰 Total: ${total} via {provider}", total=_money(order.total_amount),
          provider=PROVIDER_NAMES[invoice.provider]),
        t("🔗 Payment Link: {url}", url=escape(invoice.payment_url)),
    ]  # fmt: skip
    if summary and summary.strip():
        lines.append(t("📝 Summary: {text}", text=escape(_cut(summary, MAX_SUMMARY_CHARS))))
    return "\n".join(lines)


def escalation_message(
    admin_tag: str,
    reason: str,
    summary: str,
    order: Order | None,
    name: str | None,
    username: str | None,
    telegram_id: str | None,
    phone: str | None = None,
    last_spoken: str | None = None,
    language: str = "en",
) -> str:
    t = lambda english, **v: tr(language, english, **v)  # noqa: E731
    if order:
        name = name or order.customer_name
        username = username or order.customer_telegram_username
        telegram_id = telegram_id or order.customer_telegram_id
    lines = [
        t("🚨 HUMAN SUPPORT NEEDED {tag}", tag=admin_tag),
        t("👤 Customer: {contact}", contact=_contact(name, username, telegram_id, phone, language)),
    ]
    if order:
        lines += [
            t("🧾 Order: #{id} (${total}, {status})", id=order.id,
              total=_money(order.total_amount), status=t(order.status)),
            t("📦 Items: {items}", items=_items(order, language)),
            t("📍 Address: {address}", address=escape(order.delivery_address)),
        ]  # fmt: skip
    lines.append(t("❓ Reason: {reason}", reason=escape(_cut(reason, MAX_REASON_CHARS))))
    if last_spoken:
        words = escape(_cut(last_spoken, MAX_REASON_CHARS))
        lines.append(t("💬 Last words: “{text}”", text=words))
    lines.append(t("📝 Call summary: {text}", text=escape(_cut(summary, MAX_SUMMARY_CHARS))))
    return "\n".join(lines)


def payment_message(store_name: str, invoice: Invoice, language: str = "en") -> str:
    """Sent to the customer's own chat."""
    t = lambda english, **v: tr(language, english, **v)  # noqa: E731
    return "\n".join(
        [
            t("🛍️ {store}: order #{id}", store=escape(store_name), id=invoice.order_id),
            t("💰 Total: ${total} via {provider}", total=_money(invoice.amount),
              provider=PROVIDER_NAMES[invoice.provider]),
            t("🔗 Pay here: {url}", url=escape(invoice.payment_url)),
        ]  # fmt: skip
    )


def paid_message(order_id: int, language: str = "en") -> str:
    """Sent to the customer's own chat."""
    return tr(
        language,
        "✅ Payment Received! Your order #{id} is confirmed and being prepared for shipment.",
        id=order_id,
    )


def payment_confirmed_message(order: Order, provider: str, language: str = "en") -> str:
    return tr(
        language,
        "💳 PAYMENT CONFIRMED #{id} | ${total} received via {provider}.",
        id=order.id,
        total=_money(order.total_amount),
        provider=PROVIDER_NAMES[provider],
    )


def payment_refused_message(order: Order, provider: str, language: str = "en") -> str:
    return tr(
        language,
        "⚠️ PAYMENT REFUSED #{id} | The order had expired and its items have sold since, "
        "so {provider} was told to return the ${total}.",
        id=order.id,
        total=_money(order.total_amount),
        provider=PROVIDER_NAMES[provider],
    )


def expired_message(order: Order, language: str = "en") -> str:
    """Sent to the customer's own chat."""
    if order.invoices:
        english = (
            "Your payment link for order #{id} has expired. "
            "Let us know when you're ready to order again!"
        )
    else:
        english = (
            "Your reservation for order #{id} has expired. "
            "Let us know when you're ready to order again!"
        )
    return tr(language, english, id=order.id)


CALL_OUTCOMES = {
    "completed": "CALL ENDED",
    "escalated": "CALL ENDED, HANDED TO A PERSON",
    "failed": "CALL DROPPED, TECHNICAL PROBLEM",
    "silent": "CALL ENDED, CALLER WENT SILENT",
}


def _order_block(order: Order, language: str = "en") -> list[str]:
    t = lambda english, **v: tr(language, english, **v)  # noqa: E731
    lines = [
        t("🛍️ Order #{id} ({status}): {items}", id=order.id, status=t(order.status),
          items=_items(order, language)),
        t("📍 Address: {address}", address=escape(order.delivery_address)),
    ]  # fmt: skip
    total = _money(order.total_amount)
    if not order.invoices:
        return [*lines, t("💰 Total: ${total}. No payment link was created.", total=total)]
    invoice = next((i for i in order.invoices if i.status == "paid"), order.invoices[-1])
    return [
        *lines,
        t("💰 Total: ${total} via {provider}", total=total,
          provider=PROVIDER_NAMES[invoice.provider]),
        t("🔗 Payment Link: {url}", url=escape(invoice.payment_url)),
    ]  # fmt: skip


def call_message(
    call: CallReport, orders: list[Order], language: str = "en"
) -> tuple[str, str | None]:
    """The admin message for a finished call, plus the transcript as a file if it won't fit."""
    t = lambda english, **v: tr(language, english, **v)  # noqa: E731
    minutes, seconds = divmod(call.duration_seconds, 60)
    length = t("{m} min {s} s", m=minutes, s=seconds) if minutes else t("{s} s", s=seconds)
    name = call.customer_name or (orders[0].customer_name if orders else None)
    contact = _contact(
        name,
        call.customer_telegram_username,
        call.customer_telegram_id,
        call.customer_phone,
        language,
    )
    lines = [f"📞 {t(CALL_OUTCOMES[call.outcome])} ({length})", t("👤 Customer: {contact}",
             contact=contact)]  # fmt: skip
    for order in orders[:1]:
        lines += _order_block(order, language)
    if len(orders) > 1:
        others = ", ".join(f"#{order.id}" for order in orders[1:])
        lines.append(t("🛍️ Other orders in this call: {ids}", ids=others))
    if not orders:
        lines.append(t("🛍️ No order was placed."))
    header = "\n".join(lines)

    speakers = {"caller": t("Caller"), "aida": "Aida"}
    transcript = "\n".join(f"{speakers[line.speaker]}: {line.text}" for line in call.transcript)
    if not transcript:
        return header, None
    if len(header) + len(transcript) > MAX_CALL_MESSAGE_CHARS:
        return f"{header}\n{t('📝 Transcript attached.')}", transcript
    quote = f"<blockquote expandable>{escape(transcript)}</blockquote>"
    return f"{header}\n{t('📝 Transcript:')}\n{quote}", None


def call_summary_message(
    call: CallLog, username: str | None, phone: str | None, language: str = "en"
) -> str:
    t = lambda english, **v: tr(language, english, **v)  # noqa: E731
    if call.customer_intent:
        points = [call.customer_intent, call.items_discussed]
    else:
        points = [t("Not available. The transcript is attached instead.")]
    contact = _contact(None, username, call.caller_telegram_id, phone, language)
    return "\n".join(
        [
            t("📞 VOICE CALL SUMMARY ({s}s)", s=call.duration_seconds),
            t("👤 Customer: {contact}", contact=contact),
            t("📝 Summary:"),
            *(f"• {escape(_cut(point, MAX_REASON_CHARS))}" for point in points),
            t("📌 Outcome: {outcome}", outcome=t(call.outcome) if call.outcome else call.outcome),
        ]
    )
