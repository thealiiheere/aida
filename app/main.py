import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx2
from fastapi import Depends, FastAPI
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .db import create_tables, make_engine
from .deps import require_api_key
from .lifecycle import expiry_worker
from .limits import RateLimiter
from .routers import (
    admin,
    click,
    console,
    customers,
    orders,
    payme,
    payments,
    products,
    telephony,
)
from .seed import seed_if_empty
from .summary import Summariser
from .telegram import TelegramNotifier

log = logging.getLogger("aida")

DESCRIPTION = """
Demo store backend for **Aida**, the Telegram voice and chat operator.

Typical call: search → check stock → create order → create invoice → notify admin.
An order's stock is held for 15 minutes, then released if nobody has paid.

Payment links open a demo checkout page. Click and Payme confirm real payments through
their webhooks, once their keys are set.

If `AGENT_API_KEY` is set, use **Authorize** to send it as `X-API-Key`.
"""


def create_app(
    settings: Settings | None = None,
    telegram_transport: httpx2.BaseTransport | None = None,
    claude_client: httpx2.Client | None = None,
) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(message)s")
        engine = make_engine(settings.database_url)
        create_tables(engine)
        with Session(engine) as db:
            seed_if_empty(db)
        telegram = TelegramNotifier(settings, telegram_transport)
        if not telegram.configured:
            log.warning("TELEGRAM_BOT_TOKEN or ADMIN_CHAT_ID not set: admin messages are dry runs.")
        if settings.agent_api_key is None:
            log.warning("AGENT_API_KEY not set: anyone who can reach this server can use the API.")
        app.state.settings = settings
        app.state.engine = engine
        app.state.telegram = telegram
        app.state.limiter = RateLimiter()
        app.state.summariser = summariser = Summariser(settings, claude_client)
        if not summariser.configured:
            key = {"openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY"}.get(
                settings.llm_provider, "ANTHROPIC_API_KEY"
            )
            log.warning("%s not set: calls are logged without a summary.", key)
        if settings.real_payments and settings.uzs_per_usd is None:
            log.warning("UZS_PER_USD not set: Click and Payme payments will be refused.")
        stop = asyncio.Event()
        worker = asyncio.create_task(expiry_worker(engine, telegram, stop))
        yield
        stop.set()
        await worker
        summariser.close()
        telegram.close()
        engine.dispose()

    app = FastAPI(
        title="Aida Store Backend", version="0.1.0", description=DESCRIPTION, lifespan=lifespan
    )
    for module in (products, orders, customers, admin, telephony):
        app.include_router(module.router, dependencies=[Depends(require_api_key)])
    # Opened by customers and payment gateways. The gateways prove who they are.
    for module in (payments, click, payme):
        app.include_router(module.router)
    # Checks the key itself; its page opens without one and asks for it.
    app.include_router(console.router)
    app.include_router(console.page)

    @app.get("/", include_in_schema=False)
    def index() -> RedirectResponse:
        return RedirectResponse("/console")

    return app


app = create_app()
