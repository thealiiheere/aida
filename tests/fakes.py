"""A fake Claude that enforces the API's rules, and a call with no phone attached."""

import asyncio
import base64
import hashlib
import json
from dataclasses import dataclass

import httpx2

from agent.llm import Claude
from agent.openai_llm import OpenAiLlm
from agent.tools import StoreTools
from voice.config import VoiceSettings
from voice.session import Caller, CallSession
from voice.stt import Speech, Utterance
from voice.tts import AudioChunk

# --- Claude -------------------------------------------------------------------


@dataclass
class Tool:
    name: str
    input: dict


class Fallback:
    """Where a second model takes over the reply."""


class Reply:
    """Parts are text, Tool, Fallback, or an asyncio.Event to wait for mid-stream."""

    def __init__(self, *parts, stop=None, thinking=True):
        self.parts = parts
        self.stop = stop or (
            "tool_use" if any(isinstance(p, Tool) for p in parts) else "end_turn"
        )
        self.thinking = thinking


def prefix_signature(body: dict, count: int) -> str:
    """Claude ties a thinking block to everything before it. So does this fake."""
    prefix = [body["system"], body["tools"], body["messages"][:count]]
    return hashlib.sha256(json.dumps(prefix, sort_keys=True).encode()).hexdigest()


def request_problem(body: dict) -> str | None:
    """Why the real API would reject this request."""
    messages = body["messages"]
    if [m["role"] for m in messages] != [("user", "assistant")[i % 2] for i in range(len(messages))]:
        return "roles must alternate, starting with user"
    if messages[-1]["role"] != "user":
        return "assistant prefill is not supported"
    waiting: set[str] = set()
    for index, message in enumerate(messages):
        blocks = message["content"]
        if not blocks:
            return f"messages.{index}: empty content"
        results = {b["tool_use_id"] for b in blocks if b["type"] == "tool_result"}
        if results != waiting:
            return f"messages.{index}: tool_use ids {waiting} need results, got {results}"
        if results and blocks[0]["type"] != "tool_result":
            return f"messages.{index}: tool results must come first"
        waiting = {b["id"] for b in blocks if b["type"] == "tool_use"}
        for block in blocks:
            if block["type"] == "text" and not block["text"].strip():
                return f"messages.{index}: text must not be blank"
            if block["type"] == "thinking":
                if block["signature"] != prefix_signature(body, index):
                    return f"messages.{index}: thinking block is bound to a different conversation"
    return None


class FakeClaude:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests: list[dict] = []
        self.headers: list[httpx2.Headers] = []
        self.warmups: list[dict] = []
        self.rejected: list[str] = []

    async def __call__(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        if body["max_tokens"] == 0:
            self.warmups.append(body)
            return httpx2.Response(200, json=_message([], "max_tokens"))
        if problem := request_problem(body):
            self.rejected.append(problem)
            return _error(400, "invalid_request_error", problem)
        self.requests.append(body)
        self.headers.append(request.headers)
        reply = self.replies.pop(0) if self.replies else Reply("Okay.")
        if isinstance(reply, int):
            return _error(reply, "api_error", "scripted failure")
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_encoded(self.stream(body, reply)),
        )

    async def stream(self, body: dict, reply: Reply):
        yield _sse({"type": "message_start", "message": _message([], None)})
        index = 0

        def block(start: dict, *deltas: dict) -> str:
            nonlocal index
            events = [{"type": "content_block_start", "index": index, "content_block": start}]
            events += [{"type": "content_block_delta", "index": index, "delta": d} for d in deltas]
            events += [{"type": "content_block_stop", "index": index}]
            index += 1
            return "".join(_sse(e) for e in events)

        if reply.thinking:
            signature = prefix_signature(body, len(body["messages"]))
            yield block(
                {"type": "thinking", "thinking": "", "signature": ""},
                {"type": "signature_delta", "signature": signature},
            )
        text_open = False
        for number, part in enumerate(reply.parts):
            if isinstance(part, asyncio.Event):
                await part.wait()
            elif isinstance(part, str):
                if not text_open:
                    start = {"type": "text", "text": ""}
                    yield _sse(
                        {"type": "content_block_start", "index": index, "content_block": start}
                    )
                    text_open = True
                for i in range(0, len(part), 5):
                    delta = {"type": "text_delta", "text": part[i : i + 5]}
                    yield _sse({"type": "content_block_delta", "index": index, "delta": delta})
                    await asyncio.sleep(0)
            else:
                if text_open:
                    yield _sse({"type": "content_block_stop", "index": index})
                    index += 1
                    text_open = False
                if isinstance(part, Fallback):
                    models = {"from": {"model": "a"}, "to": {"model": "b"}}
                    yield block({"type": "fallback", **models})
                else:
                    tool_id = f"toolu_{len(self.requests)}_{number}"
                    yield block(
                        {"type": "tool_use", "id": tool_id, "name": part.name, "input": {}},
                        {"type": "input_json_delta", "partial_json": json.dumps(part.input)},
                    )
        if text_open:
            yield _sse({"type": "content_block_stop", "index": index})
        delta = {"stop_reason": reply.stop, "stop_sequence": None}
        usage = {"output_tokens": 1}
        yield _sse({"type": "message_delta", "delta": delta, "usage": usage})
        yield _sse({"type": "message_stop"})


async def _encoded(events):
    async for event in events:
        yield event.encode()


def _message(content: list, stop_reason: str | None) -> dict:
    return {
        "id": "msg_fake",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def _sse(event: dict) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"


def _error(status: int, kind: str, message: str) -> httpx2.Response:
    return httpx2.Response(
        status, json={"type": "error", "error": {"type": kind, "message": message}}
    )


# --- OpenAI -------------------------------------------------------------------


def openai_problem(body: dict) -> str | None:
    """What OpenAI's Chat Completions API would reject this request for."""
    messages = body.get("messages") or []
    if not messages or messages[0]["role"] != "system":
        return "the first message should be the system prompt"
    waiting: list[str] = []
    for message in messages[1:]:
        role = message["role"]
        if waiting and role != "tool":
            return f"tool calls {waiting} have no tool message after them"
        if role == "tool":
            if message["tool_call_id"] not in waiting:
                return f"a tool message answers no call: {message['tool_call_id']}"
            waiting.remove(message["tool_call_id"])
        elif role == "assistant":
            calls = message.get("tool_calls") or []
            if not calls and not message.get("content"):
                return "an assistant message with neither content nor tool calls"
            for call in calls:
                json.loads(call["function"]["arguments"])
            waiting = [call["id"] for call in calls]
        elif role != "user" or not message.get("content"):
            return f"a {role} message without content"
    if waiting:
        return f"tool calls {waiting} were never answered"
    return None


class FakeOpenAI:
    """OpenAI's streaming Chat Completions, scripted with the same Reply objects."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests: list[dict] = []
        self.headers: list[httpx2.Headers] = []
        self.rejected: list[str] = []
        self._calls = 0

    async def __call__(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        if problem := openai_problem(body):
            self.rejected.append(problem)
            return httpx2.Response(400, json={"error": {"type": "invalid_request_error",
                                                        "message": problem}})  # fmt: skip
        self.requests.append(body)
        self.headers.append(request.headers)
        reply = self.replies.pop(0) if self.replies else Reply("Okay.")
        if isinstance(reply, int):
            return httpx2.Response(reply, json={"error": {"type": "server_error",
                                                          "message": "scripted failure"}})
        chunks = []

        def chunk(delta: dict, finish: str | None = None) -> None:
            choice = {"index": 0, "delta": delta, "finish_reason": finish}
            chunks.append(f"data: {json.dumps({'id': 'c1', 'choices': [choice]})}\n\n")

        chunk({"role": "assistant", "content": ""})
        index = 0
        for part in reply.parts:
            if isinstance(part, str):
                for start in range(0, len(part), 7):
                    chunk({"content": part[start : start + 7]})
            elif isinstance(part, Tool):
                self._calls += 1
                arguments = json.dumps(part.input)
                half = len(arguments) // 2
                first = {"name": part.name, "arguments": arguments[:half]}
                rest = {"arguments": arguments[half:]}
                chunk({"tool_calls": [{"index": index, "id": f"call_{self._calls}",
                                       "type": "function", "function": first}]})  # fmt: skip
                chunk({"tool_calls": [{"index": index, "function": rest}]})
                index += 1
        finish = {"tool_use": "tool_calls", "end_turn": "stop", "max_tokens": "length",
                  "refusal": "content_filter"}[reply.stop]  # fmt: skip
        chunk({}, finish)
        chunks.append("data: [DONE]\n\n")
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, content="".join(chunks).encode()
        )


# --- A call -------------------------------------------------------------------


def voice_settings(**overrides) -> VoiceSettings:
    values = {
        "store_name": "Test Store",
        "anthropic_api_key": "test-key",
        "elevenlabs_api_key": "el-key",
        "elevenlabs_voice_id": "voice",
        "deepgram_api_key": "dg-key",
        "twilio_auth_token": "twilio-token",
        "voice_public_url": "https://voice.example",
        "silence_seconds": 0.2,
        **overrides,
    }
    return VoiceSettings(_env_file=None, **values)


MS_PER_CHAR = 10


class FakeTts:
    """Ten milliseconds per character, sent in two chunks."""

    def __init__(self):
        self.spoken: list[str] = []
        self.languages: list[str | None] = []
        self.gate: asyncio.Event | None = None
        self.fail: Exception | None = None

    async def speak(self, text, previous_text="", language=None):
        if self.fail:
            raise self.fail
        self.spoken.append(text)
        self.languages.append(language)
        ends = [(i + 1) * MS_PER_CHAR for i in range(len(text))]
        half = len(text) // 2
        for part in (slice(0, half), slice(half, None)):
            piece = text[part]
            audio = b"".join(
                bytes([ord(c) if c.isascii() else 63]) * MS_PER_CHAR * 8 for c in piece
            )
            yield AudioChunk(audio, ends[part])
            if self.gate:
                await self.gate.wait()


class FakeStt:
    def __init__(self):
        self.audio = b""
        self.on_event = None
        self.fail: asyncio.Future | None = None
        self.languages: list[str] = []

    def set_language(self, language: str) -> None:
        self.languages.append(language)

    def feed(self, audio: bytes) -> None:
        self.audio += audio

    async def run(self, on_event) -> None:
        self.on_event = on_event
        self.fail = asyncio.get_running_loop().create_future()
        await self.fail


class Call:
    """Runs a CallSession. Time only moves when the test plays audio or waits."""

    def __init__(
        self, client, *replies, caller: Caller | None = None, tools=None, api_key=None, **settings
    ):
        self.settings = voice_settings(**settings)
        self.claude = FakeClaude(*replies)
        self.openai = FakeOpenAI(*replies)
        self.tts = FakeTts()
        self.stt = FakeStt()
        self.now = 1000.0
        self.sent: list[dict] = []
        self.incoming: asyncio.Queue[dict | None] = asyncio.Queue()
        self.hung_up = False
        self._buffer: list[tuple[str, object]] = []  # what Twilio has yet to play
        self._played_ms = 0.0
        self.heard = ""
        caller = caller or Caller("+998901234567", "123456789", "dilnoza_k", "Dilnoza Karimova")
        self.session = CallSession(
            settings=self.settings,
            stt=self.stt,
            tts=self.tts,
            llm=(
                OpenAiLlm(
                    self.settings,
                    httpx2.AsyncClient(transport=httpx2.MockTransport(self.openai)),
                )
                if self.settings.llm_provider == "openai"
                else Claude(
                    self.settings,
                    httpx2.AsyncClient(transport=httpx2.MockTransport(self.claude)),
                )
            ),
            tools=tools
            or StoreTools(
                client=client,
                api_key=api_key,
                customer_telegram_id=caller.telegram_id,
                customer_telegram_username=caller.telegram_username,
                customer_phone=caller.phone,
                live=True,
            ),
            caller=caller,
            call_sid="CA123",
            stream_sid="MZ123",
            send=self._on_sent,
            clock=lambda: self.now,
        )

    async def __aenter__(self):
        self.task = asyncio.create_task(self.session.run(self._incoming()))
        await self.until(lambda: self.stt.on_event and self.session.agent_speaking)
        return self

    async def __aexit__(self, *exc):
        if not self.task.done():
            await self.hang_up()
        if exc[0] is None:
            assert self.claude.rejected == []

    async def _incoming(self):
        while (message := await self.incoming.get()) is not None:
            yield message

    async def _on_sent(self, message: dict) -> None:
        assert message["streamSid"] == "MZ123"
        self.sent.append(message)
        match message["event"]:
            case "media":
                self._buffer.append(("audio", base64.b64decode(message["media"]["payload"])))
            case "mark":
                self._buffer.append(("mark", message["mark"]["name"]))
            case "clear":
                # As Twilio does: drop the audio, return the marks that were waiting.
                for kind, value in self._buffer:
                    if kind == "mark":
                        self.incoming.put_nowait({"event": "mark", "mark": {"name": value}})
                self._buffer.clear()

    # What the caller does

    async def says(self, text: str, confidence: float = 0.95) -> None:
        self.stt.on_event(Utterance(text, confidence))
        await self.idle()

    async def starts_saying(self, text: str) -> None:
        self.stt.on_event(Speech(text))
        await self.idle()

    async def hang_up(self) -> None:
        self.incoming.put_nowait({"event": "stop", "stop": {}})
        self.incoming.put_nowait(None)
        await asyncio.wait_for(self.task, 5)

    # Time

    async def play(self, ms: float | None = None) -> str:
        """Let the caller hear some audio, or all of it. Returns what they heard."""
        heard = b""
        while self._buffer and (ms is None or ms > 0):
            kind, value = self._buffer[0]
            if kind == "mark":
                self._buffer.pop(0)
                self.incoming.put_nowait({"event": "mark", "mark": {"name": value}})
                await self.idle()
                continue
            size = len(value) if ms is None else min(len(value), int(ms * 8))
            heard += value[:size]
            self.now += size / 8000
            if ms is not None:
                ms -= size / 8
            if size == len(value):
                self._buffer.pop(0)
            else:
                self._buffer[0] = ("audio", value[size:])
        # Marks right behind the last sample are due too.
        while ms is None and self._buffer and self._buffer[0][0] == "mark":
            self.incoming.put_nowait({"event": "mark", "mark": {"name": self._buffer.pop(0)[1]}})
        await self.idle()
        return heard[:: MS_PER_CHAR * 8].decode()

    async def hear_everything(self) -> str:
        """Play until Aida has nothing left to say."""
        heard = ""
        for _ in range(200):
            heard += await self.play()
            if self.session._turn is None or self.task.done():
                return heard
            await asyncio.sleep(0.005)
        raise AssertionError("Aida never finished")

    async def idle(self) -> None:
        for _ in range(30):
            await asyncio.sleep(0)
        await asyncio.sleep(0.01)

    async def until(self, condition) -> None:
        async with asyncio.timeout(5):
            while not condition():
                await asyncio.sleep(0.005)

    # What to look at

    @property
    def messages(self) -> list[dict]:
        return self.session.conversation.messages

    @property
    def clears(self) -> int:
        return sum(m["event"] == "clear" for m in self.sent)

    def said(self, index: int = -1) -> str:
        """The text of one assistant message in the history."""
        replies = [m for m in self.messages if m["role"] == "assistant"]
        return " ".join(b["text"] for b in replies[index]["content"] if b["type"] == "text")
