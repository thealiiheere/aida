"""The phone line over Bluetooth: Aida answers calls to an ordinary mobile phone.

    uv run --group bluetooth python -m voice.bluetooth

The computer joins the phone the way a car's hands-free kit does (the HFP profile).
oFono, over D-Bus, says when a call rings and answers it; PulseAudio or PipeWire
carries its audio. Both only exist on Linux, so this runs on Linux: a Raspberry Pi or
an old laptop next to the phone. README.md has the setup.

Everything after the audio is the same CallSession that serves Twilio calls. This
file makes the phone look like Twilio's media stream: caller audio arrives as
"media" messages, Aida's audio is played in real time, and a "mark" comes back when
a sentence has been played, so interruptions are cut at the right word.
"""

import asyncio
import base64
import contextlib
import logging
import re
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

import httpx2

from agent.llm import make_llm
from agent.tools import StoreTools

from .audio import mulaw_to_pcm, pcm_to_mulaw
from .backend import StoreBackend, caller_phone
from .config import VoiceSettings
from .gemini_live import make_session, uses_pipeline
from .session import Caller
from .tts import make_tts

log = logging.getLogger("aida.voice.bluetooth")

FRAME_BYTES = 160  # 20 ms of 8 kHz mu-law, Twilio's frame size
FRAME_SECONDS = 0.02
SILENCE = b"\xff" * FRAME_BYTES
# Audio handed to the player ahead of time. After an interruption this, and the
# player's own buffer, still plays.
PLAY_AHEAD_SECONDS = 0.1


# --- The phone ----------------------------------------------------------------


@dataclass
class PhoneCall:
    """One call ringing on the phone."""

    number: str  # as the phone shows it, which may be local: "901234567"
    answer: Callable[[], Awaitable[None]]
    hang_up: Callable[[], Awaitable[None]]
    ended: asyncio.Event = field(default_factory=asyncio.Event)


class Phone(ABC):
    @abstractmethod
    def ringing(self) -> AsyncIterator[PhoneCall]:
        """Each incoming call, as it starts to ring."""

    async def close(self) -> None:
        pass


class OfonoPhone(Phone):
    """A phone joined over Bluetooth, through oFono's D-Bus API."""

    def __init__(self, modem: str | None = None):
        self._modem = modem
        self._bus = None

    async def ringing(self) -> AsyncIterator[PhoneCall]:
        from dbus_fast import BusType  # optional: uv sync --group bluetooth
        from dbus_fast.aio import MessageBus

        self._bus = bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        path = self._modem or await self._find_modem(bus)
        log.info("listening for calls on %s", path)
        manager = await self._interface(path, "org.ofono.VoiceCallManager")
        calls: dict[str, PhoneCall] = {}
        rung: asyncio.Queue[PhoneCall] = asyncio.Queue()

        def added(call_path: str, properties: dict) -> None:
            state = properties["State"].value
            if state != "incoming":
                log.info("ignored a %s call", state)
                return
            number = properties.get("LineIdentification")
            call = PhoneCall(
                number=number.value if number else "",
                answer=lambda: self._call(call_path, "answer"),
                hang_up=lambda: self._call(call_path, "hangup"),
            )
            calls[call_path] = call
            rung.put_nowait(call)

        def removed(call_path: str) -> None:
            if call := calls.pop(call_path, None):
                call.ended.set()

        manager.on_call_added(added)
        manager.on_call_removed(removed)
        while True:
            yield await rung.get()

    async def _find_modem(self, bus) -> str:
        manager = await self._interface("/", "org.ofono.Manager")
        for path, properties in await manager.call_get_modems():
            interfaces = properties.get("Interfaces")
            if interfaces and "org.ofono.VoiceCallManager" in interfaces.value:
                return path
        raise RuntimeError(
            "oFono has no phone that can take calls. Is the phone paired, connected and "
            "set up for hands-free calls?"
        )

    async def _interface(self, path: str, name: str):
        introspection = await self._bus.introspect("org.ofono", path)
        return self._bus.get_proxy_object("org.ofono", path, introspection).get_interface(name)

    async def _call(self, path: str, action: str) -> None:
        call = await self._interface(path, "org.ofono.VoiceCall")
        await getattr(call, f"call_{action}")()

    async def close(self) -> None:
        if self._bus:
            self._bus.disconnect()


# --- The call's audio -----------------------------------------------------------


class Audio(ABC):
    """The call's two directions of sound, as 8 kHz mu-law."""

    @abstractmethod
    async def open(self) -> None: ...

    @abstractmethod
    async def read(self) -> bytes:
        """The next 20 ms from the caller. Empty once the audio has stopped."""

    @abstractmethod
    async def write(self, frame: bytes) -> None:
        """Play 20 ms to the caller."""

    @abstractmethod
    async def close(self) -> None: ...


class PulseAudio(Audio):
    """The call's audio through PulseAudio's tools, which PipeWire also provides."""

    LATENCY_MS = 60  # what is queued in the player, and so still plays after a clear

    def __init__(self, source: str | None = None, sink: str | None = None):
        self._source, self._sink = source, sink
        self._recorder: asyncio.subprocess.Process | None = None
        self._player: asyncio.subprocess.Process | None = None

    async def open(self) -> None:
        source = self._source or await _bluetooth_device("sources")
        sink = self._sink or await _bluetooth_device("sinks")
        raw = ["--format=s16le", "--rate=8000", "--channels=1", "--raw"]
        self._recorder = await asyncio.create_subprocess_exec(
            "parec", f"--device={source}", *raw, "--latency-msec=20",
            stdout=asyncio.subprocess.PIPE,
        )  # fmt: skip
        self._player = await asyncio.create_subprocess_exec(
            "pacat", "--playback", f"--device={sink}", *raw,
            f"--latency-msec={self.LATENCY_MS}", stdin=asyncio.subprocess.PIPE,
        )  # fmt: skip

    async def read(self) -> bytes:
        try:
            pcm = await self._recorder.stdout.readexactly(FRAME_BYTES * 2)
        except asyncio.IncompleteReadError:
            return b""
        return pcm_to_mulaw(memoryview(pcm).cast("h"))

    async def write(self, frame: bytes) -> None:
        self._player.stdin.write(mulaw_to_pcm(frame).tobytes())
        with contextlib.suppress(ConnectionError):
            await self._player.stdin.drain()

    async def close(self) -> None:
        for process in (self._recorder, self._player):
            if process and process.returncode is None:
                process.terminate()
                with contextlib.suppress(ProcessLookupError):
                    await process.wait()


async def _bluetooth_device(kind: str) -> str:
    """The first Bluetooth source or sink PulseAudio knows of."""
    process = await asyncio.create_subprocess_exec(
        "pactl", "list", "short", kind, stdout=asyncio.subprocess.PIPE
    )
    listing, _ = await process.communicate()
    for line in listing.decode(errors="replace").splitlines():
        name = line.split("\t")[1] if "\t" in line else ""
        if name.startswith("bluez_") and not name.endswith(".monitor"):
            return name
    raise RuntimeError(
        f"no Bluetooth {kind[:-1]} found. Is a call's audio connected? "
        f"Set BLUETOOTH_{'SOURCE' if kind == 'sources' else 'SINK'} to choose one."
    )


# --- Looking like Twilio --------------------------------------------------------


class Line:
    """One call's audio, in and out, in the shape of Twilio's media stream."""

    def __init__(self, audio: Audio, ended: asyncio.Event, clock=time.monotonic):
        self._audio = audio
        self._ended = ended
        self._clock = clock
        self._messages: asyncio.Queue[dict] = asyncio.Queue()
        # Aida's audio waiting to play: 20 ms frames, and the names of marks.
        self._playing: asyncio.Queue[bytes | str] = asyncio.Queue()

    async def messages(self) -> AsyncIterator[dict]:
        """What Twilio would send: caller audio, marks, and a stop when the call ends."""
        jobs = [asyncio.create_task(job) for job in (self._listen(), self._play())]
        ended = asyncio.create_task(self._ended.wait())
        try:
            while True:
                getting = asyncio.create_task(self._messages.get())
                done, _ = await asyncio.wait(
                    [getting, ended, *jobs], return_when=asyncio.FIRST_COMPLETED
                )
                if getting not in done:
                    getting.cancel()
                    for job in jobs:
                        if job in done and job.exception():
                            log.error("call audio failed", exc_info=job.exception())
                    yield {"event": "stop"}
                    return
                yield getting.result()
        finally:
            for task in (*jobs, ended):
                task.cancel()
            await asyncio.gather(*jobs, ended, return_exceptions=True)

    async def send(self, message: dict) -> None:
        """What CallSession sends Twilio."""
        match message.get("event"):
            case "media":
                audio = base64.b64decode(message["media"]["payload"])
                for start in range(0, len(audio), FRAME_BYTES):
                    self._playing.put_nowait(audio[start : start + FRAME_BYTES].ljust(
                        FRAME_BYTES, b"\xff"
                    ))  # fmt: skip
            case "mark":
                self._playing.put_nowait(message["mark"]["name"])
            case "clear":
                # Like Twilio: drop the waiting audio and return its marks.
                while not self._playing.empty():
                    item = self._playing.get_nowait()
                    if isinstance(item, str):
                        self._mark(item)

    def _mark(self, name: str) -> None:
        self._messages.put_nowait({"event": "mark", "mark": {"name": name}})

    async def _listen(self) -> None:
        while frame := await self._audio.read():
            payload = base64.b64encode(frame).decode()
            self._messages.put_nowait({"event": "media", "media": {"payload": payload}})

    async def _play(self) -> None:
        """Play in real time, so a clear stops the audio instead of a full buffer."""
        ends = 0.0  # when the audio written so far finishes playing
        while True:
            item = await self._playing.get()
            if isinstance(item, str):
                self._mark(item)
                continue
            # Stay at most PLAY_AHEAD ahead: timers are too coarse to wake every 20 ms.
            if (wait := ends - self._clock() - PLAY_AHEAD_SECONDS) > 0:
                await asyncio.sleep(wait)
            await self._audio.write(item)
            ends = max(ends, self._clock()) + FRAME_SECONDS


# --- Answering ------------------------------------------------------------------


def phone_number(shown: str) -> str | None:
    """A number as the phone shows it, in the store's +998 format, or None if hidden."""
    digits = re.sub(r"[^\d+]", "", shown)
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    elif re.fullmatch(r"998\d{9}", digits):
        digits = "+" + digits
    elif re.fullmatch(r"\d{9}", digits):  # dialled inside Uzbekistan
        digits = "+998" + digits
    return caller_phone(digits)


class BluetoothLine:
    """Answers each call on the phone and lets Aida handle it. One call at a time."""

    def __init__(
        self,
        settings: VoiceSettings,
        phone: Phone | None = None,
        audio: Callable[[], Audio] | None = None,
        backend: StoreBackend | None = None,
    ):
        # A call can't be put through to the manager from here, so Aida never offers it.
        self._settings = settings.model_copy(update={"admin_phone": None})
        self._phone = phone or OfonoPhone(settings.bluetooth_modem)
        self._make_audio = audio or (
            lambda: PulseAudio(settings.bluetooth_source, settings.bluetooth_sink)
        )
        self._backend = backend
        self.calls_handled = 0

    async def run(self) -> None:
        settings = self._settings
        llm = make_llm(settings)
        pipeline = uses_pipeline(settings)
        missing = settings.missing_speech
        if pipeline and not llm.has_credentials:
            missing = missing + [llm.key_name]
        if missing:
            await llm.close()
            raise SystemExit(f"Not set in .env: {', '.join(missing)}")
        backend = self._backend or StoreBackend(settings)
        store = httpx2.Client(base_url=settings.backend_url, timeout=15)
        tts = make_tts(settings) if pipeline else None
        busy = False
        try:
            async for call in self._phone.ringing():
                if busy:
                    log.info("a second call rang during a call; the phone handles it")
                    continue
                busy = True
                try:
                    await self._handle(call, llm, tts, backend, store)
                except Exception:
                    log.exception("the call could not be handled")
                    with contextlib.suppress(Exception):
                        await call.hang_up()
                finally:
                    busy = False
                    self.calls_handled += 1
        finally:
            await self._phone.close()
            if tts:
                await tts.close()
            await llm.close()
            await backend.close()
            store.close()

    async def _handle(self, call: PhoneCall, llm, tts, backend, store) -> None:
        settings = self._settings
        phone = phone_number(call.number)
        customer = (await backend.find_customer(phone) if phone else None) or {}
        caller = Caller(
            phone=phone,
            telegram_id=customer.get("telegram_id"),
            telegram_username=customer.get("telegram_username"),
            name=customer.get("name"),
        )
        await call.answer()
        audio = self._make_audio()
        await audio.open()
        line = Line(audio, call.ended)
        key = settings.agent_api_key
        session = make_session(
            settings,
            tts=tts,
            llm=llm,
            tools=StoreTools(
                api_key=key.get_secret_value() if key else None,
                customer_telegram_id=caller.telegram_id,
                customer_telegram_username=caller.telegram_username,
                customer_phone=caller.phone,
                live=True,
                client=store,
            ),
            caller=caller,
            call_sid=f"BT{int(time.time() * 1000)}",
            stream_sid="bluetooth",
            send=line.send,
        )
        log.info("call %s answered", session.call_sid)
        try:
            await session.run(line.messages())
        finally:
            await audio.close()
            if not call.ended.is_set():
                with contextlib.suppress(Exception):
                    await call.hang_up()
        log.info("call %s ended: %s", session.call_sid, session.outcome)
        if session.outcome == "failed":
            await session.alert_problem()
        if session.order_ids:
            await backend.report_call(session.report())
        await backend.call_ended(session.ended())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(message)s")
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(BluetoothLine(VoiceSettings()).run())


if __name__ == "__main__":
    main()
