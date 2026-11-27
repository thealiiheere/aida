"""Aida's side of one customer's chat: Claude, the store's tools, and the replies.

Each step is saved before the next one starts, so a restart never repeats an order or
drops an answer.
"""

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx2

from agent.history import Conversation, tool_uses
from agent.language import from_telegram, language_of
from agent.llm import Claude, LlmError
from agent.prompts import chat_prompt
from agent.tools import StoreTools, no_manager, run_tool, tool_result

from .config import ChatSettings
from .incoming import Incoming
from .storage import Chat, Storage
from .telegram import BotApi, TelegramError

log = logging.getLogger("aida.chat")

GREETING = (
    "Hello! Welcome to {store}. I'm Aida, your AI assistant. "
    "How can I help you with our catalog today?"
)
HICCUP = "Sorry, something went wrong on my side. Could you send that again in a minute?"
REFUSED = "I'm sorry, I can't help with that. What can I find for you in our store?"
STEPPING_IN = (
    "I've passed your order notes to {admin}. They are stepping into this chat right now."
)
TROUBLE = "Sorry, I still can't answer. Please try again a little later."
TROUBLE_PASSED_ON = (
    "Sorry, I still can't answer. I've passed your messages to {admin}, "
    "who will reply to you here."
)


@dataclass(frozen=True)
class Lines:
    """What Aida writes without asking Claude, in one language."""

    greeting: str
    hiccup: str
    refused: str
    stepping_in: str
    trouble: str
    trouble_passed_on: str
    not_passed_on: str


ENGLISH = Lines(
    GREETING, HICCUP, REFUSED, STEPPING_IN, TROUBLE, TROUBLE_PASSED_ON,
    "Sorry, that message could not be passed on. Please try again a little later.",
)  # fmt: skip
UZBEK = Lines(
    greeting=(
        "Assalomu alaykum! {store} do'koniga xush kelibsiz. Men Aida, sun'iy intellekt "
        "yordamchisiman. Katalogimizdan sizga nima topib beray?"
    ),
    hiccup="Kechirasiz, menda nosozlik bo'ldi. Bir daqiqadan keyin yana yozib yubora olasizmi?",
    refused="Kechirasiz, bunga yordam bera olmayman. Do'konimizdan sizga nima topib beray?",
    stepping_in=(
        "Buyurtma ma'lumotlaringizni {admin}ga yubordim. U hozir shu chatga qo'shilmoqda."
    ),
    trouble="Kechirasiz, hozircha javob bera olmayapman. Birozdan keyin yana urinib ko'ring.",
    trouble_passed_on=(
        "Kechirasiz, hozircha javob bera olmayapman. Xabarlaringizni {admin}ga yubordim, "
        "u sizga shu yerda javob beradi."
    ),
    not_passed_on="Kechirasiz, bu xabarni yetkazib bo'lmadi. Birozdan keyin yana urinib ko'ring.",
)
RUSSIAN = Lines(
    greeting=(
        "Здравствуйте! Добро пожаловать в {store}. Я Аида, ассистент с искусственным "
        "интеллектом. Что подобрать для вас из нашего каталога?"
    ),
    hiccup=(
        "Извините, у меня произошёл сбой. Отправьте, пожалуйста, сообщение ещё раз "
        "через минуту."
    ),
    refused="Извините, с этим я помочь не могу. Что подобрать для вас в нашем магазине?",
    stepping_in="Я передала данные вашего заказа: {admin}. Сейчас вам ответят в этом чате.",
    trouble="Извините, я пока не могу ответить. Попробуйте, пожалуйста, чуть позже.",
    trouble_passed_on=(
        "Извините, я пока не могу ответить. Я передала ваши сообщения: {admin}. "
        "Вам ответят здесь."
    ),
    not_passed_on="Извините, это сообщение не удалось передать. Попробуйте чуть позже.",
)
LINES = {"en": ENGLISH, "uz": UZBEK, "ru": RUSSIAN}

ADMIN_HELP = (
    "Reply to this message, or to the customer's messages that arrive below, and your reply "
    "goes to them. Aida stays quiet until you reply /resume, or for 24 hours."
)

START_NOTE = "The customer pressed Start."
INTERRUPTED = (
    "The system restarted while this was running, so it may or may not have worked. "
    "Check before doing it again."
)

MAX_TOOL_ROUNDS = 8
# After this long, prices, stock and orders in the conversation are out of date.
IDLE_SECONDS = 6 * 3600
MAX_HISTORY_CHARS = 150_000
MAX_EARLIER_CHARS = 6000
# Answering something much older than this would only confuse.
CATCH_UP_SECONDS = 10 * 60
DELIVERY_ATTEMPTS = 20


class Aida:
    def __init__(
        self,
        settings: ChatSettings,
        llm: Claude,
        api: BotApi,
        storage: Storage,
        store: httpx2.Client,
        clock: Callable[[], float] = time.time,
        retry_seconds: float = 15,
    ):
        self._settings = settings
        self._language = settings.chat_language
        self._llm = llm
        self._api = api
        self._storage = storage
        self._store = store
        self._clock = clock
        self._retry_seconds = retry_seconds
        key = settings.agent_api_key
        self._api_key = key.get_secret_value() if key else None
        self._system = [
            {
                "type": "text",
                "text": chat_prompt(settings.store_name, settings.admin_name),
                "cache_control": {"type": "ephemeral"},
            }
        ]

    def lines_for(self, chat: Chat) -> Lines:
        """Fixed lines in the language the customer last wrote in."""
        for message in reversed(chat.conversation.messages):
            if message["role"] != "user":
                continue
            for block in reversed(message["content"]):
                text = block.get("text", "") if block.get("type") == "text" else ""
                if text and not (text.startswith("[") and text.endswith("]")):
                    return LINES[language_of(text, self._language)]
        return LINES[self._language]

    async def reply(self, chat: Chat, incoming: list[Incoming], handled: list[int]) -> None:
        """Answer the messages the customer has sent since Aida last spoke."""
        self._begin(chat)
        await self._note_orders(chat)
        if any(message.start for message in incoming):
            chat.conversation.add_note(START_NOTE)
            start = next(message for message in incoming if message.start)
            lines = LINES[from_telegram(start.language, self._language)]
            greeting = lines.greeting.format(store=self._settings.store_name)
            await self._say(chat, greeting)
        rest = [message for message in incoming if not message.start]
        for message in rest:
            add_message(chat, message)
        chat.owed = bool(rest)
        self._storage.save(chat, handled)
        if chat.owed:
            await self._answer(chat)

    async def catch_up(self, chat: Chat) -> None:
        """Finish what a restart interrupted."""
        if self._close_calls(chat):
            await self._note_orders(chat)  # one may have been placed
            self._storage.save(chat)
        if not (chat.unsent or chat.owed):
            return
        if self._clock() - chat.active_at > CATCH_UP_SECONDS:
            chat.unsent, chat.owed = "", False
            self._storage.save(chat)
            return
        await self._deliver(chat)
        # If Aida spoke last, that was all there was to send.
        if chat.owed and chat.conversation.messages[-1]["role"] == "user":
            if chat.paused_at is None:
                await self._answer(chat)
            else:
                admin = self._settings.admin_name
                await self._say(chat, self.lines_for(chat).stepping_in.format(admin=admin))
        chat.owed = False
        self._storage.save(chat)

    async def apologise(self, chat: Chat) -> None:
        """After a fault in this program."""
        self._close_calls(chat)
        chat.owed = False
        if chat.conversation.messages and chat.conversation.messages[-1]["role"] == "user":
            await self._say(chat, self.lines_for(chat).hiccup)
        else:
            await self._send(chat, self.lines_for(chat).hiccup)

    # --- The conversation -------------------------------------------------------

    def _begin(self, chat: Chat) -> None:
        """Start a new conversation if the last one is old or long, and recall its end."""
        now = self._clock()
        messages = chat.conversation.messages
        idle = now - chat.active_at
        if messages and (
            idle > IDLE_SECONDS or len(json.dumps(messages)) > MAX_HISTORY_CHARS
        ):
            earlier = transcript(chat.conversation, MAX_EARLIER_CHARS)
            chat.conversation = Conversation()
            chat.conversation.add_note(
                f"How this chat went until {_ago(idle)}. Prices, stock and orders may have "
                f"changed since.\n{earlier}"
            )
            chat.orders = ""
        chat.active_at = now

    def _close_calls(self, chat: Chat) -> bool:
        """Give tool calls that were cut short a result: Claude needs one for each."""
        messages = chat.conversation.messages
        if not messages or messages[-1]["role"] != "assistant":
            return False
        calls = tool_uses(messages[-1]["content"])
        if calls:
            result = {"ok": False, "error": {"code": "interrupted", "message": INTERRUPTED}}
            chat.conversation.add_tool_results([tool_result(call, result) for call in calls])
        return bool(calls)

    async def _note_orders(self, chat: Chat) -> None:
        """Tell Aida when the customer's orders have changed: paid, expired, or made by phone."""
        headers = {"X-API-Key": self._api_key} if self._api_key else {}
        try:
            response = await asyncio.to_thread(
                self._store.get, f"/api/customers/{chat.id}/orders", headers=headers
            )
            if response.status_code == 429:
                return  # looked up moments ago
            response.raise_for_status()
            orders = response.json()
        except (httpx2.HTTPError, ValueError) as exc:
            log.warning("orders not looked up: %s", type(exc).__name__)
            return
        lines = [
            f"#{order['order_id']} {order['status']}, ${order['total_amount']:.2f}: "
            + ", ".join(f"{item['title']} × {item['quantity']}" for item in order["items"])
            for order in orders
        ]
        if (found := "; ".join(lines)) and found != chat.orders:
            chat.orders = found
            chat.conversation.add_note(f"This customer's newest orders in the store: {found}")

    async def _answer(self, chat: Chat) -> None:
        tools = StoreTools(
            client=self._store,
            api_key=self._api_key,
            customer_telegram_id=str(chat.id),
            customer_telegram_username=chat.username,
            live=True,
        )
        tools.customer_said(_last_said(chat.conversation))
        async with self._api.typing(chat.id):
            for _ in range(MAX_TOOL_ROUNDS):
                calls = await self._ask(chat, tools)
                if not calls:
                    break
                results = await asyncio.gather(*(self._use(chat, tools, call) for call in calls))
                chat.conversation.add_tool_results(list(results))
                self._storage.save(chat)
            else:
                await self._say(chat, self._instead(chat, self.lines_for(chat).hiccup))
        chat.owed = False
        self._storage.save(chat)

    async def _ask(self, chat: Chat, tools: StoreTools) -> list[dict]:
        """Get and send Claude's next reply. Returns the tools it wants run."""
        try:
            reply = await self._llm.reply(self._system, tools.definitions, chat.conversation)
        except LlmError as exc:
            log.warning("no reply from Claude: %s", exc)
            await self._no_reply(chat, tools)
            return []
        chat.failures = 0
        if reply.stop_reason == "refusal":
            await self._say(chat, self._instead(chat, self.lines_for(chat).refused))
            return []
        content = reply.content
        if reply.stop_reason != "tool_use":
            # Cut off mid-call, so its input can't be trusted.
            content = [block for block in content if block["type"] != "tool_use"]
        if not chat.conversation.add_assistant(content):
            await self._say(chat, self._instead(chat, self.lines_for(chat).hiccup))
            return []
        content = chat.conversation.messages[-1]["content"]
        await self._send(chat, _text(content))
        return tool_uses(content)

    def _instead(self, chat: Chat, line: str) -> str:
        """What to say when Claude has nothing: after handing over, that it was done."""
        if chat.paused_at is not None:
            return self.lines_for(chat).stepping_in.format(admin=self._settings.admin_name)
        return line

    async def _no_reply(self, chat: Chat, tools: StoreTools) -> None:
        chat.failures += 1
        line = self.lines_for(chat).hiccup
        if chat.failures > 1 and chat.paused_at is None:
            summary = " / ".join(transcript(chat.conversation, 3000).splitlines())
            result = await run_tool(
                tools,
                "escalate_to_human",
                {
                    "reason": "Aida can't answer, because Claude isn't responding.",
                    "context_summary": summary,
                },
            )
            if await self._hand_over(chat, result):
                admin = self._settings.admin_name
                line = self.lines_for(chat).trouble_passed_on.format(admin=admin)
                await self._say(chat, line)
                return
            line = self.lines_for(chat).trouble
        await self._say(chat, self._instead(chat, line))

    # --- Tools --------------------------------------------------------------------

    async def _use(self, chat: Chat, tools: StoreTools, call: dict) -> dict:
        result = await run_tool(tools, call["name"], call["input"])
        if call["name"] == "escalate_to_human":
            if not await self._hand_over(chat, result):
                result = no_manager()
        elif call["name"] == "notify_admin_order":
            self._remember(chat, result)
        return tool_result(call, result)

    async def _hand_over(self, chat: Chat, result: dict) -> bool:
        """Leave the chat to a person. False if none was told."""
        alert = self._remember(chat, result)
        if alert is None:
            return False
        chat.paused_at = self._clock()
        self._storage.save(chat)
        log.info("chat %d handed over to a person", chat.id)
        try:
            admin = self._settings.admin_chat_id
            note = await self._api.send(admin, ADMIN_HELP, reply_to=alert)
            self._storage.remember(note, chat.id)
        except TelegramError as exc:
            log.error("the admin was alerted, but not told how to reply: %s", exc)
        return True

    def _remember(self, chat: Chat, result: dict) -> int | None:
        """Let the admin answer the customer by replying to the store's message about them."""
        if not (result["ok"] and result["data"].get("delivered")):
            return None
        message_id = result["data"]["telegram_message_id"]
        self._storage.remember(message_id, chat.id)
        return message_id

    # --- Sending --------------------------------------------------------------------

    async def _say(self, chat: Chat, line: str) -> None:
        """Send a fixed line, and remember it as something Aida said."""
        chat.conversation.add_assistant([{"type": "text", "text": line}])
        await self._send(chat, line)

    async def _send(self, chat: Chat, text: str) -> None:
        chat.unsent = text
        self._storage.save(chat)
        await self._deliver(chat)

    async def _deliver(self, chat: Chat) -> None:
        if not chat.unsent:
            return
        for attempt in range(1, DELIVERY_ATTEMPTS + 1):
            try:
                await self._api.send(chat.id, chat.unsent)
                break
            except TelegramError as exc:
                if not exc.temporary or attempt == DELIVERY_ATTEMPTS:
                    log.error("reply to chat %d not delivered: %s", chat.id, exc)
                    break
                log.warning("reply to chat %d not delivered yet: %s", chat.id, exc)
                await asyncio.sleep(self._retry_seconds)
        chat.unsent = ""
        self._storage.save(chat)


def add_message(chat: Chat, message: Incoming) -> None:
    for note in message.notes:
        chat.conversation.add_note(note)
    if message.text:
        chat.conversation.add_customer(message.text)


def transcript(conversation: Conversation, limit: int) -> str:
    """The end of a conversation as plain text, without thinking and tool calls."""
    lines = []
    for message in conversation.messages:
        for block in message["content"]:
            if block["type"] != "text":
                continue
            text = block["text"].strip()
            if message["role"] == "assistant":
                lines.append(f"Aida: {_quoted(text)}")
            elif text.startswith("["):  # a note
                lines.append(text[1:-1])
            else:
                lines.append(f"Customer: {_quoted(text)}")
    text = "\n".join(lines)
    if len(text) <= limit:
        return text
    return text[-limit:].partition("\n")[2] or text[-limit:]


def _quoted(text: str) -> str:
    # One line, with quotes escaped, so nobody can add a line of someone else's.
    return json.dumps(text, ensure_ascii=False)


def _text(content: list[dict]) -> str:
    return "".join(block["text"] for block in content if block["type"] == "text").strip()


def _last_said(conversation: Conversation) -> str:
    for message in reversed(conversation.messages):
        if message["role"] == "user":
            for block in reversed(message["content"]):
                if block["type"] == "text" and not block["text"].startswith("["):
                    return block["text"]
    return ""


def _ago(seconds: float) -> str:
    if seconds < 3600:
        return "now"
    hours = round(seconds / 3600)
    if hours < 48:
        return f"{hours} hours ago"
    return f"{round(hours / 24)} days ago"
