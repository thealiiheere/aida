"""Answers customers in Telegram.

    uv run python -m chat

Telegram gives a bot's messages to one program at a time, so run a single copy.
"""

import asyncio
import logging
import sys
from contextlib import suppress

import httpx2

from agent.llm import make_llm

from .aida import Aida
from .bot import Bot
from .config import ChatSettings
from .storage import Storage
from .telegram import BotApi, TelegramError

log = logging.getLogger("aida.chat")


async def serve(settings: ChatSettings) -> None:
    llm = make_llm(settings, patient=True)
    api = BotApi(settings.telegram_bot_token.get_secret_value(), settings.telegram_api_url)
    storage = Storage(settings.chat_database)
    store = httpx2.Client(base_url=settings.backend_url, timeout=15)
    if not llm.has_credentials:
        log.warning("%s not set: customers will be told Aida can't answer.", llm.key_name)
    if not settings.admin_chat_id:
        log.warning("ADMIN_CHAT_ID not set: customers can't be handed over to a person.")
    try:
        aida = Aida(settings, llm, api, storage, store)
        await Bot(settings, api, aida, storage).run()
    finally:
        await llm.close()
        await api.close()
        store.close()
        storage.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(message)s")
    settings = ChatSettings()
    if settings.telegram_bot_token is None:
        log.error("Set TELEGRAM_BOT_TOKEN in .env first.")
        return 1
    with suppress(KeyboardInterrupt):
        try:
            asyncio.run(serve(settings))
        except TelegramError as exc:
            log.error("Telegram refused TELEGRAM_BOT_TOKEN: %s", exc)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
