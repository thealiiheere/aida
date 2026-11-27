"""Streaming speech-to-text.

Every provider is reduced to two events: Speech as soon as words are heard, which is
what triggers barge-in, and Utterance once the caller has finished talking.
"""

import asyncio
import base64
import contextlib
import json
import logging
import math
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from urllib.parse import urlencode

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from .audio import level, phone_to_pcm16k
from .config import VoiceSettings

log = logging.getLogger("aida.voice.stt")

RECONNECTS = 2
STABLE_SECONDS = 10
# Five seconds of Twilio's 20 ms frames. Older audio is worthless in a live call.
QUEUE_FRAMES = 250


@dataclass(frozen=True)
class Speech:
    text: str


@dataclass(frozen=True)
class Utterance:
    text: str
    confidence: float = 1.0


Event = Speech | Utterance


class SttUnavailable(Exception):
    """Speech recognition could not be kept running."""


class ProviderError(Exception):
    pass


class SpeechToText(ABC):
    """One provider connection per call, reopened if it drops."""

    chunk_bytes: int

    def __init__(self, url: str, headers: dict[str, str]):
        self._url = url
        self._headers = headers
        self._audio: asyncio.Queue[bytes] = asyncio.Queue(QUEUE_FRAMES)

    def set_language(self, language: str) -> None:
        """The call has moved to this language. Only VoiceLab needs telling."""

    def feed(self, audio: bytes) -> None:
        """Queue caller audio. Never blocks: if the provider stalls, the oldest is dropped."""
        if self._audio.full():
            self._audio.get_nowait()
        self._audio.put_nowait(audio)

    async def run(self, on_event: Callable[[Event], None]) -> None:
        """Stream until cancelled. Raises SttUnavailable if the provider stays down."""
        failures = 0
        while True:
            opened = time.monotonic()
            try:
                async with connect(
                    self._url,
                    additional_headers=self._headers,
                    compression=None,
                    open_timeout=5,
                    close_timeout=1,
                ) as ws:
                    await self._exchange(ws, on_event)
                problem = "closed by the provider"
            except (OSError, WebSocketException, ProviderError, ValueError) as exc:
                problem = f"{type(exc).__name__}: {exc}"
            if time.monotonic() - opened > STABLE_SECONDS:
                failures = 0
            failures += 1
            if failures > RECONNECTS:
                raise SttUnavailable(problem)
            log.warning("speech-to-text connection lost (%s), reconnecting", problem)
            await asyncio.sleep(0.2 * failures)

    async def _exchange(self, ws: ClientConnection, on_event: Callable[[Event], None]) -> None:
        sender = asyncio.create_task(self._send_audio(ws))
        try:
            async for raw in ws:
                for event in self.parse(json.loads(raw)):
                    on_event(event)
        finally:
            sender.cancel()

    async def _send_audio(self, ws: ClientConnection) -> None:
        pending = b""
        with contextlib.suppress(ConnectionClosed):  # the reader reports it
            while True:
                pending += await self._audio.get()
                if len(pending) >= self.chunk_bytes:
                    await ws.send(self.encode(pending))
                    pending = b""

    @abstractmethod
    def encode(self, audio: bytes) -> bytes | str: ...

    @abstractmethod
    def parse(self, message: dict) -> Iterator[Event]: ...


class DeepgramFlux(SpeechToText):
    chunk_bytes = 640  # 80 ms, the size Deepgram recommends for Flux

    def __init__(self, settings: VoiceSettings):
        query = urlencode(
            {
                "model": settings.deepgram_model,
                "encoding": "mulaw",
                "sample_rate": 8000,
                # The turn ends at this confidence, or after this much silence.
                "eot_threshold": settings.stt_confidence,
                "eot_timeout_ms": 2000,
            }
        )
        super().__init__(
            f"{settings.deepgram_url}/v2/listen?{query}",
            {"Authorization": f"Token {settings.deepgram_api_key.get_secret_value()}"},
        )

    def encode(self, audio: bytes) -> bytes:
        return audio

    def parse(self, message: dict) -> Iterator[Event]:
        kind = message.get("type")
        if kind == "Error":
            raise ProviderError(f"{message.get('code')}: {message.get('description')}")
        if kind != "TurnInfo":
            return
        text = (message.get("transcript") or "").strip()
        if not text:
            return
        if message.get("event") == "EndOfTurn":
            scores = [float(w["confidence"]) for w in message.get("words") or []]
            yield Utterance(text, sum(scores) / len(scores) if scores else 1.0)
        else:
            yield Speech(text)


class ElevenLabsScribe(SpeechToText):
    chunk_bytes = 800  # 100 ms, the smallest Scribe recommends

    def __init__(self, settings: VoiceSettings):
        query = urlencode(
            {
                "model_id": settings.elevenlabs_stt_model,
                "audio_format": "ulaw_8000",
                "commit_strategy": "vad",
                "vad_silence_threshold_secs": 0.5,
                "include_timestamps": "true",  # the only way to get confidence
            }
        )
        base = settings.elevenlabs_url.replace("http", "ws", 1)
        super().__init__(
            f"{base}/v1/speech-to-text/realtime?{query}",
            {"xi-api-key": settings.elevenlabs_api_key.get_secret_value()},
        )

    def encode(self, audio: bytes) -> str:
        return json.dumps(
            {
                "message_type": "input_audio_chunk",
                "audio_base_64": base64.b64encode(audio).decode(),
                "commit": False,
                "sample_rate": 8000,
            }
        )

    def parse(self, message: dict) -> Iterator[Event]:
        kind = message.get("message_type")
        text = (message.get("text") or "").strip()
        if kind == "partial_transcript":
            if text:
                yield Speech(text)
        elif kind == "committed_transcript_with_timestamps":
            if text:
                words = [w for w in message.get("words") or [] if w.get("type") == "word"]
                scores = [float(w["logprob"]) for w in words if w.get("logprob") is not None]
                yield Utterance(text, math.exp(sum(scores) / len(scores)) if scores else 1.0)
        elif "error" in message:
            raise ProviderError(f"{kind}: {message['error']}")
        # A plain committed_transcript repeats the timestamped one, so it is ignored.


FRAME = 160  # 20 ms of 8 kHz mu-law
# Speech is this much louder than the line's own hiss, and never quieter than this.
LOUDER_THAN_HISS = 3.0
QUIETEST_SPEECH = 500.0
PRE_ROLL_FRAMES = 15  # 300 ms kept from before the voice was noticed
ONSET_FRAMES = 3  # loud frames among the last five that mean someone is talking
SHORTEST_MS = 160  # shorter than this is a click or a cough
BARGE_IN_MS = 500  # this much talking stops Aida: longer than "ha" or "mhm"
LONGEST_MS = 30_000  # VoiceLab takes at most 35 s at a time
HEARD = "..."  # what a Speech event says before the words are known


class VoiceLabStt(SpeechToText):
    """VoiceLab's realtime WebSocket, which turns one finished phrase into text.

    It sends nothing while someone talks, so the phrase's start and end are found
    here, by loudness: a phrase is sent once the caller has been quiet for
    VOICELAB_PAUSE_MS. Speech is reported after half a second of talking, without
    words, which is what lets the caller cut in. There is no confidence score, so
    every transcript counts as understood.
    """

    chunk_bytes = FRAME

    def __init__(self, settings: VoiceSettings):
        base = settings.voicelab_url.replace("http", "ws", 1).rstrip("/")
        super().__init__(
            f"{base}/v1/stt/stream",
            {"xvl-api-key": settings.voicelab_api_key.get_secret_value()},
        )
        self.language = settings.voice_language
        self._pause_frames = max(1, settings.voicelab_pause_ms * 8 // FRAME)

    def set_language(self, language: str) -> None:
        self.language = language

    async def _exchange(self, ws: ClientConnection, on_event: Callable[[Event], None]) -> None:
        ready = json.loads(await asyncio.wait_for(ws.recv(), 5))
        if ready.get("event") != "ready":
            raise ProviderError(f"{ready.get('code')}: {ready.get('message')}")
        phrases: asyncio.Queue[bytes] = asyncio.Queue()
        tasks = [
            asyncio.create_task(self._find_phrases(phrases, on_event)),
            asyncio.create_task(self._transcribe(ws, phrases, on_event)),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _find_phrases(
        self, phrases: asyncio.Queue[bytes], on_event: Callable[[Event], None]
    ) -> None:
        hiss = 100.0
        recent: list[bool] = []
        before: list[bytes] = []
        phrase: list[bytes] | None = None
        loud_frames = quiet_frames = 0
        pending = b""
        while True:
            pending += await self._audio.get()
            while len(pending) >= FRAME:
                frame, pending = pending[:FRAME], pending[FRAME:]
                loudness = level(frame)
                loud = loudness > max(QUIETEST_SPEECH, hiss * LOUDER_THAN_HISS)
                if phrase is None:
                    if not loud:
                        hiss = 0.98 * hiss + 0.02 * loudness
                    recent = (recent + [loud])[-5:]
                    before = (before + [frame])[-PRE_ROLL_FRAMES:]
                    if sum(recent) >= ONSET_FRAMES:
                        phrase, loud_frames, quiet_frames = before, sum(recent), 0
                        before, recent = [], []
                    continue
                phrase.append(frame)
                if loud:
                    loud_frames += 1
                    quiet_frames = 0
                    if loud_frames * 20 == BARGE_IN_MS:
                        on_event(Speech(HEARD))
                else:
                    quiet_frames += 1
                if quiet_frames >= self._pause_frames or len(phrase) * 20 >= LONGEST_MS:
                    if loud_frames * 20 >= SHORTEST_MS:
                        # A little of the quiet is kept, so the last word isn't clipped.
                        keep = len(phrase) - max(0, quiet_frames - 10)
                        phrases.put_nowait(b"".join(phrase[:keep]))
                    phrase = None
                    loud_frames = quiet_frames = 0

    async def _transcribe(
        self, ws: ClientConnection, phrases: asyncio.Queue[bytes], on_event: Callable[[Event], None]
    ) -> None:
        """One phrase at a time: start, its audio, commit, then wait for the text."""
        while True:
            phrase = await phrases.get()
            start = {"type": "start", "language": self.language, "audio_format": "pcm_s16le",
                     "sample_rate": 16000, "channels": 1}  # fmt: skip
            await ws.send(json.dumps(start))
            pcm = phone_to_pcm16k(phrase)
            for offset in range(0, len(pcm), 3200):
                await ws.send(pcm[offset : offset + 3200])
            await ws.send(json.dumps({"type": "commit"}))
            async with asyncio.timeout(15):
                while True:
                    message = json.loads(await ws.recv())
                    for event in self.parse(message):
                        on_event(event)
                    if message.get("event") in ("final", "error"):
                        break

    def encode(self, audio: bytes) -> bytes:
        return phone_to_pcm16k(audio)

    def parse(self, message: dict) -> Iterator[Event]:
        kind = message.get("event")
        if kind == "error":
            if message.get("code") in ("no_speech_detected", "invalid_audio"):
                return  # a noise that sounded like speech
            raise ProviderError(f"{message.get('code')}: {message.get('message')}")
        if kind == "final" and (text := (message.get("text") or "").strip()):
            yield Utterance(text)


def make_stt(settings: VoiceSettings) -> SpeechToText:
    providers = {"deepgram": DeepgramFlux, "elevenlabs": ElevenLabsScribe, "voicelab": VoiceLabStt}
    return providers[settings.stt_provider](settings)
