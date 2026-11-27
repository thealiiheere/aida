from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

CENT = Decimal("0.01")


class Money(TypeDecorator):
    """Decimal in Python, whole cents in SQLite, so totals never drift."""

    impl = Integer
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return int(Decimal(value).quantize(CENT, ROUND_HALF_UP) * 100)

    def process_result_value(self, value, dialect):
        return None if value is None else (Decimal(value) / 100).quantize(CENT)


class UtcDateTime(TypeDecorator):
    """SQLite drops time zones; store UTC and put the zone back on read."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        return None if value is None else value.replace(tzinfo=UTC)


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (CheckConstraint("stock_quantity >= 0"), CheckConstraint("price >= 0"))

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(120))
    price: Mapped[Decimal] = mapped_column(Money)
    stock_quantity: Mapped[int] = mapped_column(default=0)
    category: Mapped[str] = mapped_column(String(60), index=True)
    description: Mapped[str] = mapped_column(default="")


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'paid', 'escalated', 'cancelled_expired')"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_telegram_id: Mapped[str] = mapped_column(String(20), index=True)
    # Not in the spec's Order model, but the admin message needs the @username.
    customer_telegram_username: Mapped[str | None] = mapped_column(String(32))
    customer_name: Mapped[str] = mapped_column(String(120))
    delivery_address: Mapped[str] = mapped_column(String(500))
    total_amount: Mapped[Decimal] = mapped_column(Money)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)

    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="OrderItem.id"
    )
    invoices: Mapped[list["Invoice"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="Invoice.created_at"
    )


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("quantity > 0"),
        CheckConstraint("state IN ('reserved', 'sold', 'released')"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    quantity: Mapped[int]
    unit_price: Mapped[Decimal] = mapped_column(Money)
    # Not in the spec's OrderItem model: keeps the size/color the caller confirmed.
    variant: Mapped[str | None] = mapped_column(String(60))
    # Reserved items are already out of Product.stock_quantity; released ones are back in.
    state: Mapped[str] = mapped_column(String(10), default="reserved")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)

    order: Mapped[Order] = relationship(back_populates="items")
    product: Mapped[Product] = relationship()


class Invoice(Base):
    """One per order and provider, so a retried tool call returns the same link."""

    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("order_id", "provider"),
        CheckConstraint("provider IN ('click', 'payme')"),
        CheckConstraint("status IN ('unpaid', 'paid')"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    provider: Mapped[str] = mapped_column(String(10))
    amount: Mapped[Decimal] = mapped_column(Money)
    payment_url: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(10), default="unpaid")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)

    order: Mapped[Order] = relationship(back_populates="invoices")


class PaymentTransaction(Base):
    """One payment attempt at Click or Payme, under the id they gave it."""

    __tablename__ = "payment_transactions"
    __table_args__ = (
        UniqueConstraint("provider", "external_id"),
        CheckConstraint("provider IN ('click', 'payme')"),
        CheckConstraint("state IN ('created', 'paid', 'cancelled')"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(10))
    external_id: Mapped[str] = mapped_column(String(64))
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    amount_tiyin: Mapped[int] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(10), default="created")
    reason: Mapped[int | None]
    # Milliseconds since 1970, as Payme counts; 0 until it happens.
    requested_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    created_ms: Mapped[int] = mapped_column(BigInteger)
    paid_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    cancelled_ms: Mapped[int] = mapped_column(BigInteger, default=0)


class CallLog(Base):
    __tablename__ = "call_logs"

    call_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    caller_telegram_id: Mapped[str | None] = mapped_column(String(20), index=True)
    transcript: Mapped[str] = mapped_column(Text)
    duration_seconds: Mapped[int]
    # Empty until Claude has summarised the call.
    customer_intent: Mapped[str | None] = mapped_column(Text)
    items_discussed: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    # Set just before the admin is messaged, so two deliveries can't both do it.
    alerted_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class Customer(Base):
    """Links a phone number to a Telegram account, so phone callers can get payment links."""

    __tablename__ = "customers"

    phone: Mapped[str] = mapped_column(String(16), primary_key=True)
    telegram_id: Mapped[str] = mapped_column(String(20), index=True)
    telegram_username: Mapped[str | None] = mapped_column(String(32))
    name: Mapped[str | None] = mapped_column(String(120))
