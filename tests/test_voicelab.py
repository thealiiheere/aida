"""VoiceLab speech, Uzbek on the phone, and the audio conversions behind them."""

import asyncio
import io
import json
import math
import wave
from array import array

import pytest
from fastapi import FastAPI, WebSocket
from starlette.websockets import WebSocketDisconnect

from agent.prompts import voice_prompt
from tests.fakes import voice_settings
from tests.simulation import serve
from voice.audio import (
    PhoneDownsampler,
    double_rate,
    halve_rate,
    level,
    mulaw_to_pcm,
    pcm_to_mulaw,
    phone_to_pcm16k,
    wav_to_phone,
)
from voice.session import LINES
from voice.speech import is_backchannel, speakable, uzbek_number_words
from voice.stt import Speech, SttUnavailable, Utterance, make_stt
from voice.tts import TtsError, VoiceLabTts, make_tts

# --- Audio --------------------------------------------------------------------


def tone(hz: float, rate: int = 16000, seconds: float = 0.5, level: int = 10000) -> array:
    return array("h", (round(level * math.sin(2 * math.pi * hz * i / rate))
                       for i in range(int(rate * seconds))))  # fmt: skip


def wav(samples: array, rate: int = 16000, channels: int = 1) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())
    return out.getvalue()


def peak(samples) -> int:
    return max(abs(s) for s in samples[50:-50])


def test_mulaw_survives_a_round_trip():
    # 0x7F is mu-law's "negative zero", which comes back as plain zero.
    codes = bytes(c for c in range(256) if c != 0x7F)
    assert pcm_to_mulaw(mulaw_to_pcm(codes)) == codes
    assert pcm_to_mulaw(array("h", [0, 32767, -32768])) == b"\xff\x80\x00"


def test_speech_band_passes_and_what_8khz_cant_carry_is_cut():
    for hz in (300, 1000, 3000):
        assert peak(halve_rate(tone(hz))) == pytest.approx(10000, rel=0.15)
    assert peak(halve_rate(tone(7000))) < 300  # would alias down to 1 kHz otherwise


def test_voicelab_audio_becomes_phone_audio():
    audio = wav_to_phone(wav(tone(1000)))
    assert len(audio) == 4000  # half a second at 8 kHz, one byte a sample
    assert peak(mulaw_to_pcm(audio)) == pytest.approx(10000, rel=0.1)
    # Other rates and stereo still come out at 8 kHz.
    assert len(wav_to_phone(wav(tone(1000, rate=24000), rate=24000))) == 4000
    stereo = array("h", [s for s in tone(1000) for _ in range(2)])
    assert len(wav_to_phone(wav(stereo, channels=2))) == 4000


def test_phone_audio_becomes_voicelab_audio():
    phone = pcm_to_mulaw(tone(500, rate=8000, seconds=0.1))
    pcm = array("h", phone_to_pcm16k(phone))
    assert len(pcm) == 1600  # 100 ms at 16 kHz
    # The original samples are kept; the new ones sit between them.
    assert pcm[::2] == mulaw_to_pcm(phone)
    assert double_rate(array("h", [0, 100])) == array("h", [0, 50, 100, 100])


def test_streamed_voicelab_audio_becomes_phone_audio():
    pcm = tone(1000, rate=24000).tobytes()
    whole = PhoneDownsampler(24000).feed(pcm)
    assert len(whole) == 4000  # half a second at 8 kHz
    assert peak(mulaw_to_pcm(whole)) == pytest.approx(10000, rel=0.1)
    # Chunks that split samples and the filter's window join up without a seam.
    pieces = PhoneDownsampler(24000)
    assert b"".join(pieces.feed(pcm[i : i + 777]) for i in range(0, len(pcm), 777)) == whole
    # What 8 kHz can't carry is filtered out rather than folded down into the speech.
    assert peak(mulaw_to_pcm(PhoneDownsampler(24000).feed(tone(7000, rate=24000).tobytes()))) < 600
    assert len(PhoneDownsampler(8000).feed(tone(500, rate=8000).tobytes())) == 4000
    with pytest.raises(ValueError):
        PhoneDownsampler(22050)


def test_loudness():
    assert level(b"\xff" * 160) == 0
    assert level(pcm_to_mulaw(tone(400, rate=8000, seconds=0.02, level=8000))) > 5000


# --- A VoiceLab stand-in ------------------------------------------------------


class VoiceLab:
    """VoiceLab's realtime speech endpoints, as docs.voicelab.uz describes them."""

    def __init__(self):
        self.tts_keys: list[str | None] = []
        self.tts_requests: list[dict] = []
        self.tts_errors = 0  # requests to answer with an error
        self.stt_keys: list[str | None] = []
        self.stt_starts: list[dict] = []
        self.stt_phrases: list[bytes] = []
        self.stt_replies: list[dict] = []  # one per commit; then "Salom"
        self.stt_drop = 0
        self.app = FastAPI()
        self.app.websocket("/v1/tts/stream")(self._tts)
        self.app.websocket("/v1/stt/stream")(self._stt)

    async def _tts(self, ws: WebSocket):
        self.tts_keys.append(ws.headers.get("xvl-api-key"))
        await ws.accept()
        await ws.send_json({"event": "ready", "format": "pcm_s16le", "sample_rate": 24000,
                            "channels": 1})  # fmt: skip
        try:
            request = json.loads(await ws.receive_text())
            self.tts_requests.append(request)
            if self.tts_errors:
                self.tts_errors -= 1
                await ws.send_json({"event": "error", "code": "generation_failed",
                                    "message": "try later"})  # fmt: skip
            else:
                seconds = len(request["text"]) / 100
                pcm = tone(440, rate=24000, seconds=seconds).tobytes()
                for i in range(0, len(pcm), 4801):  # odd sizes, as a network may split them
                    await ws.send_bytes(pcm[i : i + 4801])
                await ws.send_json({"event": "done", "duration_ms": round(seconds * 1000)})
            await ws.receive_text()  # one request per connection: wait to be closed
        except WebSocketDisconnect:
            pass  # a spare connection that was never needed ends here too

    async def _stt(self, ws: WebSocket):
        self.stt_keys.append(ws.headers.get("xvl-api-key"))
        await ws.accept()
        if self.stt_drop:
            self.stt_drop -= 1
            await ws.close(1011)
            return
        await ws.send_json({"event": "ready", "audio_format": "pcm_s16le",
                            "sample_rate": 16000, "channels": 1})  # fmt: skip
        started, audio = False, b""
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                return
            if message.get("bytes") is not None:
                if not started:
                    await ws.send_json({"event": "error", "code": "not_started",
                                        "message": "Send a start message before audio."})  # fmt: skip
                audio += message["bytes"]
                continue
            request = json.loads(message["text"])
            if request.get("type") == "start":
                self.stt_starts.append(request)
                started, audio = True, b""
            elif request.get("type") == "commit":
                self.stt_phrases.append(audio)
                started = False
                reply = self.stt_replies.pop(0) if self.stt_replies else final("Salom")
                await ws.send_json(reply)


def final(text: str) -> dict:
    return {"event": "final", "text": text, "language": "uz", "duration_ms": 1000,
            "segments": []}  # fmt: skip


def settings(url: str, **overrides):
    values = {
        "voicelab_url": url, "voicelab_api_key": "vl-key", "tts_provider": "voicelab",
        "stt_provider": "voicelab", "voice_language": "uz",
    }  # fmt: skip
    return voice_settings(**values | overrides)


def synthesize(lab: VoiceLab, *texts: str, language: str | None = None) -> list[bytes]:
    async def run(url):
        tts = make_tts(settings(url))
        assert isinstance(tts, VoiceLabTts)
        try:
            return [
                b"".join([c.audio async for c in tts.speak(text, language=language)])
                for text in texts
            ]
        finally:
            await tts.close()

    with serve(lab.app) as url:
        return asyncio.run(run(url))


# --- Text to speech -----------------------------------------------------------


def test_sentences_are_streamed_one_connection_each():
    lab = VoiceLab()
    first, second = synthesize(lab, "Assalomu alaykum!", "Bir daqiqa.")
    assert len(first) == 1360 and len(second) == 880  # 8 kHz mu-law, 10 ms a character
    assert peak(mulaw_to_pcm(first)) == pytest.approx(10000, rel=0.1)
    assert [r["text"] for r in lab.tts_requests] == ["Assalomu alaykum!", "Bir daqiqa."]
    assert lab.tts_requests[0] == {
        "text": "Assalomu alaykum!", "language": "uz",
        "voice_id": "voice_01J9NEUTRAL0000000000000001", "speed": 1.0,
    }  # fmt: skip
    # The next sentence's connection was opened ahead: two used, one spare closed unused.
    assert lab.tts_keys == ["vl-key"] * 3


def test_each_sentence_is_spoken_in_its_own_language():
    lab = VoiceLab()
    synthesize(lab, "Zdravstvuyte.", language="ru")
    assert lab.tts_requests[0]["language"] == "ru"


def test_a_failed_sentence_is_retried_once_on_a_new_connection():
    lab = VoiceLab()
    lab.tts_errors = 1
    (audio,) = synthesize(lab, "Bir soniya.")
    assert len(audio) == 880
    assert len(lab.tts_requests) == 2


def test_tts_gives_up_after_the_retry_without_leaking_the_key():
    lab = VoiceLab()
    lab.tts_errors = 2
    with pytest.raises(TtsError, match="generation_failed") as failure:
        synthesize(lab, "Bir soniya.")
    assert "vl-key" not in str(failure.value)


def test_unreachable_tts_is_reported():
    async def run():
        tts = make_tts(settings("http://127.0.0.1:9"))
        with pytest.raises(TtsError):
            async for _ in tts.speak("Salom."):
                pass
        await tts.close()

    asyncio.run(run())


# --- Speech to text -----------------------------------------------------------

QUIET = b"\xff" * 160  # 20 ms of silence


def talking(seconds: float) -> bytes:
    return pcm_to_mulaw(tone(300, rate=8000, seconds=seconds, level=6000))


def listen(lab: VoiceLab, audio: bytes, expect: int, language: str | None = None) -> list:
    async def run(url):
        stt = make_stt(settings(url))
        if language:
            stt.set_language(language)
        events = []
        task = asyncio.create_task(stt.run(events.append))
        rest = audio
        async with asyncio.timeout(10):
            while (len(events) < expect or rest) and not task.done():
                frame, rest = (rest[:160], rest[160:]) if rest else (QUIET, b"")
                stt.feed(frame)
                await asyncio.sleep(0.001)
        if task.done():
            task.result()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return events

    with serve(lab.app) as url:
        return asyncio.run(run(url))


def test_a_phrase_is_sent_once_the_caller_pauses():
    lab = VoiceLab()
    lab.stt_replies = [final("Quloqchin bormi?")]
    events = listen(lab, QUIET * 20 + talking(1.0) + QUIET * 40, expect=2)
    # Half a second in, the caller can cut Aida off; the words come once they pause.
    assert events == [Speech("..."), Utterance("Quloqchin bormi?", 1.0)]
    assert lab.stt_keys[0] == "vl-key"
    assert lab.stt_starts == [{"type": "start", "language": "uz", "audio_format": "pcm_s16le",
                               "sample_rate": 16000, "channels": 1}]  # fmt: skip
    # 16 kHz PCM: the talking, a little from before it, and a little of the pause.
    (phrase,) = lab.stt_phrases
    assert 1.0 < len(phrase) / 32000 < 1.6


def test_two_phrases_and_a_change_of_language():
    lab = VoiceLab()
    lab.stt_replies = [final("Ha"), final("Da")]
    phrase = talking(0.3) + QUIET * 40
    events = listen(lab, phrase + phrase, expect=2, language="ru")
    assert events == [Utterance("Ha"), Utterance("Da")]  # too short to cut Aida off
    assert [s["language"] for s in lab.stt_starts] == ["ru", "ru"]


def test_clicks_and_hiss_are_not_sent():
    lab = VoiceLab()
    hiss = pcm_to_mulaw(tone(3000, rate=8000, seconds=1, level=200))
    listen(lab, hiss + talking(0.06) + QUIET * 40, expect=0)
    assert lab.stt_phrases == []


def test_no_speech_is_ignored_and_other_errors_end_the_connection():
    lab = VoiceLab()
    lab.stt_replies = [{"event": "error", "code": "no_speech_detected", "message": "None."}]
    assert listen(lab, talking(0.3) + QUIET * 40, expect=0) == []
    lab = VoiceLab()
    error = {"event": "error", "code": "insufficient_credits", "message": "Top up"}
    lab.stt_replies = [error] * 10
    phrase = talking(0.3) + QUIET * 40
    with pytest.raises(SttUnavailable, match="insufficient_credits"):
        listen(lab, phrase * 30, expect=1)


def test_dropped_stt_connection_is_reopened():
    lab = VoiceLab()
    lab.stt_drop = 1
    events = listen(lab, QUIET * 10 + talking(0.3) + QUIET * 40, expect=1)
    assert events == [Utterance("Salom")]
    assert len(lab.stt_keys) == 2


# --- Settings -----------------------------------------------------------------


def test_voicelab_needs_only_its_own_key():
    ready = settings("https://api.voicelab.uz", elevenlabs_api_key=None,
                     elevenlabs_voice_id=None, deepgram_api_key=None)  # fmt: skip
    assert ready.missing == []
    assert settings("https://x", voicelab_api_key=None).missing == ["VOICELAB_API_KEY"]
    mixed = voice_settings(stt_provider="voicelab", elevenlabs_voice_id=None)
    assert mixed.missing == ["ELEVENLABS_VOICE_ID", "VOICELAB_API_KEY"]


# --- Uzbek --------------------------------------------------------------------


@pytest.mark.parametrize(
    "n, words",
    [
        (0, "nol"), (11, "o'n bir"), (40, "qirq"), (99, "to'qson to'qqiz"), (100, "yuz"),
        (121, "yuz yigirma bir"), (1005, "ming besh"), (2500, "ikki ming besh yuz"),
        (250_000, "ikki yuz ellik ming"), (1_000_001, "bir million bir"),
    ],
)  # fmt: skip
def test_uzbek_number_words(n, words):
    assert uzbek_number_words(n) == words


@pytest.mark.parametrize(
    "text, spoken",
    [
        ("Narxi $49.99.", "Narxi qirq to'qqiz dollar to'qson to'qqiz sent."),
        ("$1 yoki $0.50", "bir dollar yoki ellik sent"),
        ("Buyurtma #12 tayyor", "Buyurtma raqam o'n ikki tayyor"),
        ("2 ta krujka, 10% chegirma", "ikki ta krujka, o'n foiz chegirma"),
        ("4.5 yulduz", "to'rt butun besh yulduz"),
        ("10:30 yoki 9:05 da", "o'n o'ttiz yoki to'qqiz nol besh da"),
        ("+998901234567", "plyus to'qqiz to'qqiz sakkiz to'qqiz nol bir ikki uch to'rt besh olti yetti"),
        ("To'lov: https://pay.example/x", "To'lov: havola"),
        ("**Quloqchin** bor", "Quloqchin bor"),
    ],
)  # fmt: skip
def test_uzbek_speakable(text, spoken):
    assert speakable(text, "uz") == spoken


@pytest.mark.parametrize("text", ["ha", "Ha, mayli.", "xo'p", "xo‘p", "tushunarli", "zoʻr"])
def test_uzbek_backchannels(text):
    assert is_backchannel(text)


@pytest.mark.parametrize("text", ["yo'q", "to'xtang", "ha, lekin qorasi", "nechpul"])
def test_uzbek_turns_are_not_backchannels(text):
    assert not is_backchannel(text)


def test_uzbek_prompt_and_lines():
    english = voice_prompt("Test Store", "Kim")
    uzbek = voice_prompt("Test Store", "Kim", "uz")
    assert uzbek.startswith(english) and "everything you say is in Uzbek" in uzbek
    assert "Uzbek" not in english.split("# Guardrails")[0]
    lines = LINES["uz"]
    assert lines.greeting.format(store="Test Store").startswith("Assalomu alaykum! Test Store")
    assert lines.transfer.format(admin="Kim").startswith("Buyurtma ma'lumotlaringizni Kimga")
    # Every fixed line survives being made speakable.
    for line in (lines.greeting, lines.hiccup, lines.refused, lines.silent, *lines.fillers):
        assert speakable(line, "uz") == line
