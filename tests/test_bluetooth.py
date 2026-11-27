"""The Bluetooth phone line, with the phone and its audio simulated.

The speech providers and Claude are the same local stand-ins the Twilio tests use;
what the caller says travels as text hidden in the audio, which passes through the
bridge untouched.
"""

import asyncio
import base64

import pytest

from tests.fakes import FakeClaude, Reply, voice_settings
from tests.simulation import SILENCE, Providers, serve, voice_audio
from voice.bluetooth import FRAME_BYTES, Audio, BluetoothLine, Line, Phone, PhoneCall, phone_number
from voice.session import GREETING

HELLO = GREETING.format(store="Test Store")


class FakeAudio(Audio):
    def __init__(self):
        self.heard = asyncio.Queue()  # what the caller says, in 20 ms frames
        self.played: list[bytes] = []
        self.opened = self.closed = False

    async def open(self):
        self.opened = True

    async def read(self) -> bytes:
        try:
            return await asyncio.wait_for(self.heard.get(), 0.02)
        except TimeoutError:
            return SILENCE

    async def write(self, frame: bytes):
        assert len(frame) == FRAME_BYTES
        self.played.append(frame)

    async def close(self):
        self.closed = True

    def say(self, text: str, confidence: float = 0.95):
        audio = f"<final {confidence}>{text}</>".encode()
        for start in range(0, len(audio), FRAME_BYTES):
            self.heard.put_nowait(audio[start : start + FRAME_BYTES].ljust(FRAME_BYTES, b"\xff"))

    def said(self) -> str:
        """Aida's words, read back out of the audio she played, without spaces.

        Each sentence is spoken on its own, so the spaces between them aren't heard.
        """
        audio = b"".join(self.played)
        words = bytes(audio[i] for i in range(0, len(audio), 80) if audio[i] != 0xFF).decode()
        return words.replace(" ", "")


def heard(text: str, audio: FakeAudio) -> bool:
    return text.replace(" ", "") in audio.said()


class FakePhone(Phone):
    def __init__(self, *numbers: str):
        self.numbers = numbers
        self.events: list[str] = []
        self.calls: list[PhoneCall] = []

    async def ringing(self):
        for number in self.numbers:
            async def answer(n=number):
                self.events.append(f"answer {n}")

            async def hang_up(n=number):
                self.events.append(f"hang up {n}")
                call.ended.set()

            call = PhoneCall(number, answer, hang_up)
            self.calls.append(call)
            yield call


class FakeBackend:
    def __init__(self):
        self.lookups, self.ended = [], []

    async def find_customer(self, phone):
        self.lookups.append(phone)
        return {"telegram_id": "42", "name": "Dilnoza"} if phone == "+998901234567" else None

    async def report_call(self, report):
        return True

    async def call_ended(self, call):
        self.ended.append(call)
        return True

    async def close(self):
        pass


@pytest.mark.parametrize(
    "shown, phone",
    [
        ("+998901234567", "+998901234567"),
        ("998901234567", "+998901234567"),
        ("901234567", "+998901234567"),
        ("90 123-45-67", "+998901234567"),
        ("00447911123456", "+447911123456"),
        ("", None),
        ("Unknown", None),
    ],
)
def test_phone_numbers_as_the_store_keeps_them(shown, phone):
    assert phone_number(shown) == phone


def test_a_call_is_answered_and_handled_like_a_twilio_call():
    claude = FakeClaude(Reply("Yes, we have earbuds."))
    providers = Providers(claude)
    phone, audio, backend = FakePhone("901234567"), FakeAudio(), FakeBackend()

    async def run(url):
        settings = voice_settings(
            deepgram_url=url.replace("http", "ws"), elevenlabs_url=url, anthropic_base_url=url,
            silence_seconds=30, admin_phone="+998900000000",
            twilio_auth_token=None, voice_public_url=None,  # not needed over Bluetooth
        )  # fmt: skip
        line = BluetoothLine(settings, phone, lambda: audio, backend)
        task = asyncio.create_task(line.run())
        async with asyncio.timeout(15):
            while not heard(HELLO, audio):
                await asyncio.sleep(0.02)
            audio.say("Do you have earbuds?")
            while not heard("Yes, we have earbuds.", audio):
                await asyncio.sleep(0.02)
            phone.calls[0].ended.set()  # the caller hangs up
            while not backend.ended:
                await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return line

    with serve(providers.app) as url:
        asyncio.run(run(url))

    assert phone.events == ["answer 901234567"]  # the caller hung up, not Aida
    assert backend.lookups == ["+998901234567"]
    assert audio.opened and audio.closed
    (ended,) = backend.ended
    assert ended["caller_phone"] == "+998901234567" and ended["caller_telegram_id"] == "42"
    assert "Caller: Do you have earbuds?" in ended["raw_audio_transcript"]


def test_audio_plays_in_real_time_and_a_clear_returns_its_marks():
    async def run():
        audio = FakeAudio()
        ended = asyncio.Event()
        line = Line(audio, ended)
        messages = line.messages()
        first = await anext(messages)
        assert first["event"] == "media"  # the caller's side flows straight away

        speech = base64.b64encode(voice_audio("Hello there")).decode()  # 880 ms
        await line.send({"event": "media", "media": {"payload": speech}})
        await line.send({"event": "mark", "mark": {"name": "1:0"}})
        await asyncio.sleep(0.2)
        played = len(audio.played)
        assert 5 <= played <= 13  # about 10 frames in 200 ms, not all 44 at once
        await line.send({"event": "clear"})
        marks = []
        async with asyncio.timeout(2):
            async for message in messages:
                if message["event"] == "mark":
                    marks.append(message["mark"]["name"])
                    break
        assert marks == ["1:0"]
        await asyncio.sleep(0.1)
        assert len(audio.played) <= played + 2  # the rest was dropped

        ended.set()
        rest = [m["event"] async for m in messages]
        assert rest[-1] == "stop"

    asyncio.run(run())


def test_a_missing_key_stops_the_line_before_any_call():
    settings = voice_settings(tts_provider="voicelab", stt_provider="voicelab",
                              voicelab_api_key=None)  # fmt: skip
    line = BluetoothLine(settings, FakePhone(), FakeAudio, FakeBackend())
    with pytest.raises(SystemExit, match="VOICELAB_API_KEY"):
        asyncio.run(line.run())
