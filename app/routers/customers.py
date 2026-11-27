from fastapi import APIRouter
from sqlalchemy import select

from ..deps import Db, LimitsDep, api_error
from ..models import Customer, Order
from ..schemas import CustomerIn, CustomerLookupIn, CustomerOut, ErrorOut, OrderOut, TelegramId
from .orders import order_out

router = APIRouter(prefix="/api/customers", tags=["Customers"])


@router.put("")
def link_customer(body: CustomerIn, db: Db) -> CustomerOut:
    """Link a phone number to a Telegram account, replacing any earlier link."""
    customer = db.merge(Customer(**body.model_dump()))
    db.commit()
    return CustomerOut.model_validate(customer)


@router.post("/lookup", responses={404: {"model": ErrorOut}})
def find_customer(body: CustomerLookupIn, db: Db) -> CustomerOut:
    """Find who is calling. POST keeps phone numbers out of URLs and access logs."""
    customer = db.get(Customer, body.phone)
    if customer is None:
        raise api_error(404, "customer_not_found", "No Telegram account is linked to this number.")
    return CustomerOut.model_validate(customer)


@router.get("/{telegram_id}/orders", responses={429: {"model": ErrorOut}})
def recent_orders(telegram_id: TelegramId, db: Db, limits: LimitsDep) -> list[OrderOut]:
    """A customer's five newest orders, newest first, wherever they were placed."""
    limits.check(telegram_id)
    orders = db.scalars(
        select(Order)
        .where(Order.customer_telegram_id == telegram_id)
        .order_by(Order.id.desc())
        .limit(5)
    )
    return [order_out(order) for order in orders]
