"""Text-to-speech through ElevenLabs or VoiceLab, as 8 kHz mu-law audio."""

import asyncio
import base64
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol

import httpx2
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

from .audio import PhoneDownsampler
from .config import VoiceSettings
from .playback import BYTES_PER_MS

log = logging.getLogger("aida.voice.tts")


class TtsError(Exception):
    pass


@dataclass(frozen=True)
class AudioChunk:
    audio: bytes
    # When each character ends, in milliseconds from the start of the sentence.
    char_ends_ms: list[float] = field(default_factory=list)


class TextToSpeech(Protocol):
    def speak(
        self, text: str, previous_text: str = "", language: str | None = None
    ) -> AsyncIterator["AudioChunk"]: ...

    async def close(self) -> None: ...


class ElevenLabsTts:
    def __init__(self, settings: VoiceSettings, transport: httpx2.AsyncBaseTransport | None = None):
        self._url = (
            f"{settings.elevenlabs_url}/v1/text-to-speech/"
            f"{settings.elevenlabs_voice_id}/stream/with-timestamps"
        )
        self._model = settings.elevenlabs_tts_model
        # One client for all calls, so most requests skip the TLS handshake.
        self._client = httpx2.AsyncClient(
            headers={"xi-api-key": settings.elevenlabs_api_key.get_secret_value()},
            timeout=httpx2.Timeout(10, connect=3),
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def speak(
        self, text: str, previous_text: str = "", language: str | None = None
    ) -> AsyncIterator[AudioChunk]:
        """Stream one sentence. A failure before any audio is retried once.

        Flash v2.5 tells the language from the text, so `language` isn't needed.
        """
        for attempt in (1, 2):
            started = False
            try:
                async for chunk in self._stream(text, previous_text):
                    started = True
                    yield chunk
                if started:
                    return
                problem = "no audio returned"
            except (httpx2.HTTPError, ValueError) as exc:
                problem = f"{type(exc).__name__}: {exc}"
            if started or attempt == 2:
                raise TtsError(problem)
            log.warning("text-to-speech failed (%s), retrying", problem)

    async def _stream(self, text: str, previous_text: str) -> AsyncIterator[AudioChunk]:
        body = {"text": text, "model_id": self._model}
        if previous_text:
            body["previous_text"] = previous_text  # keeps the intonation continuous
        async with self._client.stream(
            "POST", self._url, params={"output_format": "ulaw_8000"}, json=body
        ) as response:
            if response.is_error:
                detail = (await response.aread()).decode(errors="replace")
                raise ValueError(f"HTTP {response.status_code}: {detail[:200]}")
            sent_ms = last_end_ms = 0.0
            async for item in _json_values(response.aiter_text()):
                audio = base64.b64decode(item.get("audio_base64") or "")
                ends = [], 0.0
                if alignment := item.get("alignment"):
                    ends = _char_ends(alignment, sent_ms, last_end_ms)
                char_ends, last_end_ms = ends[0], max(last_end_ms, ends[1])
                sent_ms += len(audio) / BYTES_PER_MS
                if audio or char_ends:
                    yield AudioChunk(audio, char_ends)


def _char_ends(alignment: dict, sent_ms: float, last_end_ms: float) -> tuple[list[float], float]:
    """Character end times for one chunk, measured from the start of the sentence.

    The docs don't say whether a chunk's times restart at zero. Times that go
    backwards do, so they are shifted by the audio that came before.
    """
    starts = [t * 1000 for t in alignment.get("character_start_times_seconds") or []]
    ends = [t * 1000 for t in alignment.get("character_end_times_seconds") or []]
    if not starts or not ends:
        return [], last_end_ms
    shift = sent_ms if starts[0] + 50 < last_end_ms else 0.0
    ends = [end + shift for end in ends]
    return ends, ends[-1]


async def _json_values(texts: AsyncIterator[str]) -> AsyncIterator[dict]:
    """Objects from a stream of JSON, whether separated by newlines, nothing or SSE framing."""
    decoder = json.JSONDecoder()
    buffer = ""
    async for text in texts:
        buffer += text
        while start := buffer.find("{") + 1:
            try:
                value, end = decoder.raw_decode(buffer, start - 1)
            except json.JSONDecodeError:
                break  # incomplete: wait for more
            yield value
            buffer = buffer[end:]
    if "{" in buffer:
        raise ValueError("the audio stream ended mid-message")


# --- VoiceLab -----------------------------------------------------------------

# A request may hold 8 KiB of text; Cyrillic takes two bytes a letter.
VOICELAB_MAX_CHARS = 2000
# A connection opened ahead for the next sentence is thrown away after this long,
# in case VoiceLab has closed it in the meantime.
SPARE_SECONDS = 20


class VoiceLabTts:
    """VoiceLab's realtime stream: raw PCM, sent while the sentence is being made.

    Each connection takes one sentence, so the next one is opened while this one is
    spoken: opening costs a TLS handshake. There are no character timings, so an
    interruption is placed by the share of the sentence's audio that was heard.
    """

    def __init__(self, settings: VoiceSettings):
        self._url = settings.voicelab_url.replace("http", "ws", 1).rstrip("/") + "/v1/tts/stream"
        self._headers = {"xvl-api-key": settings.voicelab_api_key.get_secret_value()}
        self._voice = settings.voicelab_voice
        self._speed = settings.voicelab_speed
        self._language = settings.voice_language
        self._spare: asyncio.Task | None = None
        self._spare_at = 0.0

    async def close(self) -> None:
        await self._drop_spare()

    async def speak(
        self, text: str, previous_text: str = "", language: str | None = None
    ) -> AsyncIterator[AudioChunk]:
        """Speak one sentence, in `language` or else the line's own.

        A failure before any audio is retried once, on a new connection.
        """
        request = {
            "text": text[:VOICELAB_MAX_CHARS],
            "language": language or self._language,
            "voice_id": self._voice,
            "speed": self._speed,
        }
        for attempt in (1, 2):
            started = False
            try:
                async for audio in self._stream(request, fresh=attempt == 2):
                    started = True
                    yield AudioChunk(audio)
                if started:
                    return
                problem = "no audio returned"
            except (OSError, WebSocketException, ValueError, TimeoutError) as exc:
                problem = f"{type(exc).__name__}: {exc}"
            if started or attempt == 2:
                raise TtsError(problem)
            log.warning("text-to-speech failed (%s), retrying", problem)

    async def _stream(self, request: dict, fresh: bool) -> AsyncIterator[bytes]:
        ws, rate = await self._connection(fresh)
        try:
            await ws.send(json.dumps(request))
            phone = PhoneDownsampler(rate)
            while True:
                frame = await asyncio.wait_for(ws.recv(), 10)
                if isinstance(frame, bytes):
                    if audio := phone.feed(frame):
                        yield audio
                    continue
                message = _message(frame)
                if message.get("event") == "done":
                    return
                if message.get("event") == "error":
                    raise ValueError(_problem(message))
        finally:
            await _end(ws)

    async def _connection(self, fresh: bool) -> tuple[ClientConnection, int]:
        """The spare connection if it's fresh enough, else a new one. Then a new spare."""
        spare, self._spare = self._spare, None
        found = None
        if spare and not fresh and time.monotonic() - self._spare_at < SPARE_SECONDS:
            try:
                found = await spare
            except (OSError, WebSocketException, ValueError, TimeoutError):
                found = None
        elif spare:
            self._spare = spare
            await self._drop_spare()
        if found is None:
            found = await self._open()
        self._spare = asyncio.create_task(self._open())
        self._spare.add_done_callback(_quietly)
        self._spare_at = time.monotonic()
        return found

    async def _drop_spare(self) -> None:
        spare, self._spare = self._spare, None
        if spare is None:
            return
        spare.cancel()
        try:
            ws, _ = await spare
        except (asyncio.CancelledError, OSError, WebSocketException, ValueError, TimeoutError):
            return
        await _end(ws)

    async def _open(self) -> tuple[ClientConnection, int]:
        ws = await connect(
            self._url,
            additional_headers=self._headers,
            compression=None,
            open_timeout=5,
            close_timeout=1,
        )
        try:
            ready = _message(await asyncio.wait_for(ws.recv(), 5))
            if ready.get("event") != "ready":
                raise ValueError(_problem(ready))
            if ready.get("format", "pcm_s16le") != "pcm_s16le":
                raise ValueError(f"unexpected audio format {ready.get('format')}")
            return ws, int(ready.get("sample_rate") or 24000)
        except BaseException:
            await _end(ws)
            raise


def _quietly(task: asyncio.Task) -> None:
    """A spare that failed to open is reported when it's used, not as an unread error."""
    if not task.cancelled():
        task.exception()


def _message(frame: str | bytes) -> dict:
    if isinstance(frame, bytes):
        raise ValueError("expected a message, got audio")
    message = json.loads(frame)
    if not isinstance(message, dict):
        raise ValueError("expected a JSON object")
    return message


def _problem(message: dict) -> str:
    return f"{message.get('code') or message.get('event')}: {message.get('message', '')}"[:200]


async def _end(ws: ClientConnection) -> None:
    try:
        await ws.close()
    except (OSError, WebSocketException):
        pass


def make_tts(settings: VoiceSettings) -> TextToSpeech:
    if settings.tts_provider == "voicelab":
        return VoiceLabTts(settings)
    return ElevenLabsTts(settings)
