"""A phone call with everything real except the providers.

Local servers stand in for Deepgram, ElevenLabs and Claude, following their documented
protocols, and TwilioCaller plays Twilio and the caller. What the caller says travels as
text hidden in the audio bytes; what Aida says can be read back the same way.
"""

import asyncio
import base64
import contextlib
import json
import re
import socket
import threading
import time
from xml.etree import ElementTree

import httpx2
import uvicorn
from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.responses import StreamingResponse
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from tests.fakes import FakeClaude
from voice.server import signature

MS_PER_CHAR = 10
SILENCE = b"\xff" * 160
MARKER = re.compile(rb"<(partial|final)( [\d.]+)?>(.*?)</>", re.S)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def serve(app: FastAPI, port: int | None = None):
    """Run an app on a real port, in a thread."""
    port = port or free_port()
    server = uvicorn.Server(uvicorn.Config(app, port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("server did not start")
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(10)


# --- Providers ----------------------------------------------------------------


class Providers:
    """Deepgram, ElevenLabs and Claude on one local port."""

    def __init__(self, claude: FakeClaude | None = None):
        self.claude = claude or FakeClaude()
        self.stt_connections: list[tuple[dict, dict]] = []  # query, headers
        self.stt_chunks: list[int] = []
        self.stt_drop = 0  # connections to close straight after accepting
        self.stt_error: dict | None = None
        self.tts_requests: list[tuple[dict, dict]] = []  # body, query
        self.tts_failures = 0
        self.tts_framing = "lines"  # or "joined", "sse"
        self.tts_times = "request"  # or "chunk", None
        self.app = FastAPI()
        self.app.websocket("/v2/listen")(self._flux)
        self.app.websocket("/v1/speech-to-text/realtime")(self._scribe)
        self.app.post("/v1/text-to-speech/{voice}/stream/with-timestamps")(self._tts)
        self.app.post("/v1/messages")(self._messages)

    async def _listen(self, ws: WebSocket, audio_of, hello: dict, partial, final):
        self.stt_connections.append((dict(ws.query_params), dict(ws.headers)))
        await ws.accept()
        if self.stt_drop:
            self.stt_drop -= 1
            await ws.close(1011)
            return
        await ws.send_json(hello)
        if self.stt_error:
            await ws.send_json(self.stt_error)
        buffer = b""
        with contextlib.suppress(WebSocketDisconnect):
            while True:
                audio = await audio_of(ws)
                self.stt_chunks.append(len(audio))
                buffer += audio
                while match := MARKER.search(buffer):
                    buffer = buffer[match.end() :]
                    kind, score, text = match.groups()
                    messages = (
                        partial(text.decode())
                        if kind == b"partial"
                        else final(text.decode(), float(score or 0.95))
                    )
                    for message in messages:
                        await ws.send_json(message)
                if b"<" not in buffer:
                    buffer = b""

    async def _flux(self, ws: WebSocket):
        def turn(event, text, score=0.9):
            words = [{"word": w, "confidence": score} for w in text.split()]
            return {
                "type": "TurnInfo", "event": event, "turn_index": 0,
                "transcript": text, "words": words, "end_of_turn_confidence": 0.9,
            }

        async def audio(ws):
            return await ws.receive_bytes()

        await self._listen(
            ws, audio, {"type": "Connected", "sequence_id": 0},
            lambda text: [turn("StartOfTurn", text), turn("Update", text)],
            lambda text, score: [turn("EndOfTurn", text, score), turn("Update", "")],
        )

    async def _scribe(self, ws: WebSocket):
        import math

        def committed(text, score):
            words = [
                {"text": w, "type": "word", "start": 0, "end": 1, "logprob": math.log(score)}
                for w in text.split()
            ]
            return [
                {"message_type": "committed_transcript", "text": text},
                {
                    "message_type": "committed_transcript_with_timestamps",
                    "text": text,
                    "words": [*words, {"text": " ", "type": "spacing", "logprob": -99}],
                },
            ]

        async def audio(ws):
            message = await ws.receive_json()
            assert message["message_type"] == "input_audio_chunk"
            assert message["sample_rate"] == 8000 and message["commit"] is False
            return base64.b64decode(message["audio_base_64"])

        await self._listen(
            ws, audio, {"message_type": "session_started", "session_id": "s", "config": {}},
            lambda text: [{"message_type": "partial_transcript", "text": text}],
            committed,
        )

    async def _tts(self, request: Request):
        self.tts_requests.append((await request.json(), dict(request.query_params)))
        if self.tts_failures:
            self.tts_failures -= 1
            return Response('{"detail": "busy"}', 503)
        text = self.tts_requests[-1][0]["text"]

        async def body():
            for start in range(0, len(text), 12):
                piece = text[start : start + 12]
                item = {
                    "audio_base64": base64.b64encode(voice_audio(piece)).decode(),
                    "alignment": None,
                }
                if self.tts_times:
                    base = start if self.tts_times == "request" else 0
                    at = [(base + i) * MS_PER_CHAR / 1000 for i in range(len(piece) + 1)]
                    item["alignment"] = {
                        "characters": list(piece),
                        "character_start_times_seconds": at[:-1],
                        "character_end_times_seconds": at[1:],
                    }
                text_item = json.dumps(item)
                frames = {
                    "lines": f"{text_item}\n",
                    "joined": text_item,
                    "sse": f"data: {text_item}\n\n",
                }
                frame = frames[self.tts_framing].encode()
                yield frame[:50]  # split mid-object, as a network would
                yield frame[50:]

        return StreamingResponse(body(), media_type="application/json")

    async def _messages(self, request: Request):
        reply = await self.claude(
            httpx2.Request("POST", str(request.url), headers=dict(request.headers),
                           content=await request.body())
        )
        if reply.headers["content-type"].startswith("text/event-stream"):
            return StreamingResponse(reply.aiter_bytes(), media_type="text/event-stream")
        return Response(await reply.aread(), reply.status_code, media_type="application/json")


def voice_audio(text: str) -> bytes:
    codes = (ord(c) if c.isascii() else ord("?") for c in text)
    return b"".join(bytes([code]) * MS_PER_CHAR * 8 for code in codes)


# --- Twilio and the caller ----------------------------------------------------


class TwilioCaller:
    """Rings the server, streams the caller's audio and plays Aida's in real time."""

    def __init__(self, public_url: str, auth_token: str, phone="+998901234567", sid="CA777"):
        self.public_url, self.auth_token = public_url, auth_token
        self.phone, self.sid = phone, sid
        self.heard = ""
        self.clears = 0
        self.ended_by_server = False
        self.reply_ms: list[float] = []  # from the end of what we said to the first audio
        self._said_at: float | None = None
        self._to_say: list[bytes] = []
        self._playing: list[tuple[str, object]] = []
        self._last_audio = time.monotonic()

    async def webhook(self, path: str, **extra) -> tuple[int, str]:
        form = {"CallSid": self.sid, "From": self.phone, "To": "+15550001111", **extra}
        url = self.public_url + path
        headers = {"X-Twilio-Signature": signature(self.auth_token, url, list(form.items()))}
        async with httpx2.AsyncClient() as client:
            response = await client.post(url, data=form, headers=headers)
        return response.status_code, response.text

    async def __aenter__(self):
        status, xml = await self.webhook("/voice/incoming")
        assert status == 200, xml
        stream = ElementTree.fromstring(xml).find("Connect/Stream")
        assert stream is not None, xml
        params = {p.get("name"): p.get("value") for p in stream.findall("Parameter")}
        self._ws = await connect(stream.get("url"))
        await self._send({"event": "connected", "protocol": "Call", "version": "1.0.0"})
        await self._send(
            {
                "event": "start",
                "streamSid": "MZ777",
                "start": {
                    "streamSid": "MZ777",
                    "callSid": self.sid,
                    "accountSid": "AC1",
                    "tracks": ["inbound"],
                    "customParameters": params,
                    "mediaFormat": {
                        "encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1
                    },
                },
            }
        )
        self._tasks = [asyncio.create_task(t()) for t in (self._talk, self._listen, self._play)]
        return self

    async def __aexit__(self, *exc):
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._ws.close()

    async def _send(self, message: dict) -> None:
        with contextlib.suppress(ConnectionClosed):
            await self._ws.send(json.dumps(message))

    async def _talk(self) -> None:
        """A 20 ms frame every 20 ms, like a phone."""
        next_at = time.monotonic()
        while True:
            frame = self._to_say.pop(0) if self._to_say else SILENCE
            if frame.endswith(b"</>") and frame.startswith(b"<final"):
                self._said_at = time.monotonic()
            payload = base64.b64encode(frame).decode()
            await self._send(
                {
                    "event": "media",
                    "streamSid": "MZ777",
                    "media": {"track": "inbound", "chunk": "1", "timestamp": "0",
                              "payload": payload},
                }
            )
            next_at += 0.02
            await asyncio.sleep(max(0, next_at - time.monotonic()))

    async def _listen(self) -> None:
        with contextlib.suppress(ConnectionClosed):
            async for raw in self._ws:
                message = json.loads(raw)
                assert message["streamSid"] == "MZ777"
                match message["event"]:
                    case "media":
                        if self._said_at is not None:
                            self.reply_ms.append((time.monotonic() - self._said_at) * 1000)
                            self._said_at = None
                        audio = base64.b64decode(message["media"]["payload"])
                        self._playing.append(("audio", audio))
                    case "mark":
                        self._playing.append(("mark", message["mark"]))
                    case "clear":
                        self.clears += 1
                        dropped, self._playing = self._playing, []
                        for kind, value in dropped:
                            if kind == "mark":
                                await self._send(
                                    {"event": "mark", "streamSid": "MZ777", "mark": value}
                                )
        self.ended_by_server = True

    async def _play(self) -> None:
        """Play Aida's audio at the speed a phone would."""
        left = b""
        next_at = time.monotonic()
        while True:
            while self._playing and self._playing[0][0] == "mark" and not left:
                mark = self._playing.pop(0)[1]
                await self._send({"event": "mark", "streamSid": "MZ777", "mark": mark})
            if self._playing and self._playing[0][0] == "audio":
                audio = self._playing[0][1]
                tick, rest = audio[:80], audio[80:]  # 10 ms, one character
                self.heard += chr(tick[0])
                self._last_audio = time.monotonic()
                if rest:
                    self._playing[0] = ("audio", rest)
                else:
                    self._playing.pop(0)
            now = time.monotonic()
            next_at = max(next_at + 0.01, now - 0.05)
            await asyncio.sleep(max(0, next_at - now))

    # What a test does

    def start_saying(self, text: str) -> None:
        self._to_say.append(f"<partial>{text}</>".encode())

    def say(self, text: str, confidence: float = 0.95) -> None:
        self._to_say.append(f"<final {confidence}>{text}</>".encode())

    async def hear(self, text: str, timeout: float = 10) -> None:
        """Wait until Aida has said this."""
        async with asyncio.timeout(timeout):
            while text.replace(" ", "") not in self.heard.replace(" ", ""):
                assert not self._listen_failed(), self.heard
                await asyncio.sleep(0.005)

    async def quiet(self, seconds: float = 0.25) -> None:
        """Wait until nothing has played for a while."""
        while self._playing or time.monotonic() - self._last_audio < seconds:
            await asyncio.sleep(0.01)

    async def hang_up(self) -> None:
        await self._send({"event": "stop", "streamSid": "MZ777", "stop": {"callSid": self.sid}})

    def _listen_failed(self) -> bool:
        return any(task.done() and task.exception() for task in self._tasks)
