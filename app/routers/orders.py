from collections import Counter

from fastapi import APIRouter
from sqlalchemy import select

from ..deps import Db, LimitsDep, api_error, get_order
from ..lifecycle import reserved_until, take_stock
from ..models import Invoice, Order, OrderItem, Product
from ..schemas import (
    ErrorOut,
    InvoiceOut,
    OrderIn,
    OrderItemOut,
    OrderOut,
    OrderStatusOut,
    TelegramId,
)

router = APIRouter(prefix="/api/orders", tags=["Orders"])


def invoice_out(invoice: Invoice) -> InvoiceOut:
    return InvoiceOut(
        invoice_id=invoice.id,
        order_id=invoice.order_id,
        provider=invoice.provider,
        amount=invoice.amount,
        payment_url=invoice.payment_url,
        status=invoice.status,
    )


def order_out(order: Order) -> OrderOut:
    return OrderOut(
        order_id=order.id,
        status=order.status,
        customer_telegram_id=order.customer_telegram_id,
        customer_telegram_username=order.customer_telegram_username,
        customer_name=order.customer_name,
        delivery_address=order.delivery_address,
        items=[
            OrderItemOut(
                product_id=item.product_id,
                title=item.product.title,
                variant=item.variant,
                quantity=item.quantity,
                unit_price=item.unit_price,
                line_total=item.unit_price * item.quantity,
                state=item.state,
            )
            for item in order.items
        ],
        total_amount=order.total_amount,
        invoices=[invoice_out(invoice) for invoice in order.invoices],
        created_at=order.created_at,
        reserved_until=reserved_until(order),
    )


@router.post(
    "",
    status_code=201,
    responses={404: {"model": ErrorOut}, 409: {"model": ErrorOut}, 429: {"model": ErrorOut}},
)
def create_order(body: OrderIn, db: Db, limits: LimitsDep) -> OrderOut:
    """Create a pending order and hold its stock for 15 minutes. Tool: `create_order`.

    Prices always come from the catalog. If any item is short, nothing is held.
    """
    limits.check(body.customer_telegram_id)
    wanted = Counter()
    for item in body.items:
        wanted[item.product_id] += item.quantity

    products = {p.id: p for p in db.scalars(select(Product).where(Product.id.in_(wanted)))}
    if missing := sorted(wanted.keys() - products.keys()):
        raise api_error(
            404,
            "product_not_found",
            f"No product with id {', '.join(map(str, missing))}.",
            product_ids=missing,
        )

    order = Order(
        customer_telegram_id=body.customer_telegram_id,
        customer_telegram_username=body.customer_telegram_username,
        customer_name=body.customer_name,
        delivery_address=body.delivery_address,
        total_amount=sum(products[i.product_id].price * i.quantity for i in body.items),
        items=[
            OrderItem(
                product_id=i.product_id,
                quantity=i.quantity,
                unit_price=products[i.product_id].price,
                variant=i.variant,
            )
            for i in body.items
        ],
    )

    if short := take_stock(db, wanted):
        db.rollback()
        items = [
            {
                "product_id": p.id,
                "title": p.title,
                "requested": wanted[p.id],
                "available": p.stock_quantity,
            }
            for p in db.scalars(select(Product).where(Product.id.in_(short)).order_by(Product.id))
        ]
        summary = "; ".join(
            f"{i['title']}: {i['requested']} requested, {i['available']} available" for i in items
        )
        raise api_error(409, "insufficient_stock", f"Not enough stock. {summary}.", items=items)

    db.add(order)
    db.commit()
    return order_out(order)


@router.get(
    "/{order_id}/status", responses={404: {"model": ErrorOut}, 429: {"model": ErrorOut}}
)
def order_status(
    order_id: int, db: Db, limits: LimitsDep, customer_telegram_id: TelegramId | None = None
) -> OrderStatusOut:
    """Whether a customer's own order is paid. Tool: `get_order_status`.

    Someone else's order is reported as not found.
    """
    limits.check(customer_telegram_id)
    order = db.get(Order, order_id)
    if order is None or order.customer_telegram_id != customer_telegram_id:
        raise api_error(404, "order_not_found", f"This customer has no order #{order_id}.")
    return OrderStatusOut(
        order_id=order.id,
        status=order.status,
        total_amount=order.total_amount,
        reserved_until=reserved_until(order),
    )


@router.get("/{order_id}", responses={404: {"model": ErrorOut}})
def read_order(order_id: int, db: Db) -> OrderOut:
    """Order details, including payment status."""
    return order_out(get_order(db, order_id))
