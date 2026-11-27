import re
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    model_validator,
)

Price = Annotated[Decimal, PlainSerializer(float, return_type=float, when_used="json")]
Provider = Literal["click", "payme"]
OrderStatus = Literal["pending", "paid", "escalated", "cancelled_expired"]


def _one_line(value: str) -> str:
    # Customer text ends up in a line-per-field admin message. Without this, a
    # name containing a newline could forge its own "Total" or "Payment Link" line.
    return " ".join(value.split())


def _blank_to_none(value):
    if isinstance(value, str):
        value = value.strip().removeprefix("@")
    return value or None


def _int_to_str(value):
    return str(value) if isinstance(value, int) else value


def _phone_digits(value):
    return re.sub(r"[\s().-]", "", value) if isinstance(value, str) else value


def _not_blank(value: str) -> str:
    if not value:
        raise ValueError("must not be blank")
    return value


OneLine = Annotated[str, AfterValidator(_one_line), AfterValidator(_not_blank)]
TelegramId = Annotated[str, BeforeValidator(_int_to_str), Field(pattern=r"^\d{1,20}$")]
Username = Annotated[
    Annotated[str, Field(pattern=r"^[A-Za-z0-9_]{4,32}$")] | None, BeforeValidator(_blank_to_none)
]
Phone = Annotated[str, BeforeValidator(_phone_digits), Field(pattern=r"^\+\d{7,15}$")]
PersonName = Annotated[OneLine, Field(max_length=120)]


class Request(BaseModel):
    # Unknown fields are an error, so a price or discount can't be slipped in.
    model_config = ConfigDict(extra="forbid")


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorOut(BaseModel):
    detail: ErrorDetail


# --- Products ---------------------------------------------------------------


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    price: Price
    stock_quantity: int
    category: str
    description: str


class SearchOut(BaseModel):
    query: str
    count: int
    products: list[ProductOut]


class StockOut(BaseModel):
    product_id: int
    title: str
    price: Price
    stock_quantity: int
    in_stock: bool
    variant: str | None = None
    note: str | None = None


# --- Orders -----------------------------------------------------------------


class OrderItemIn(Request):
    product_id: int
    quantity: int = Field(ge=1, le=100)
    variant: Annotated[OneLine, Field(max_length=60)] | None = None


class OrderIn(Request):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "customer_telegram_id": "123456789",
                "customer_telegram_username": "dilnoza_k",
                "customer_name": "Dilnoza Karimova",
                "delivery_address": "12 Amir Temur Street, Tashkent",
                "items": [{"product_id": 3, "quantity": 1, "variant": "brown"}],
            }
        },
    )

    customer_telegram_id: TelegramId
    customer_telegram_username: Username = None
    customer_name: PersonName
    delivery_address: Annotated[OneLine, Field(max_length=500)]
    items: list[OrderItemIn] = Field(min_length=1, max_length=20)


class OrderItemOut(BaseModel):
    product_id: int
    title: str
    variant: str | None
    quantity: int
    unit_price: Price
    line_total: Price
    state: Literal["reserved", "sold", "released"]


class InvoiceOut(BaseModel):
    invoice_id: str
    order_id: int
    provider: Provider
    amount: Price
    payment_url: str
    status: Literal["unpaid", "paid"]


class OrderOut(BaseModel):
    order_id: int
    status: OrderStatus
    customer_telegram_id: str
    customer_telegram_username: str | None
    customer_name: str
    delivery_address: str
    items: list[OrderItemOut]
    total_amount: Price
    invoices: list[InvoiceOut]
    created_at: datetime
    reserved_until: datetime | None = Field(
        description="When an unpaid order gives its stock back. Null once it is paid, "
        "escalated or expired."
    )


class OrderStatusOut(BaseModel):
    order_id: int
    status: OrderStatus
    total_amount: Price
    reserved_until: datetime | None


class InvoiceIn(Request):
    order_id: int
    provider: Provider
    send_to_customer: bool = Field(
        default=False, description="Also send the link to the customer's Telegram chat."
    )


class CreatedInvoiceOut(InvoiceOut):
    sent_to_customer: bool = Field(
        default=False, description="True only if Telegram accepted the message to the customer."
    )
    delivery_note: str | None = None


# --- Customers --------------------------------------------------------------


class CustomerIn(Request):
    phone: Phone
    telegram_id: TelegramId
    telegram_username: Username = None
    name: PersonName | None = None


class CustomerLookupIn(Request):
    phone: Phone


class CustomerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    phone: str
    telegram_id: str
    telegram_username: str | None
    name: str | None


# --- Admin ------------------------------------------------------------------


class TranscriptLine(Request):
    speaker: Literal["caller", "aida"]
    text: OneLine


class CallReport(Request):
    call_id: str = Field(max_length=64, pattern=r"^[\w.-]+$")
    duration_seconds: int = Field(ge=0)
    outcome: Literal["completed", "escalated", "failed", "silent"] = "completed"
    order_ids: list[int] = Field(default=[], max_length=20)
    customer_phone: Phone | None = None
    customer_telegram_id: TelegramId | None = None
    customer_telegram_username: Username = None
    customer_name: PersonName | None = None
    transcript: list[TranscriptLine] = Field(default=[], max_length=2000)


class NotifyOrderIn(Request):
    """Either one order (sent during a call or chat) or a finished call's report."""

    order_id: int | None = None
    customer_summary: str | None = Field(default=None, max_length=4000)
    payment_link: str | None = Field(
        default=None,
        max_length=500,
        description="Picks which invoice to report. The link sent to the admin is always "
        "the stored one, never this text.",
    )
    call: CallReport | None = None

    @model_validator(mode="after")
    def _one_kind(self):
        if (self.order_id is None) == (self.call is None):
            raise ValueError("send either order_id or call")
        return self


class EscalateIn(Request):
    reason: Annotated[OneLine, Field(max_length=1000)]
    context_summary: Annotated[OneLine, Field(max_length=4000)]
    order_id: int | None = None
    customer_telegram_id: TelegramId | None = None
    customer_telegram_username: Username = None
    customer_phone: Phone | None = None
    customer_name: PersonName | None = None
    last_spoken: Annotated[OneLine, Field(max_length=1000)] | None = Field(
        default=None, description="The last thing the customer said, word for word."
    )


class CallEndedIn(BaseModel):
    # Phone providers add fields of their own over time, so unknown ones are ignored.
    call_id: str = Field(max_length=64, pattern=r"^[\w.-]+$")
    caller_telegram_id: TelegramId | None = None
    raw_audio_transcript: str = Field(max_length=200_000)
    call_duration: float = Field(ge=0, le=86_400, description="In seconds.")
    # What the phone line knows for certain. Without these, orders are matched by the
    # caller's Telegram ID and the time of the call.
    caller_phone: Phone | None = None
    order_ids: list[int] | None = Field(default=None, max_length=20)
    escalated: bool = False


class CallEndedOut(BaseModel):
    call_id: str
    customer_intent: str | None
    items_discussed: str | None
    outcome: Literal["Order Placed", "Escalated", "Inquiry Only"]
    delivered: bool = Field(description="True only if Telegram accepted the admin's message.")
    duplicate: bool = Field(description="True if this call had already been reported.")


class DeliveryOut(BaseModel):
    delivered: bool = Field(description="True only if Telegram accepted the message.")
    mode: Literal["telegram", "dry_run"]
    telegram_message_id: int | None = None
    message: str = Field(description="The message text, as Telegram HTML.")


class NotifyOrderOut(DeliveryOut):
    order_id: int | None = None
    payment_link: str | None = None


class EscalateOut(DeliveryOut):
    pause_ai: Literal[True] = True
    order_id: int | None
    order_status: str | None


# --- Console ----------------------------------------------------------------


class StoreStatusOut(BaseModel):
    name: str
    telegram: Literal["live", "dry_run"] = Field(
        description="dry_run: admin messages are only logged."
    )
    customer_messages: bool = Field(description="Payment links can reach customers' chats.")
    payments: Literal["demo", "real"] = Field(
        description="demo: payment links open the demo checkout, which marks orders paid."
    )
    gateways: list[Provider]
    summaries: bool = Field(description="The AI summarises finished calls.")
    ai: Literal["claude", "openai", "gemini"] = Field(description="Which AI answers and summarises.")
    language: Literal["en", "uz", "ru"] = Field(description="STORE_LANGUAGE: the default.")
    api_key: bool = Field(description="The API asks for X-API-Key.")
    hold_minutes: int
    expiry_check_seconds: int


class StatusCountOut(BaseModel):
    count: int
    total: Price


class ShelfOut(BaseModel):
    product_id: int
    title: str
    category: str
    price: Price
    on_shelf: int = Field(description="Free to sell: stock_quantity.")
    held: int = Field(description="In unpaid or escalated orders.")
    sold: int


class TransactionOut(BaseModel):
    provider: Provider
    external_id: str
    state: Literal["created", "paid", "cancelled"]
    amount_uzs: Price
    requested_at: datetime | None


class ConsoleOrderOut(OrderOut):
    transactions: list[TransactionOut]


class CallOut(BaseModel):
    call_id: str
    caller_telegram_id: str | None
    caller_name: str | None
    caller_username: str | None
    duration_seconds: int
    outcome: str | None = Field(description="Empty until the call has been summarised.")
    customer_intent: str | None
    items_discussed: str | None
    created_at: datetime
    reported: bool = Field(description="The admin's message about this call was sent.")


class CallDetailOut(CallOut):
    transcript: str


class ChatEntryOut(BaseModel):
    kind: Literal["customer", "aida", "note", "tool"]
    text: str
    tool: str | None = None
    ok: bool | None = None
    html: str | None = Field(
        default=None, description="Aida's words as Telegram HTML, the way the customer saw them."
    )


class ChatOut(BaseModel):
    chat_id: int
    username: str | None
    customer_name: str | None = Field(description="From the customer's newest order.")
    state: Literal["aida", "answering", "with_person"]
    active_at: datetime | None
    paused_at: datetime | None = Field(description="When a person took over from Aida.")
    failures: int = Field(description="Times in a row that Claude didn't answer.")
    waiting: int = Field(description="Messages received but not answered yet.")
    messages: int = Field(description="Things said by either side.")
    last_text: str | None
    last_from: Literal["customer", "aida"] | None


class ChatDetailOut(ChatOut):
    entries: list[ChatEntryOut]


class ChatsOut(BaseModel):
    available: bool
    note: str | None = Field(description="Why there are no conversations to show.")
    items: list[ChatOut]


class ConsoleOut(BaseModel):
    now: datetime
    store: StoreStatusOut
    counts: dict[OrderStatus, StatusCountOut]
    orders_total: int
    orders: list[ConsoleOrderOut] = Field(description="The newest orders, newest first.")
    shelf: list[ShelfOut]
    calls: list[CallOut]
    chats: ChatsOut
