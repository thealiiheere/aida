"""A phone call handled by Gemini Live: one model that hears, thinks and speaks.

It takes the place of speech-to-text, the AI and text-to-speech together. The caller's
audio streams to Gemini, Gemini's voice streams back, and Gemini decides for itself
when the caller has finished or cut in. This side turns the phone's 8 kHz mu-law into
what Gemini takes and back, runs the store tools Gemini asks for, keeps a transcript
and ends the call after a hand-over, so it fits wherever CallSession does.
"""

import asyncio
import base64
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from agent.prompts import voice_prompt
from agent.tools import StoreTools, live_definitions, no_manager, run_tool

from .audio import PhoneDownsampler, phone_to_pcm16k
from .config import VoiceSettings
from .session import LINES, Caller, CallSession, _call_context
from .stt import make_stt

log = logging.getLogger("aida.voice.gemini")

FRAME_BYTES = 160  # 20 ms of 8 kHz mu-law, as Twilio sends and expects
SEND_FRAMES = 5  # caller audio goes to Gemini 100 ms at a time
GOODBYE_SECONDS = 15  # longest wait for the last line to be heard before hanging up
RECONNECTS = 2
# Gemini's own schema is a subset of JSON Schema; these are the parts it accepts.
SCHEMA_KEYS = {"type", "description", "enum", "properties", "required", "items", "minimum",
               "maximum", "minItems", "maxItems", "nullable"}  # fmt: skip


def gemini_schema(schema: dict) -> dict:
    """A tool's JSON Schema, reduced to what Gemini's function declarations take."""
    out = {}
    for key, value in schema.items():
        if key not in SCHEMA_KEYS:
            continue
        if key == "properties":
            value = {name: gemini_schema(prop) for name, prop in value.items()}
        elif key == "items":
            value = gemini_schema(value)
        out[key] = value
    return out


def declarations(tools: list[dict]) -> list[dict]:
    return [
        {"name": t["name"], "description": t["description"],
         "parameters": gemini_schema(t["input_schema"])}
        for t in tools
    ]  # fmt: skip


def make_session(settings: VoiceSettings, *, llm, tts, **call) -> "CallSession | GeminiLiveSession":
    """The session VOICE_ENGINE asks for, for one call."""
    if settings.voice_engine == "gemini_live":
        return GeminiLiveSession(settings=settings, **call)
    return CallSession(settings=settings, stt=make_stt(settings), tts=tts, llm=llm, **call)


def uses_pipeline(settings: VoiceSettings) -> bool:
    """Whether calls need the separate AI and text to speech."""
    return settings.voice_engine != "gemini_live"


class GeminiLiveSession:
    """One call, the way CallSession has one: run(), then report() and ended()."""

    def __init__(
        self,
        *,
        settings: VoiceSettings,
        tools: StoreTools,
        caller: Caller,
        call_sid: str,
        stream_sid: str,
        send: Callable[[dict], Awaitable[None]],
        clock: Callable[[], float] = time.monotonic,
    ):
        self._settings = settings
        self._tools = tools
        self._caller = caller
        self._send = send
        self._clock = clock
        self.call_sid = call_sid
        self.stream_sid = stream_sid
        self.language = settings.voice_language

        self.outcome = "completed"
        self.problem: str | None = None
        self.order_ids: list[int] = []
        self.transcript: list[dict] = []
        self.duration_seconds = 0

        key = settings.gemini_api_key.get_secret_value() if settings.gemini_api_key else ""
        base = settings.gemini_live_url.rstrip("/")
        self._url = (
            f"{base}/ws/google.ai.generativelanguage.v1beta.GenerativeService."
            f"BidiGenerateContent?key={key}"
        )
        self._started = clock()
        self._ended = asyncio.Event()
        self._closing = False  # a hand-over was made: hang up after Aida's next turn
        self._heard: list[str] = []  # the caller's words, as Gemini writes them down
        self._said: list[str] = []  # Aida's
        self._phone = PhoneDownsampler(24000)
        self._marks = 0
        self._played = asyncio.Event()
        self._resume: str | None = None  # Gemini's handle for carrying on after a reconnect
        self._ws: ClientConnection | None = None
        self._ready = asyncio.Event()

    # --- The call ---------------------------------------------------------------

    async def run(self, incoming: AsyncIterator[dict]) -> None:
        jobs = [
            asyncio.create_task(self._read(incoming)),
            asyncio.create_task(self._talk()),
            asyncio.create_task(self._ended.wait()),
        ]
        try:
            finished, _ = await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
            for job in finished:
                if exc := job.exception():
                    log.error("call %s failed", self.call_sid, exc_info=exc)
                    self._give_up(f"{type(exc).__name__}: {exc}")
        finally:
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            self._flush()
            if self._ws:
                with suppress(WebSocketException, OSError):
                    await self._ws.close()
            self.duration_seconds = int(self._clock() - self._started)
            if self.problem and self.outcome != "escalated":
                self.outcome = "failed"

    def report(self) -> dict:
        """The call, as POST /api/admin/notify-order expects it."""
        return {
            "call_id": self.call_sid,
            "duration_seconds": self.duration_seconds,
            "outcome": self.outcome,
            "order_ids": self.order_ids[-20:],
            "customer_phone": self._caller.phone,
            "customer_telegram_id": self._caller.telegram_id,
            "customer_telegram_username": self._caller.telegram_username,
            "customer_name": self._caller.name,
            "transcript": self.transcript[-2000:],
        }

    def ended(self) -> dict:
        """The call, as POST /api/telephony/call-ended expects it."""
        speakers = {"caller": "Caller", "aida": "Aida"}
        return {
            "call_id": self.call_sid,
            "caller_telegram_id": self._caller.telegram_id,
            "caller_phone": self._caller.phone,
            "raw_audio_transcript": "\n".join(
                f"{speakers[line['speaker']]}: {line['text']}" for line in self.transcript
            )[-200_000:],
            "call_duration": self.duration_seconds,
            "order_ids": self.order_ids[-20:],
            "escalated": self.outcome in ("escalated", "failed"),
        }

    async def alert_problem(self) -> None:
        """Tag the admin after a call that broke down."""
        lines = [f"{line['speaker']}: {line['text']}" for line in self.transcript[-12:]]
        await run_tool(
            self._tools,
            "escalate_to_human",
            {
                "reason": f"The call failed. {self.problem}"[:300],
                "context_summary": " / ".join(lines)[-3000:] or "Nothing had been said yet.",
            },
        )

    def _give_up(self, problem: str) -> None:
        if self.problem is None:
            log.error("call %s cannot continue: %s", self.call_sid, problem)
            self.problem = problem
        self._ended.set()

    # --- The phone --------------------------------------------------------------

    async def _read(self, incoming: AsyncIterator[dict]) -> None:
        """The caller's audio, passed on to Gemini 100 ms at a time."""
        pending = b""
        async for message in incoming:
            try:
                match message.get("event"):
                    case "media":
                        if message["media"].get("track", "inbound") != "inbound":
                            continue
                        pending += base64.b64decode(message["media"]["payload"])
                        if len(pending) >= FRAME_BYTES * SEND_FRAMES:
                            await self._hear(pending)
                            pending = b""
                    case "mark":
                        self._played.set()
                    case "stop":
                        return
            except (KeyError, TypeError, AttributeError, ValueError):
                log.warning("ignored a malformed %s message", message.get("event"))

    async def _hear(self, audio: bytes) -> None:
        if not self._ready.is_set():
            return  # nobody to give it to while (re)connecting; old audio is worthless
        pcm = base64.b64encode(phone_to_pcm16k(audio)).decode()
        message = {"realtimeInput": {"audio": {"data": pcm, "mimeType": "audio/pcm;rate=16000"}}}
        with suppress(ConnectionClosed):
            await self._ws.send(json.dumps(message))

    async def _play(self, pcm: bytes) -> None:
        audio = self._phone.feed(pcm)
        for start in range(0, len(audio), FRAME_BYTES):
            payload = base64.b64encode(audio[start : start + FRAME_BYTES]).decode()
            await self._send(
                {"event": "media", "streamSid": self.stream_sid, "media": {"payload": payload}}
            )

    # --- Gemini -----------------------------------------------------------------

    def _setup(self) -> dict:
        settings = self._settings
        greeting = LINES[settings.voice_language].greeting.format(store=settings.store_name)
        system = "\n\n".join(
            [
                voice_prompt(settings.store_name, settings.admin_name, settings.voice_language),
                _call_context(self._caller),
                "# Starting the call\n\nWhen told the caller has connected, greet them first, "
                f"in these words or close to them: {greeting}",
            ]
        )
        setup = {
            "model": f"models/{settings.gemini_live_model}",
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": settings.gemini_voice}}
                },
            },
            "systemInstruction": {"parts": [{"text": system}]},
            "tools": [{"functionDeclarations": declarations(live_definitions())}],
            "inputAudioTranscription": {},
            "outputAudioTranscription": {},
            # Without this an audio session ends after 15 minutes.
            "contextWindowCompression": {"slidingWindow": {}},
            "sessionResumption": {"handle": self._resume} if self._resume else {},
        }
        return {"setup": setup}

    async def _talk(self) -> None:
        """Gemini's side of the call, reconnecting when Google moves the session."""
        failures = 0
        first = True
        while True:
            opened = self._clock()
            try:
                async with connect(
                    self._url, compression=None, open_timeout=10, close_timeout=1,
                    max_size=None,
                ) as ws:  # fmt: skip
                    self._ws = ws
                    await ws.send(json.dumps(self._setup()))
                    ready = json.loads(await asyncio.wait_for(ws.recv(), 10))
                    if "setupComplete" not in ready:
                        raise ValueError(f"no setupComplete: {str(ready)[:200]}")
                    self._ready.set()
                    if first:
                        first = False
                        await self._tell("The caller has just connected.")
                    async for raw in ws:
                        await self._on_message(json.loads(raw))
                problem = "Gemini closed the connection"
            except ConnectionClosed as exc:
                problem = f"Gemini closed the connection ({exc.rcvd.code if exc.rcvd else '?'}: "
                problem += f"{exc.rcvd.reason if exc.rcvd else ''})"
            except (OSError, WebSocketException, ValueError, TimeoutError) as exc:
                problem = f"{type(exc).__name__}: {str(exc)[:200]}"
            finally:
                self._ready.clear()
            if self._clock() - opened > 30:
                failures = 0
            failures += 1
            if self._resume is None or failures > RECONNECTS:
                raise ConnectionError(problem.replace(self._url, "Gemini"))
            log.warning("Gemini connection lost (%s); resuming the session", problem)

    async def _tell(self, note: str) -> None:
        """A note from the phone system, which Gemini answers as a turn."""
        turn = {"role": "user", "parts": [{"text": f"[{note}]"}]}
        await self._ws.send(json.dumps({"clientContent": {"turns": [turn], "turnComplete": True}}))

    async def _on_message(self, message: dict) -> None:
        if update := message.get("sessionResumptionUpdate"):
            if update.get("resumable") and update.get("newHandle"):
                self._resume = update["newHandle"]
        if call := message.get("toolCall"):
            await self._run_tools(call.get("functionCalls") or [])
        if message.get("goAway"):
            log.info("Gemini will move this session soon (%s left)", message["goAway"])
        content = message.get("serverContent")
        if not content:
            return
        if text := (content.get("inputTranscription") or {}).get("text"):
            self._heard.append(text)
        if text := (content.get("outputTranscription") or {}).get("text"):
            self._said.append(text)
        for part in (content.get("modelTurn") or {}).get("parts") or []:
            data = (part.get("inlineData") or {}).get("data")
            if data:
                await self._play(base64.b64decode(data))
        if content.get("interrupted"):
            # The caller cut in: drop what the phone hasn't played yet.
            self._phone = PhoneDownsampler(24000)
            await self._send({"event": "clear", "streamSid": self.stream_sid})
        if content.get("turnComplete"):
            self._flush()
            if self._closing:
                await self._hang_up()

    def _flush(self) -> None:
        """Write the turn just finished into the transcript."""
        for speaker, parts in (("caller", self._heard), ("aida", self._said)):
            if text := " ".join("".join(parts).split()):
                self.transcript.append({"speaker": speaker, "text": text})
            parts.clear()

    async def _run_tools(self, calls: list[dict]) -> None:
        self._flush()  # the caller's words come before what Aida does about them
        responses = []
        for call in calls:
            name, arguments = call.get("name", ""), call.get("args") or {}
            result = await run_tool(self._tools, name, arguments)
            if name == "create_order" and result["ok"]:
                self.order_ids.append(result["data"]["order_id"])
            if name == "escalate_to_human":
                result = self._hand_off(result)
            responses.append({"id": call.get("id"), "name": name, "response": result})
        await self._ws.send(json.dumps({"toolResponse": {"functionResponses": responses}}))

    def _hand_off(self, result: dict) -> dict:
        """After a hand-over Aida says goodbye and the call ends (or goes to the admin)."""
        alerted = result["ok"] and result["data"].get("delivered")
        lines = LINES[self.language]
        if self._settings.admin_phone:
            line = lines.transfer if alerted else lines.transfer_only
        elif not alerted:
            return no_manager()
        elif self._caller.telegram_id:
            line = lines.stepping_in
        else:
            line = lines.callback if self._caller.phone else lines.passed_on
        self.outcome = "escalated"
        self._closing = True
        say = line.format(admin=self._settings.admin_name)
        return {**result, "next": f"Say only this, then stop talking: {say}"}

    async def _hang_up(self) -> None:
        """Let the goodbye play out, then end Aida's part of the call."""
        self._marks += 1
        self._played.clear()
        await self._send(
            {"event": "mark", "streamSid": self.stream_sid, "mark": {"name": f"end:{self._marks}"}}
        )
        with suppress(TimeoutError):
            await asyncio.wait_for(self._played.wait(), GOODBYE_SECONDS)
        self._ended.set()
