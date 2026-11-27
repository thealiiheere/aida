"""Calls run by Gemini Live, against a stand-in for Google's Live API."""

import asyncio
import base64
import json
from urllib.parse import parse_qs

import pytest
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.testclient import TestClient

from agent.tools import StoreTools, live_definitions
from tests.fakes import voice_settings
from tests.simulation import serve
from voice.gemini_live import GeminiLiveSession, declarations, gemini_schema
from voice.server import create_app as create_line
from voice.session import Caller

PATH = "/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
HELLO_PCM = b"\x00\x10" * 2400  # 100 ms of 24 kHz audio


def spoken(text: str, pcm: bytes = HELLO_PCM) -> list[dict]:
    """What Gemini sends for one turn of Aida's speech."""
    data = base64.b64encode(pcm).decode()
    return [
        {"serverContent": {"outputTranscription": {"text": text}}},
        {"serverContent": {"modelTurn": {"parts": [{"inlineData": {"mimeType": "audio/pcm;rate=24000", "data": data}}]}}},
        {"serverContent": {"turnComplete": True}},
    ]  # fmt: skip


class Gemini:
    """The Live API's one WebSocket. Each connection plays the next scenario."""

    def __init__(self, *scenarios):
        self.scenarios = list(scenarios)
        self.keys: list[str] = []
        self.setups: list[dict] = []
        self.received: list[dict] = []  # everything but audio
        self.audio_bytes = 0
        self.app = FastAPI()
        self.app.websocket(PATH)(self._connect)

    async def _connect(self, ws: WebSocket):
        self.keys.append(parse_qs(ws.url.query).get("key", [""])[0])
        await ws.accept()
        self.setups.append(json.loads(await ws.receive_text())["setup"])
        await ws.send_json({"setupComplete": {}})
        scenario = self.scenarios.pop(0)
        try:
            await scenario(Peer(self, ws))
            while True:  # stay open until the call ends
                await self._next(ws)
        except WebSocketDisconnect:
            pass

    async def _next(self, ws: WebSocket, audio_until: int | None = None) -> dict | None:
        """The next message that isn't audio; or None once this much audio is in."""
        while audio_until is None or self.audio_bytes < audio_until:
            message = json.loads(await ws.receive_text())
            audio = message.get("realtimeInput", {}).get("audio")
            if audio:
                assert audio["mimeType"] == "audio/pcm;rate=16000"
                self.audio_bytes += len(base64.b64decode(audio["data"]))
                continue
            self.received.append(message)
            return message
        return None


class Peer:
    def __init__(self, gemini: Gemini, ws: WebSocket):
        self.gemini, self.ws = gemini, ws

    async def send(self, *messages: dict) -> None:
        for message in messages:
            await self.ws.send_json(message)

    async def receive(self) -> dict:
        return await self.gemini._next(self.ws)

    async def hear(self, seconds: float) -> None:
        """Wait until this much of the caller's audio has come in (16 kHz PCM16)."""
        need = self.gemini.audio_bytes + int(seconds * 32000)
        while await self.gemini._next(self.ws, audio_until=need):
            pass


async def greet(peer: Peer) -> None:
    hello = await peer.receive()
    assert hello["clientContent"]["turns"][0]["parts"][0]["text"] == "[The caller has just connected.]"
    await peer.send(*spoken("Assalomu alaykum!"))


class Phone:
    """The Twilio side: what the session sends, and the caller's audio going in."""

    def __init__(self):
        self.sent: list[dict] = []
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.talking = True

    async def send(self, message: dict) -> None:
        self.sent.append(message)
        if message["event"] == "mark":
            await self.incoming.put({"event": "mark", "mark": message["mark"]})

    async def messages(self):
        while True:
            try:
                message = await asyncio.wait_for(self.incoming.get(), 0.02)
            except TimeoutError:
                payload = base64.b64encode(b"\xff" * 160).decode()
                message = {"event": "media", "media": {"track": "inbound", "payload": payload}}
            if message is None:
                return
            yield message

    def events(self, name: str) -> list[dict]:
        return [m for m in self.sent if m["event"] == name]

    def audio(self) -> bytes:
        return b"".join(base64.b64decode(m["media"]["payload"]) for m in self.events("media"))


def settings(url: str, **overrides):
    values = {"voice_engine": "gemini_live", "gemini_api_key": "g-key",
              "gemini_live_url": url.replace("http", "ws"), "voice_language": "uz"}  # fmt: skip
    return voice_settings(**values | overrides)


async def run_call(gemini: Gemini, client, until, caller=None, **overrides):
    """One call; `until(phone, session)` returns when the caller should hang up."""
    with serve(gemini.app) as url:
        phone = Phone()
        caller = caller or Caller(phone="+998901234567", telegram_id="123456789")
        session = GeminiLiveSession(
            settings=settings(url, **overrides),
            tools=StoreTools(client=client, customer_telegram_id=caller.telegram_id,
                             customer_phone=caller.phone, live=True),  # fmt: skip
            caller=caller, call_sid="CA1", stream_sid="MZ1", send=phone.send,
        )  # fmt: skip
        task = asyncio.create_task(session.run(phone.messages()))
        async with asyncio.timeout(15):
            await until(phone, session, task)
            await phone.incoming.put(None)
            await task
        return phone, session


def call_test(test):
    def run(client, telegram):
        asyncio.run(test(client, telegram))

    run.__name__ = test.__name__
    return run


async def until_done(phone, session, task):
    while not task.done() and len(session.transcript) < 3:
        await asyncio.sleep(0.02)


@call_test
async def test_a_call_with_a_tool(client, telegram):
    async def scenario(peer):
        await greet(peer)
        await peer.hear(0.5)
        await peer.send(
            {"serverContent": {"inputTranscription": {"text": "Krujkalar "}}},
            {"serverContent": {"inputTranscription": {"text": "bormi?"}}},
            {"toolCall": {"functionCalls": [{"id": "c1", "name": "search_catalog", "args": {"query": "mug"}}]}},
        )  # fmt: skip
        answer = (await peer.receive())["toolResponse"]["functionResponses"][0]
        assert answer["id"] == "c1" and answer["response"]["ok"] is True
        await peer.send(*spoken("Ha, bor."))

    gemini = Gemini(scenario)
    phone, session = await run_call(gemini, client, until_done)
    assert session.transcript == [
        {"speaker": "aida", "text": "Assalomu alaykum!"},
        {"speaker": "caller", "text": "Krujkalar bormi?"},
        {"speaker": "aida", "text": "Ha, bor."},
    ]
    assert gemini.keys == ["g-key"]
    setup = gemini.setups[0]
    assert setup["model"] == "models/gemini-3.8-live"
    voice = setup["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]
    assert voice == {"voiceName": "Kore"}
    assert "Assalomu alaykum" in setup["systemInstruction"]["parts"][0]["text"]
    names = [d["name"] for d in setup["tools"][0]["functionDeclarations"]]
    assert "search_catalog" in names and "escalate_to_human" in names
    # Two turns of 100 ms at 24 kHz become 8 kHz phone frames.
    frames = phone.events("media")
    assert all(len(base64.b64decode(f["media"]["payload"])) <= 160 for f in frames)
    assert 2 * 800 - 160 <= len(phone.audio()) <= 2 * 800
    assert session.outcome == "completed" and session.problem is None
    assert session.ended()["raw_audio_transcript"].startswith("Aida: Assalomu alaykum!")


@call_test
async def test_cutting_in_clears_the_phone(client, telegram):
    async def scenario(peer):
        await greet(peer)
        await peer.send({"serverContent": {"interrupted": True}})
        await peer.send(*spoken("Labbay?"))

    async def until(phone, session, task):
        while len(session.transcript) < 2:
            await asyncio.sleep(0.02)

    phone, session = await run_call(Gemini(scenario), client, until)
    kinds = [m["event"] for m in phone.sent]
    assert "clear" in kinds and kinds.index("clear") < len(kinds) - 1  # audio after it


@call_test
async def test_a_hand_over_says_goodbye_and_hangs_up(client, telegram):
    async def scenario(peer):
        await greet(peer)
        await peer.send({"toolCall": {"functionCalls": [{"id": "e1", "name": "escalate_to_human",
            "args": {"reason": "Wants a manager", "context_summary": "Asked for a person"}}]}})  # fmt: skip
        answer = (await peer.receive())["toolResponse"]["functionResponses"][0]
        assert answer["response"]["next"].startswith("Say only this, then stop talking:")
        await peer.send(*spoken("Hozir ulayman. Xayr!"))

    async def until(phone, session, task):
        await task  # Aida ends the call herself

    phone, session = await run_call(Gemini(scenario), client, until)
    assert session.outcome == "escalated"
    assert phone.events("mark") and session.transcript[-1]["text"] == "Hozir ulayman. Xayr!"


@call_test
async def test_a_moved_session_carries_on(client, telegram):
    async def first(peer):
        await greet(peer)
        await peer.send({"sessionResumptionUpdate": {"newHandle": "h-1", "resumable": True}},
                        {"goAway": {"timeLeft": "1s"}})  # fmt: skip
        await peer.ws.close()

    async def second(peer):
        await peer.send(*spoken("Davom etamiz."))

    async def until(phone, session, task):
        while len(session.transcript) < 2:
            await asyncio.sleep(0.02)

    gemini = Gemini(first, second)
    phone, session = await run_call(gemini, client, until)
    assert gemini.setups[1]["sessionResumption"] == {"handle": "h-1"}
    assert [m for m in gemini.received if "clientContent" in m][1:] == []  # no second hello
    assert session.problem is None and session.transcript[-1]["text"] == "Davom etamiz."


@call_test
async def test_an_unreachable_gemini_fails_the_call_without_leaking_the_key(client, telegram):
    phone = Phone()
    session = GeminiLiveSession(
        settings=settings("ws://127.0.0.1:9"), tools=StoreTools(client=client, live=True),
        caller=Caller(), call_sid="CA2", stream_sid="MZ2", send=phone.send,
    )  # fmt: skip
    async with asyncio.timeout(15):
        await session.run(phone.messages())
    assert session.outcome == "failed" and session.problem
    assert "g-key" not in session.problem


def test_tool_schemas_are_reduced_to_what_gemini_takes():
    schema = {"type": "object", "additionalProperties": False, "properties": {
        "items": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                  "properties": {"n": {"type": "integer", "minimum": 1}}}}}}  # fmt: skip
    assert gemini_schema(schema) == {"type": "object", "properties": {"items": {
        "type": "array", "items": {"type": "object", "properties": {
            "n": {"type": "integer", "minimum": 1}}}}}}  # fmt: skip
    text = json.dumps(declarations(live_definitions()))
    assert "additionalProperties" not in text and "input_schema" not in text


def test_gemini_live_needs_only_its_key():
    ready = settings("ws://x", anthropic_api_key=None, elevenlabs_api_key=None,
                     deepgram_api_key=None, elevenlabs_voice_id=None)  # fmt: skip
    assert ready.missing_speech == []
    assert settings("ws://x", gemini_api_key=None).missing_speech == ["GEMINI_API_KEY"]
    with TestClient(create_line(ready)) as line:
        assert line.get("/health").json()["ready"] is True
    with TestClient(create_line(settings("ws://x", gemini_api_key=None))) as line:
        assert line.get("/health").json()["ready"] is False


@pytest.mark.parametrize("engine", ["pipeline"])
def test_the_pipeline_is_unchanged(engine):
    assert voice_settings().voice_engine == engine


def test_the_live_check(tmp_path):
    from check import check_gemini_live, check_voicelab, lines

    async def speaks(peer):
        assert (await peer.receive())["clientContent"]["turnComplete"] is True
        await peer.send(*spoken("Salom."))

    gemini = Gemini(speaks)
    with serve(gemini.app) as url:
        result = check_gemini_live(settings(url))
    assert result.ok is True and "gemini-3.8-live" in result.detail
    assert gemini.setups[0]["model"] == "models/gemini-3.8-live"
    with serve(Gemini().app) as url:  # no scenario: the stand-in breaks the connection
        failed = check_gemini_live(settings(url))
    assert failed.ok is False and "g-key" not in failed.detail
    assert check_gemini_live(settings("ws://x", gemini_api_key=None)).ok is False
    assert check_gemini_live(voice_settings()).ok is None
    assert check_voicelab(settings("ws://x", tts_provider="voicelab")).ok is None
    from check import Result

    works = {n: Result(n, True, "") for n in ("Gemini Live", "Store", "Telegram", "Gemini")}
    assert "Phone over Bluetooth: ready" in lines(works, settings("ws://x"))
