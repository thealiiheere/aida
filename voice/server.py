"""The phone line: answers Twilio's webhooks and its audio WebSocket.

    uv run uvicorn voice.server:app --port 8001

Calls live in memory, so run a single worker.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from urllib.parse import parse_qsl
from xml.sax.saxutils import escape, quoteattr

import httpx2
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect

from agent.llm import make_llm
from agent.tools import StoreTools
from app.limits import RateLimiter

from .backend import StoreBackend, caller_phone
from .config import VoiceSettings
from .gemini_live import GeminiLiveSession, make_session, uses_pipeline
from .session import Caller, CallSession
from .tts import make_tts

log = logging.getLogger("aida.voice")

START_TIMEOUT_SECONDS = 10
PENDING_SECONDS = 300
MAX_REMEMBERED = 1000

NOT_SET_UP = "Sorry, our phone assistant isn't available right now. Please try again later."
BROKE_DOWN = "Sorry, we're having a technical problem."
CONNECTING = "I'll connect you with our store manager."
CALL_LATER = "Please call again in a few minutes."
NO_ANSWER = "Sorry, nobody could pick up just now. Please call again later."


@dataclass
class Expected:
    """A call Twilio told us about, whose audio stream has yet to connect."""

    caller: Caller
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    expires: float = field(default_factory=lambda: time.monotonic() + PENDING_SECONDS)


def signature(auth_token: str, url: str, form: list[tuple[str, str]]) -> str:
    """Twilio's X-Twilio-Signature for a form POST."""
    signed = url + "".join(name + value for name, value in sorted(form))
    digest = hmac.new(auth_token.encode(), signed.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def twiml(*verbs: str) -> Response:
    body = f'<?xml version="1.0" encoding="UTF-8"?><Response>{"".join(verbs)}</Response>'
    return Response(body, media_type="text/xml")


def say(*lines: str) -> str:
    return f"<Say>{escape(' '.join(lines))}</Say>"


def create_app(settings: VoiceSettings | None = None) -> FastAPI:
    settings = settings or VoiceSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.getLogger("httpx2").setLevel(logging.WARNING)  # it logs every URL
        if not logging.getLogger().handlers:
            logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(message)s")
        state = app.state
        state.expected: dict[str, Expected] = {}
        state.outcomes: OrderedDict[str, str] = OrderedDict()
        state.sessions: dict[str, CallSession | GeminiLiveSession] = {}
        state.limiter = RateLimiter()
        state.backend = StoreBackend(settings)
        state.store_client = httpx2.Client(base_url=settings.backend_url, timeout=15)
        state.llm = make_llm(settings)
        state.missing = settings.missing
        pipeline = uses_pipeline(settings)
        if pipeline and not state.llm.has_credentials:
            state.missing.append(state.llm.key_name)
        state.tts = make_tts(settings) if pipeline and not state.missing else None
        if state.missing:
            log.warning("calls will be turned away until these are set: %s", state.missing)
        yield
        await state.backend.close()
        await state.llm.close()
        state.store_client.close()
        if state.tts:
            await state.tts.close()

    app = FastAPI(title="Aida voice server", lifespan=lifespan, docs_url=None, redoc_url=None)

    async def twilio_form(request: Request) -> dict[str, str]:
        """The request's form, once proven to come from Twilio."""
        if not (settings.twilio_auth_token and settings.voice_public_url):
            raise HTTPException(503, "TWILIO_AUTH_TOKEN and VOICE_PUBLIC_URL are not set.")
        form = parse_qsl((await request.body()).decode(), keep_blank_values=True)
        # Built from the public URL, which is what Twilio signed: behind a proxy,
        # request.url has another host and scheme.
        url = settings.voice_public_url.rstrip("/") + request.url.path
        if request.url.query:
            url += f"?{request.url.query}"
        expected = signature(settings.twilio_auth_token.get_secret_value(), url, form)
        given = request.headers.get("X-Twilio-Signature", "")
        if not hmac.compare_digest(expected.encode(), given.encode()):
            raise HTTPException(403, "Invalid Twilio signature.")
        return dict(form)

    @app.get("/health")
    async def health() -> dict:
        return {"ready": not app.state.missing, "active_calls": len(app.state.sessions)}

    @app.post("/voice/incoming")
    async def incoming(request: Request) -> Response:
        """A call is ringing. Answer it and have Twilio open the audio stream."""
        form = await twilio_form(request)
        if app.state.missing:
            log.error("call turned away, not set: %s", app.state.missing)
            return twiml(say(NOT_SET_UP), "<Hangup/>")
        phone = caller_phone(form.get("From", ""))
        customer = await app.state.backend.find_customer(phone) if phone else None
        customer = customer or {}
        # Hidden numbers aren't limited: many unrelated people share each one.
        account = customer.get("telegram_id") or phone
        if account and app.state.limiter.wait(account):
            log.warning("call refused, too many in a minute from the same caller")
            return twiml('<Reject reason="busy"/>')
        call = Expected(
            Caller(
                phone=phone,
                telegram_id=customer.get("telegram_id"),
                telegram_username=customer.get("telegram_username"),
                name=customer.get("name"),
            )
        )
        expected = app.state.expected
        for sid in [sid for sid, c in expected.items() if c.expires < time.monotonic()]:
            del expected[sid]
        expected[form.get("CallSid", "")] = call

        base = settings.voice_public_url.rstrip("/")
        stream = base.replace("http", "ws", 1) + "/voice/stream"
        return twiml(
            f'<Connect action={quoteattr(f"{base}/voice/after")} method="POST">',
            f"<Stream url={quoteattr(stream)}>",
            f'<Parameter name="token" value={quoteattr(call.token)}/>',
            "</Stream></Connect>",
        )

    @app.post("/voice/after")
    async def after(request: Request) -> Response:
        """Aida's part of the call is over. Put the caller through, or hang up."""
        form = await twilio_form(request)
        outcome = app.state.outcomes.pop(form.get("CallSid", ""), None)
        admin = settings.admin_phone
        dial = [f"<Dial>{escape(admin)}</Dial>", say(NO_ANSWER)] if admin else []
        if outcome == "escalated" and dial:
            return twiml(*dial)
        if outcome == "failed":
            if dial:
                return twiml(say(BROKE_DOWN, CONNECTING), *dial)
            return twiml(say(BROKE_DOWN, CALL_LATER))
        return twiml("<Hangup/>")

    @app.websocket("/voice/stream")
    async def stream(ws: WebSocket) -> None:
        """Both directions of a call's audio."""
        await ws.accept()
        try:
            start = await asyncio.wait_for(_read_start(ws), START_TIMEOUT_SECONDS)
            call = _claim(app.state.expected, start)
        except (TimeoutError, WebSocketDisconnect, ValueError, KeyError, TypeError) as exc:
            log.warning("audio stream refused: %s", exc or type(exc).__name__)
            with suppress(RuntimeError, WebSocketDisconnect):
                await ws.close(1008)
            return

        async def send(message: dict) -> None:
            # If the caller has gone, the reading side notices and ends the call.
            with suppress(RuntimeError, WebSocketDisconnect):
                await ws.send_text(json.dumps(message))

        caller = call.caller
        key = settings.agent_api_key
        session = make_session(
            settings,
            tts=app.state.tts,
            llm=app.state.llm,
            tools=StoreTools(
                api_key=key.get_secret_value() if key else None,
                customer_telegram_id=caller.telegram_id,
                customer_telegram_username=caller.telegram_username,
                customer_phone=caller.phone,
                live=True,
                client=app.state.store_client,
            ),
            caller=caller,
            call_sid=start["callSid"],
            stream_sid=start["streamSid"],
            send=send,
        )
        app.state.sessions[session.call_sid] = session
        log.info("call %s started", session.call_sid)
        try:
            await session.run(_messages(ws))
        finally:
            del app.state.sessions[session.call_sid]
            # Stored before closing: Twilio asks /voice/after what's next right away.
            outcomes = app.state.outcomes
            outcomes[session.call_sid] = session.outcome
            while len(outcomes) > MAX_REMEMBERED:
                outcomes.popitem(last=False)
            with suppress(RuntimeError, WebSocketDisconnect):
                await ws.close()
        log.info("call %s ended: %s", session.call_sid, session.outcome)
        if session.outcome == "failed":
            await session.alert_problem()
        if session.order_ids:
            await app.state.backend.report_call(session.report())
        await app.state.backend.call_ended(session.ended())

    return app


async def _messages(ws: WebSocket) -> AsyncIterator[dict]:
    """Twilio's JSON messages, until it disconnects."""
    while True:
        received = await ws.receive()
        if received["type"] == "websocket.disconnect":
            return
        try:
            message = json.loads(received.get("text") or "")
        except ValueError:
            continue
        if isinstance(message, dict):
            yield message


async def _read_start(ws: WebSocket) -> dict:
    async for message in _messages(ws):
        if message.get("event") == "start":
            return message["start"]
    raise ValueError("disconnected before starting")


def _claim(expected: dict[str, Expected], start: dict) -> Expected:
    """Match a stream to a call we answered. Each call's token works once."""
    call = expected.pop(start["callSid"], None)
    token = start["customParameters"]["token"]
    if call is None or call.expires < time.monotonic():
        raise ValueError("unknown call")
    if not hmac.compare_digest(call.token.encode(), str(token).encode()):
        raise ValueError("wrong token")
    audio = start["mediaFormat"]
    if (audio["encoding"], audio["sampleRate"], audio["channels"]) != ("audio/x-mulaw", 8000, 1):
        raise ValueError(f"unsupported audio {audio}")
    return call


app = create_app()
