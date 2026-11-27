"""A fake Telegram with customers and an admin's group, and the chat bot running in it."""

import asyncio
import json

import httpx2

from agent.llm import Claude
from agent.openai_llm import OpenAiLlm
from chat.aida import Aida
from chat.bot import Bot
from chat.config import ChatSettings
from chat.storage import Storage
from chat.telegram import BotApi

from .conftest import CUSTOMER, TOKEN
from .fakes import FakeClaude, FakeOpenAI

GROUP = -100555
BOT_ID = 111


class FakeBotApi:
    """api.telegram.org, for the bot and the store alike."""

    def __init__(self):
        self.sent: list[dict] = []  # everything but polling and typing
        self.typing: list[int] = []
        self.problems: dict[str, list] = {}  # method -> answers to give first
        self.updates: list[dict] = []
        self.offsets: list[int | None] = []
        self._arrived = asyncio.Event()
        self._update_id = 500
        self._message_id = 1000

    # What people do

    def new_message(self, chat_id, sender: dict | None = None, **content) -> dict:
        self._message_id += 1
        private = chat_id > 0
        return {
            "message_id": self._message_id,
            "from": {"id": chat_id if private else 42, "is_bot": False, **(sender or {})},
            "chat": {"id": chat_id, "type": "private" if private else "supergroup"},
            **content,
        }

    def deliver(self, message: dict) -> dict:
        self._update_id += 1
        self.updates.append({"update_id": self._update_id, "message": message})
        self._arrived.set()
        return message

    # What the bot and the store do

    async def __call__(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/getUpdates"):
            return await self._poll(json.loads(request.content))
        return self.answer(request)

    def answer(self, request: httpx2.Request) -> httpx2.Response:
        method = request.url.path.rsplit("/", 1)[1]
        assert request.url.path == f"/bot{TOKEN}/{method}"
        if self.problems.get(method):
            problem = self.problems[method].pop(0)
            if isinstance(problem, Exception):
                raise problem
            return httpx2.Response(problem["error_code"], json={"ok": False, **problem})
        if method == "getMe":
            return _ok({"id": BOT_ID, "is_bot": True, "username": "aida_bot"})
        body = json.loads(request.content)
        if method == "sendChatAction":
            self.typing.append(body["chat_id"])
            return _ok(True)
        self._message_id += 1
        if "message_id" in body:  # of the message to forward or copy
            body["source"] = body.pop("message_id")
        self.sent.append({"method": method, "message_id": self._message_id, **body})
        return _ok({"message_id": self._message_id})

    async def _poll(self, body: dict) -> httpx2.Response:
        assert body["allowed_updates"] == ["message"] and body["timeout"] > 0
        self.offsets.append(body.get("offset"))
        if self.problems.get("getUpdates"):
            problem = self.problems["getUpdates"].pop(0)
            return httpx2.Response(problem["error_code"], json={"ok": False, **problem})
        if "offset" in body:
            self.updates = [u for u in self.updates if u["update_id"] >= body["offset"]]
        if not self.updates:
            self._arrived.clear()
            await self._arrived.wait()
        return _ok(list(self.updates))

    # What to look at

    def to(self, chat_id, method="sendMessage") -> list[dict]:
        return [m for m in self.sent if str(m["chat_id"]) == str(chat_id) and m["method"] == method]

    def texts(self, chat_id) -> list[str]:
        return [m["text"] for m in self.to(chat_id)]


def _ok(result) -> httpx2.Response:
    return httpx2.Response(200, json={"ok": True, "result": result})


def failure(code: int, description: str, retry_after: int | None = None) -> dict:
    problem = {"error_code": code, "description": description}
    return {**problem, "parameters": {"retry_after": retry_after}} if retry_after else problem


class World:
    """Runs the bot against a store. `async with` it again for a restart on the same files."""

    def __init__(self, store, tmp_path, *replies, admin=GROUP, **settings):
        self.telegram = FakeBotApi()
        self.claude = FakeClaude(*replies)
        self.openai = FakeOpenAI(*replies)
        self.store = store
        self.now = 1_800_000_000.0
        self.path = tmp_path / "chat.db"
        self.settings = ChatSettings(
            _env_file=None,
            store_name="Test Store",
            admin_name="Kim",
            telegram_bot_token=TOKEN,
            admin_chat_id=str(admin) if admin else None,
            anthropic_api_key="test-key",
            **settings,
        )
        self.quiet_seconds = 0.01

    async def __aenter__(self) -> "World":
        self.storage = Storage(self.path, clock=lambda: self.now)
        self.api = BotApi(
            TOKEN,
            self.settings.telegram_api_url,
            httpx2.MockTransport(self.telegram),
            retry_seconds=0,
        )
        if self.settings.llm_provider == "openai":
            self.llm = OpenAiLlm(
                self.settings,
                httpx2.AsyncClient(transport=httpx2.MockTransport(self.openai)),
                patient=True,
            )
        else:
            self.llm = Claude(
                self.settings,
                httpx2.AsyncClient(transport=httpx2.MockTransport(self.claude)),
                patient=True,
            )
        self.aida = Aida(
            self.settings, self.llm, self.api, self.storage, self.store,
            clock=lambda: self.now, retry_seconds=0,
        )  # fmt: skip
        self.bot = Bot(
            self.settings, self.api, self.aida, self.storage,
            clock=lambda: self.now, quiet_seconds=self.quiet_seconds, retry_seconds=0.01,
        )  # fmt: skip
        self._polls_before = len(self.telegram.offsets)
        self.task = asyncio.create_task(self.bot.run())
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()
        if exc[0] is None:
            assert self.claude.rejected == []

    async def stop(self) -> None:
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        await self.llm.close()
        await self.api.close()
        self.storage.close()

    async def crash(self) -> None:
        """Stop as a power cut would: in the middle of anything."""
        for worker in list(self.bot._workers.values()):
            worker.cancel()
        await asyncio.gather(*self.bot._workers.values(), return_exceptions=True)
        await self.stop()

    # What people do

    def says(self, text: str, user=CUSTOMER, **content) -> dict:
        sender = {"username": "dilnoza_k"} if str(user) == CUSTOMER else {}
        return self.telegram.deliver(
            self.telegram.new_message(int(user), sender, text=text, **content)
        )

    def sends(self, user=CUSTOMER, **content) -> dict:
        return self.telegram.deliver(self.telegram.new_message(int(user), **content))

    def admin_replies(self, text: str, to: dict, chat=GROUP) -> dict:
        replied = {"message_id": to["message_id"], "from": {"id": BOT_ID, "is_bot": True}}
        return self.telegram.deliver(
            self.telegram.new_message(chat, text=text, reply_to_message=replied)
        )

    # Time

    async def until(self, condition, seconds: float = 5) -> None:
        async with asyncio.timeout(seconds):
            while not condition():
                await asyncio.sleep(0.005)

    async def idle(self) -> None:
        """Until every message has been received and handled."""
        await self.until(
            lambda: len(self.telegram.offsets) > self._polls_before
            and not self.telegram.updates
            and not self.bot._workers
        )

    async def ask(self, text: str, **kwargs) -> str:
        """Say something, and return the bot's last message once it has finished."""
        self.says(text, **kwargs)
        await self.idle()
        return self.texts(kwargs.get("user", CUSTOMER))[-1]

    # What to look at

    def texts(self, user=CUSTOMER) -> list[str]:
        return self.telegram.texts(user)

    def chat(self, user=CUSTOMER):
        return self.storage.load(int(user))

    @property
    def admin_texts(self) -> list[str]:
        return self.telegram.texts(GROUP)

    @property
    def last_request(self) -> list[dict]:
        return self.claude.requests[-1]["messages"]
