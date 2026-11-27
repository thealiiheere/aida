"""Calls over real sockets: both servers running, providers and Twilio simulated."""

import asyncio
import json
from contextlib import contextmanager

import httpx2
import pytest

from app.config import Settings
from app.main import create_app as create_store
from tests.conftest import TOKEN, FakeTelegram
from tests.fakes import FakeClaude, Reply, Tool, voice_settings
from tests.simulation import Providers, TwilioCaller, free_port, serve
from voice.server import create_app as create_line

KEY = "store-key"
ORDER = {
    "customer_name": "Dilnoza Karimova",
    "delivery_address": "12 Amir Temur Street",
    "items": [{"product_id": 1, "quantity": 2}],
}


SUMMARY = {
    "customer_intent": "Wanted earbuds.",
    "items_discussed": "Wireless Earbuds.",
    "outcome": "Inquiry Only",
}


def summarising_claude(request: httpx2.Request) -> httpx2.Response:
    """The store's own Claude client, which only writes summaries."""
    content = [{"type": "text", "text": json.dumps(SUMMARY)}]
    return httpx2.Response(
        200,
        json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": content, "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )  # fmt: skip


class World:
    """The store backend, the phone line and the providers, all on local ports."""

    def __init__(self, tmp_path):
        self.telegram = FakeTelegram()
        self.database = f"sqlite:///{(tmp_path / 'e2e.db').as_posix()}"

    @contextmanager
    def running(self, *replies, **settings):
        self.claude = FakeClaude(*replies)
        self.providers = Providers(self.claude)
        store = create_store(
            Settings(
                _env_file=None,
                store_name="Test Store",
                database_url=self.database,
                payment_base_url="http://localhost:8000/pay",
                agent_api_key=KEY,
                telegram_bot_token=TOKEN,
                admin_chat_id="555",
                admin_username="shop_admin",
                anthropic_api_key="test-key",
                anthropic_base_url=None,
            ),
            telegram_transport=httpx2.MockTransport(self.telegram),
            claude_client=httpx2.Client(transport=httpx2.MockTransport(summarising_claude)),
        )
        port = free_port()
        self.public_url = f"http://127.0.0.1:{port}"
        with serve(store) as self.store_url, serve(self.providers.app) as providers_url:
            line = create_line(
                voice_settings(
                    backend_url=self.store_url,
                    agent_api_key=KEY,
                    voice_public_url=self.public_url,
                    deepgram_url=providers_url.replace("http", "ws"),
                    elevenlabs_url=providers_url,
                    anthropic_base_url=providers_url,
                    **{"silence_seconds": 30, **settings},
                )
            )
            with serve(line, port):
                yield

    def store(self, method, path, **kwargs):
        with httpx2.Client(base_url=self.store_url, headers={"X-API-Key": KEY}) as client:
            return client.request(method, path, **kwargs)

    def caller(self, **kwargs):
        return TwilioCaller(self.public_url, "twilio-token", **kwargs)

    async def summary(self):
        """The admin's message about the finished call."""
        async with asyncio.timeout(10):
            while not (found := [t for t in self.texts() if "VOICE CALL SUMMARY" in t]):
                await asyncio.sleep(0.02)
        return found[0]

    def texts(self):
        # Attached transcripts aren't JSON.
        sent = [r for r in self.telegram.requests if r.url.path.endswith("/sendMessage")]
        return [json.loads(r.content)["text"] for r in sent]

    def link_customer(self):
        body = {"phone": "+998901234567", "telegram_id": "123456789",
                "telegram_username": "dilnoza_k", "name": "Dilnoza Karimova"}
        assert self.store("PUT", "/api/customers", json=body).is_success


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


@pytest.mark.parametrize("stt", ["deepgram", "elevenlabs"])
def test_order_by_phone_with_an_interruption(world, stt):
    replies = [
        Reply(Tool("search_catalog", {"query": "earbuds"})),
        Reply("We have wireless earbuds for forty-nine dollars and they last all day long."),
        Reply("Two earbuds to Amir Temur Street. Shall I order them?"),
        Reply("Placing it now. ", Tool("create_order", ORDER)),
        Reply(Tool("generate_invoice", {"order_id": "1", "payment_provider": "click"})),
        Reply(
            Tool(
                "notify_admin_order",
                {"order_id": "1", "customer_summary": "Ring twice", "payment_link": "x"},
            )
        ),
        Reply("All done! The link is in your Telegram chat."),
    ]

    async def call():
        async with world.caller() as caller:
            await caller.hear("How can I help you today?")
            await caller.quiet()

            caller.say("Do you have earbuds?")
            await caller.hear("wireless earbuds for forty")
            caller.start_saying("two of them")
            caller.say("two of them to twelve Amir Temur Street")
            await caller.hear("Shall I order them?")
            # The rest of the interrupted sentence was never played.
            assert "all day long" not in caller.heard
            assert caller.clears == 1
            await caller.quiet()

            caller.say("yes please")
            await caller.hear("The link is in your Telegram chat.")
            await caller.quiet()
            await caller.hang_up()
            return await world.summary()

    with world.running(*replies, stt_provider=stt):
        world.link_customer()
        summary = asyncio.run(call())
        order = world.store("GET", "/api/orders/1").json()

    assert order["total_amount"] == pytest.approx(99.98)
    assert order["customer_telegram_id"] == "123456789"
    link, notice, report, last = world.texts()
    assert world.telegram.payloads[0]["chat_id"] == "123456789"
    assert order["invoices"][0]["payment_url"] in link
    assert "NEW ORDER CONFIRMED #1" in notice and "Ring twice" in notice
    assert report.startswith("📞 CALL ENDED") and "Order #1" in report
    assert "Caller: two of them to twelve Amir Temur Street" in report
    assert last == summary and summary.splitlines()[1:] == [
        "👤 Customer: @dilnoza_k",
        "📝 Summary:",
        "• Wanted earbuds.",
        "• Wireless Earbuds.",
        "📌 Outcome: Order Placed",  # the store knows better than the summary
    ]

    # Claude's history holds what was heard, cut where the caller cut in.
    assert world.claude.rejected == []
    history = world.claude.requests[2]["messages"]
    [said] = [b["text"] for b in history[-2]["content"] if b["type"] == "text"]
    assert said.startswith("We have wireless earbuds for") and "all day long" not in said
    assert f"Aida: {said} —" in report.replace("One moment. ", "").replace(
        "Let me check. ", "").replace("Just a second. ", "")


def test_transfer_to_a_person(world):
    escalate = Tool("escalate_to_human", {"reason": "Upset", "context_summary": "Late parcel"})

    async def call():
        async with world.caller() as caller:
            await caller.hear("How can I help you today?")
            await caller.quiet()
            caller.say("I want to talk to a person")
            await caller.hear("Connecting you right now")
            async with asyncio.timeout(5):
                while not caller.ended_by_server:
                    await asyncio.sleep(0.01)
            # With the stream closed, Twilio asks what to do next.
            status, xml = await caller.webhook("/voice/after")
            return status, xml, await world.summary()

    with world.running(Reply(escalate), admin_phone="+998900000000"):
        status, xml, summary = asyncio.run(call())
    assert status == 200 and "<Dial>+998900000000</Dial>" in xml
    alert, _ = world.texts()  # no order, so no order report
    assert "HUMAN SUPPORT NEEDED" in alert
    assert "💬 Last words: “I want to talk to a person”" in alert
    assert summary.endswith("📌 Outcome: Escalated")


def test_losing_speech_recognition_apologises_and_alerts(world):
    async def call():
        async with world.caller() as caller:
            async with asyncio.timeout(10):
                while not caller.ended_by_server:
                    await asyncio.sleep(0.01)
            _, xml = await caller.webhook("/voice/after")
            return xml, await world.summary()

    with world.running():
        world.providers.stt_error = {"type": "Error", "code": "X", "description": "no credit"}
        xml, summary = asyncio.run(call())
    assert "technical problem" in xml and "Please call again" in xml
    assert "HUMAN SUPPORT NEEDED" in world.texts()[0]
    assert summary.endswith("📌 Outcome: Escalated")
    assert "dg-key" not in json.dumps(world.telegram.payloads)


def test_dropped_line_is_reported_too(world):
    async def call():
        async with world.caller() as caller:
            await caller.hear("Hello!")
            await caller._ws.close()
            return await world.summary()

    with world.running():
        summary = asyncio.run(call())
    assert summary.endswith("📌 Outcome: Inquiry Only")
    assert world.texts() == [summary]


def test_two_calls_at_once_stay_apart(world):
    def answer(body):
        return body["messages"][-1]["content"][-1]["text"]

    class Echo(FakeClaude):
        async def __call__(self, request):
            body = json.loads(request.content)
            if body["max_tokens"]:
                self.replies = [Reply(f"You said {answer(body)}. Right?")]
            return await super().__call__(request)

    async def call(word, sid):
        async with world.caller(sid=sid) as caller:
            await caller.hear("How can I help you today?")
            await caller.quiet()
            caller.say(word)
            await caller.hear(f"You said {word}. Right?")
            return caller.heard

    async def both():
        return await asyncio.gather(call("apples", "CA1"), call("pears", "CA2"))

    with world.running():
        world.providers.claude = world.claude = Echo()
        first, second = asyncio.run(both())
    assert "pears" not in first and "apples" not in second


def test_our_own_share_of_the_latency_is_small(world):
    """With providers that answer at once, what's left is this server's overhead."""
    replies = [Reply("Sure thing. Anything else?") for _ in range(6)]

    async def call():
        async with world.caller() as caller:
            await caller.hear("How can I help you today?")
            for _ in replies:
                await caller.quiet(0.1)
                caller.say("tell me about mugs")
                before = len(caller.reply_ms)
                async with asyncio.timeout(5):
                    while len(caller.reply_ms) == before:
                        await asyncio.sleep(0.005)
            return caller.reply_ms

    with world.running(*replies):
        reply_ms = asyncio.run(call())
    typical = sorted(reply_ms)[len(reply_ms) // 2]
    print(f"\nserver overhead: {typical:.0f} ms typical, {max(reply_ms):.0f} ms worst")
    assert typical < 250  # includes up to 80 ms of audio batched for Deepgram
