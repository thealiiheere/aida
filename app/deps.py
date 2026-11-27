import secrets
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import APIKeyHeader
from sqlalchemy.orm import Session

from .config import Settings
from .limits import RateLimiter
from .models import Order
from .telegram import Delivery, TelegramError, TelegramNotifier

_api_key = APIKeyHeader(name="X-API-Key", auto_error=False)


def api_error(status: int, code: str, message: str, **extra) -> HTTPException:
    return HTTPException(status, detail={"code": code, "message": message, **extra})


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _telegram(request: Request) -> TelegramNotifier:
    return request.app.state.telegram


def _session(request: Request) -> Iterator[Session]:
    with Session(request.app.state.engine) as db:
        yield db


SettingsDep = Annotated[Settings, Depends(_settings)]
TelegramDep = Annotated[TelegramNotifier, Depends(_telegram)]
Db = Annotated[Session, Depends(_session)]


def require_api_key(
    settings: SettingsDep, key: Annotated[str | None, Security(_api_key)] = None
) -> None:
    expected = settings.agent_api_key
    if expected is None:
        return
    if not secrets.compare_digest((key or "").encode(), expected.get_secret_value().encode()):
        raise api_error(401, "invalid_api_key", "Missing or wrong X-API-Key header.")


class Limits:
    """Five calls a minute per Telegram account, counted separately for each endpoint."""

    def __init__(self, request: Request):
        self._limiter: RateLimiter = request.app.state.limiter
        self._endpoint = request.scope["endpoint"]

    def check(self, telegram_id: str | None) -> None:
        if telegram_id and (seconds := self._limiter.wait(self._endpoint, telegram_id)):
            error = api_error(
                429, "rate_limited", f"Too many requests. Try again in {seconds} seconds."
            )
            error.headers = {"Retry-After": str(seconds)}
            raise error


LimitsDep = Annotated[Limits, Depends(Limits)]


def get_order(db: Session, order_id: int) -> Order:
    order = db.get(Order, order_id)
    if order is None:
        raise api_error(404, "order_not_found", f"There is no order #{order_id}.")
    return order


def deliver(telegram: TelegramNotifier, html: str, document: str | None = None) -> Delivery:
    try:
        return telegram.send_admin(html, document)
    except TelegramError as exc:
        raise api_error(
            502, "telegram_delivery_failed", f"The admin was not notified: {exc}."
        ) from None
