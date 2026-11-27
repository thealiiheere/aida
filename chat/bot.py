"""The Telegram side: receives messages, and passes them to Aida or between people.

Each chat has at most one worker, which handles its messages in order. A chat is either
Aida's or, after an escalation, a person's: then the customer's messages are forwarded to
the admin's chat, and the admin's replies there are copied back.
"""

import asyncio
import logging
import re
import time
from collections.abc import Callable
from functools import partial
from weakref import WeakValueDictionary

from app.limits import RateLimiter

from .aida import Aida, add_message
from .config import ChatSettings
from .incoming import Incoming, read
from .storage import Chat, Storage
from .telegram import BotApi, TelegramError

log = logging.getLogger("aida.chat")

REPLIES_PER_MINUTE = 10
MAX_GATHER_SECONDS = 5
MAX_MESSAGES = 10  # answered at once; a flood's older ones are left out
MAX_POLL_WAIT_SECONDS = 60
STOP_SECONDS = 20
# An admin who forgets to hand a chat back must not leave the customer with nobody.
PAUSE_SECONDS = 24 * 3600

BACK_NOTE = "You are handling this chat again."
HELP = (
    "To answer a customer, reply to one of their messages or to an alert about them. "
    "To hand them back to Aida, reply /resume to it."
)
PRIVATE_HELP = (
    f"{HELP}\n\nThis is the admin chat, so Aida doesn't answer here. To try Aida from this "
    "account, make a group the admin chat instead."
)
PAUSED = "Aida is quiet in this chat now. Reply /resume to hand it back."
RESUMED = "Aida is answering this customer again."
NOT_PAUSED = "Aida is already answering this customer."
RESUMED_ALONE = "24 hours have passed, so Aida is answering {customer} again."


class Bot:
    def __init__(
        self,
        settings: ChatSettings,
        api: BotApi,
        aida: Aida,
        storage: Storage,
        clock: Callable[[], float] = time.time,
        quiet_seconds: float = 1,
        retry_seconds: float = 15,
    ):
        self._settings = settings
        self._api = api
        self._aida = aida
        self._storage = storage
        self._clock = clock
        self._quiet_seconds = quiet_seconds
        self._retry_seconds = retry_seconds
        self._limiter = RateLimiter(REPLIES_PER_MINUTE)
        self._me: dict = {}
        self._workers: dict[int, asyncio.Task] = {}
        # One object per chat while anyone is using it, so nobody works on a stale copy.
        self._chats: WeakValueDictionary[int, Chat] = WeakValueDictionary()

    async def run(self) -> None:
        """Runs until cancelled. Raises TelegramError if the token is wrong."""
        try:
            self._me = await self._keep_trying(partial(self._api.call, "getMe"))
            log.info("answering customers as @%s", self._me.get("username"))
            for chat_id in self._storage.unfinished():
                self._wake(chat_id)
            offset = None
            while True:
                updates = await self._keep_trying(partial(self._api.updates, offset))
                for update in updates:
                    self._receive(update)
                    offset = update["update_id"] + 1
        finally:
            await self._stop()

    async def _keep_trying(self, request):
        wait = 1
        while True:
            try:
                return await request()
            except TelegramError as exc:
                if exc.code in (401, 404):
                    raise  # the token is wrong, and stays wrong
                if exc.code == 409:
                    log.error("another program is reading this bot's messages: %s", exc)
                else:
                    log.warning("no messages from Telegram: %s", exc)
                await asyncio.sleep(exc.retry_after or wait)
                wait = min(wait * 2, MAX_POLL_WAIT_SECONDS)

    async def _stop(self) -> None:
        """Let answers in progress finish. What doesn't is picked up at the next start."""
        workers = list(self._workers.values())
        if workers:
            log.info("finishing %d chats", len(workers))
            _, unfinished = await asyncio.wait(workers, timeout=STOP_SECONDS)
            for worker in unfinished:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    # --- Receiving ------------------------------------------------------------------

    def _receive(self, update: dict) -> None:
        message = update.get("message")
        if not message:
            return
        sender = message.get("from") or {}
        # Anonymous group admins write through one of Telegram's bots, for a "sender chat".
        if sender.get("is_bot") and "sender_chat" not in message:
            return
        chat_id = message["chat"]["id"]
        if not self._is_admin(chat_id) and message["chat"]["type"] != "private":
            return
        if self._storage.receive(update["update_id"], chat_id, message):
            self._wake(chat_id)

    def _is_admin(self, chat_id: int) -> bool:
        return str(chat_id) == self._settings.admin_chat_id

    def _wake(self, chat_id: int) -> None:
        if chat_id not in self._workers:
            self._workers[chat_id] = asyncio.create_task(self._work(chat_id))

    async def _work(self, chat_id: int) -> None:
        serve = self._serve_admin if self._is_admin(chat_id) else self._serve_customer
        try:
            await serve(chat_id)
        except Exception:
            log.exception("chat %d failed", chat_id)
        finally:
            # With no await since the last look for messages, so none can slip through.
            del self._workers[chat_id]

    def _chat(self, chat_id: int) -> Chat:
        chat = self._chats.get(chat_id)
        if chat is None:
            chat = self._chats[chat_id] = self._storage.load(chat_id)
        return chat

    # --- Customers ------------------------------------------------------------------

    async def _serve_customer(self, chat_id: int) -> None:
        chat = self._chat(chat_id)
        try:
            async with chat.lock:
                await self._aida.catch_up(chat)
        except Exception:
            log.exception("chat %d could not be picked up again", chat_id)
            chat.owed = False
        while self._storage.waiting(chat_id):
            if chat.paused_at is None:
                await self._gather(chat_id)
            async with chat.lock:
                waiting = self._storage.waiting(chat_id)
                chat.username = waiting[-1][1]["from"].get("username")
                try:
                    await self._handle(chat, waiting)
                except Exception:
                    log.exception("a message in chat %d could not be answered", chat_id)
                    self._storage.handled([update_id for update_id, _ in waiting])
                    await self._aida.apologise(chat)

    async def _gather(self, chat_id: int) -> None:
        """People write one thought in several messages, so wait until they pause."""
        give_up = time.monotonic() + MAX_GATHER_SECONDS
        count = 0
        while count != (count := len(self._storage.waiting(chat_id))):
            if time.monotonic() > give_up:
                break
            await asyncio.sleep(self._quiet_seconds)
        while wait := self._limiter.wait(chat_id):
            await asyncio.sleep(wait)

    async def _handle(self, chat: Chat, waiting: list[tuple[int, dict]]) -> None:
        if chat.paused_at is not None:
            if self._clock() - chat.paused_at < PAUSE_SECONDS and self._settings.admin_chat_id:
                await self._forward(chat, waiting)
                return
            await self._resume_alone(chat)
        handled = [update_id for update_id, _ in waiting]
        incoming = [m for _, message in waiting[-MAX_MESSAGES:] if (m := read(message))]
        if incoming:
            await self._aida.reply(chat, incoming, handled)
        else:
            self._storage.handled(handled)

    async def _forward(self, chat: Chat, waiting: list[tuple[int, dict]]) -> None:
        for update_id, message in waiting:
            try:
                sent = await self._api.call(
                    "forwardMessage",
                    chat_id=self._settings.admin_chat_id,
                    from_chat_id=chat.id,
                    message_id=message["message_id"],
                )
            except TelegramError as exc:
                if exc.temporary:
                    log.warning("not forwarded to the admin yet: %s", exc)
                    await asyncio.sleep(self._retry_seconds)
                    return  # the rest are still waiting, in order
                log.error("not forwarded to the admin: %s", exc)
                self._storage.handled([update_id])
                await self._tell(chat.id, self._aida.lines_for(chat).not_passed_on)
                continue
            self._storage.remember(sent["message_id"], chat.id)
            add_message(chat, read(message) or Incoming())
            chat.active_at = self._clock()
            self._storage.save(chat, [update_id])

    async def _resume_alone(self, chat: Chat) -> None:
        self._take_back(chat)
        customer = f"@{chat.username}" if chat.username else f"customer {chat.id}"
        if self._settings.admin_chat_id:
            note = await self._tell(
                self._settings.admin_chat_id, RESUMED_ALONE.format(customer=customer)
            )
            if note:
                self._storage.remember(note, chat.id)

    def _take_back(self, chat: Chat) -> None:
        chat.paused_at = None
        chat.failures = 0
        chat.conversation.add_note(BACK_NOTE)
        self._storage.save(chat)

    # --- The admin --------------------------------------------------------------------

    async def _serve_admin(self, chat_id: int) -> None:
        while waiting := self._storage.waiting(chat_id):
            for update_id, message in waiting:
                try:
                    await self._from_admin(message)
                except Exception:
                    log.exception("the admin's message could not be handled")
                self._storage.handled([update_id])

    async def _from_admin(self, message: dict) -> None:
        replied = message.get("reply_to_message") or {}
        customer = self._storage.customer_of(replied.get("message_id"))
        resume = re.match(r"/resume(@\w+)?\b", message.get("text") or "")
        if customer is None:
            private = message["chat"]["type"] == "private"
            about_me = (replied.get("from") or {}).get("id") == self._me.get("id")
            if private or resume or about_me:
                await self._answer_admin(message, PRIVATE_HELP if private else HELP)
            return
        chat = self._chat(customer)
        if resume:
            await self._resume(chat, message)
        else:
            await self._pass_on(chat, message)

    async def _resume(self, chat: Chat, message: dict) -> None:
        async with chat.lock:
            paused = chat.paused_at is not None
            if paused:
                self._take_back(chat)
        await self._answer_admin(message, RESUMED if paused else NOT_PAUSED)

    async def _pass_on(self, chat: Chat, message: dict) -> None:
        try:
            await self._api.call(
                "copyMessage",
                chat_id=chat.id,
                from_chat_id=message["chat"]["id"],
                message_id=message["message_id"],
            )
        except TelegramError as exc:
            await self._answer_admin(message, f"Not delivered: {exc}")
            return
        if chat.paused_at is None:
            await self._answer_admin(message, PAUSED)
        chat.paused_at = self._clock()  # at once, even if Aida is in the middle of answering
        said = read(message) or Incoming()
        text = said.text or "(something other than text)"
        async with chat.lock:
            chat.conversation.add_note(
                f"{self._settings.admin_name} wrote to the customer here: “{text}”"
            )
            chat.active_at = self._clock()
            self._storage.save(chat)

    async def _answer_admin(self, message: dict, text: str) -> None:
        await self._tell(message["chat"]["id"], text, reply_to=message["message_id"])

    async def _tell(self, chat_id: int | str, text: str, reply_to: int | None = None) -> int | None:
        try:
            return await self._api.send(chat_id, text, reply_to)
        except TelegramError as exc:
            log.error("message to chat %s not delivered: %s", chat_id, exc)
            return None
