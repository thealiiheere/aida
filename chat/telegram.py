"""The parts of Telegram's Bot API the chat uses."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx2

from .formatting import split, to_html

log = logging.getLogger("aida.chat.telegram")

# httpx2 logs every request URL at INFO, and Telegram URLs contain the bot token.
logging.getLogger("httpx2").setLevel(logging.WARNING)

POLL_SECONDS = 50
TYPING_SECONDS = 4  # Telegram shows it for five
ATTEMPTS = 4
MAX_WAIT_SECONDS = 60


class TelegramError(Exception):
    """Safe to show: never contains the request URL or bot token."""

    def __init__(self, message: str, code: int | None = None, retry_after: int | None = None):
        super().__init__(message)
        self.code = code  # None if Telegram couldn't be reached
        self.retry_after = retry_after

    @property
    def temporary(self) -> bool:
        return self.code is None or self.code == 429 or self.code >= 500


class BotApi:
    def __init__(
        self,
        token: str,
        base_url: str,
        transport: httpx2.AsyncBaseTransport | None = None,
        retry_seconds: float = 1,
    ):
        self._url = f"{base_url.rstrip('/')}/bot{token}"
        self._client = httpx2.AsyncClient(timeout=15, transport=transport)
        self._retry_seconds = retry_seconds

    async def close(self) -> None:
        await self._client.aclose()

    async def updates(self, offset: int | None) -> list[dict]:
        """Wait for new messages. Asking again from a later offset confirms these ones."""
        params = {"timeout": POLL_SECONDS, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        return await self._request("getUpdates", params, timeout=POLL_SECONDS + 15)

    async def call(self, method: str, **params) -> dict:
        """Call a method, trying again if Telegram is busy or out of reach."""
        wait = self._retry_seconds
        for attempt in range(1, ATTEMPTS + 1):
            try:
                return await self._request(method, params)
            except TelegramError as exc:
                if not exc.temporary or attempt == ATTEMPTS:
                    raise
                log.info("%s failed, trying again: %s", method, exc)
                await asyncio.sleep(min(exc.retry_after or wait, MAX_WAIT_SECONDS))
                wait *= 2

    async def send(self, chat_id: int | str, markdown: str, reply_to: int | None = None) -> int:
        """Send a message, in pieces if it is long. Returns the last one's id."""
        message_id = 0
        for piece in split(markdown):
            params = {"chat_id": chat_id, "link_preview_options": {"is_disabled": True}}
            if reply_to is not None:
                params["reply_parameters"] = {
                    "message_id": reply_to,
                    "allow_sending_without_reply": True,
                }
            try:
                sent = await self.call(
                    "sendMessage", **params, text=to_html(piece), parse_mode="HTML"
                )
            except TelegramError as exc:
                if exc.code != 400 or "parse entities" not in str(exc):
                    raise
                sent = await self.call("sendMessage", **params, text=piece)  # as it was written
            message_id = sent["message_id"]
        return message_id

    @asynccontextmanager
    async def typing(self, chat_id: int) -> AsyncIterator[None]:
        """Show the customer that an answer is being written."""
        task = asyncio.create_task(self._type(chat_id))
        try:
            yield
        finally:
            task.cancel()
            await asyncio.wait([task])

    async def _type(self, chat_id: int) -> None:
        while True:
            try:
                await self._request("sendChatAction", {"chat_id": chat_id, "action": "typing"})
            except TelegramError as exc:
                log.debug("typing not shown: %s", exc)
            await asyncio.sleep(TYPING_SECONDS)

    async def _request(self, method: str, params: dict, timeout: float = 15):
        try:
            response = await self._client.post(
                f"{self._url}/{method}", json=params, timeout=timeout
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
            raise TelegramError(
                body.get("description") or f"HTTP {response.status_code}",
                body.get("error_code", response.status_code),
                (body.get("parameters") or {}).get("retry_after"),
            )
        return body["result"]
