"""The Deepgram, ElevenLabs and Claude adapters, against servers speaking their protocols."""

import asyncio
import json

import httpx2
import pytest

from agent.history import Conversation
from agent.llm import Claude, LlmError
from agent.tools import live_definitions
from tests.fakes import FakeClaude, Reply, request_problem, voice_settings
from tests.simulation import SILENCE, Providers, serve, voice_audio
from voice.stt import Speech, SttUnavailable, Utterance, make_stt
from voice.tts import ElevenLabsTts, TtsError

# --- Speech to text -----------------------------------------------------------


def local(providers_url, **overrides):
    return voice_settings(
        deepgram_url=providers_url.replace("http", "ws"),
        elevenlabs_url=providers_url,
        anthropic_base_url=providers_url,
        **overrides,
    )


def transcribe(providers, provider, *phrases, expect):
    """Stream some phrases and collect events until `expect` have arrived."""

    async def run(url):
        stt = make_stt(local(url, stt_provider=provider))
        events = []
        task = asyncio.create_task(stt.run(events.append))
        fed = 0
        async with asyncio.timeout(10):
            while len(events) < expect and not task.done():
                if len(providers.stt_connections) > fed:
                    fed += 1
                    for phrase in phrases:
                        stt.feed(phrase)
                stt.feed(SILENCE)
                await asyncio.sleep(0.01)
        if task.done():
            task.result()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return events

    with serve(providers.app) as url:
        return asyncio.run(run(url))


@pytest.mark.parametrize("provider", ["deepgram", "elevenlabs"])
def test_speech_then_utterance(provider):
    providers = Providers()
    events = transcribe(
        providers,
        provider,
        b"<partial>two</>",
        b"<final 0.8>two mugs</>",
        expect=3 if provider == "deepgram" else 2,
    )
    assert events[0] == Speech("two")
    assert events[-1].text == "two mugs" and events[-1].confidence == pytest.approx(0.8)
    assert isinstance(events[-1], Utterance)


def test_flux_connection():
    providers = Providers()
    transcribe(providers, "deepgram", b"<final 0.9>hi</>", expect=1)
    [(query, headers)] = providers.stt_connections
    assert headers["authorization"] == "Token dg-key"
    assert query["model"] == "flux-general-en"
    assert (query["encoding"], query["sample_rate"]) == ("mulaw", "8000")
    assert query["eot_threshold"] == "0.85"  # the spec's confidence threshold
    assert all(640 <= size < 800 for size in providers.stt_chunks)


def test_scribe_connection():
    providers = Providers()
    transcribe(providers, "elevenlabs", b"<final 0.9>hi</>", expect=1)
    [(query, headers)] = providers.stt_connections
    assert headers["xi-api-key"] == "el-key"
    assert query["model_id"] == "scribe_v2_realtime"
    assert (query["audio_format"], query["commit_strategy"]) == ("ulaw_8000", "vad")
    assert all(size >= 800 for size in providers.stt_chunks)


def test_confidence_may_arrive_as_text():
    stt = make_stt(voice_settings())
    words = [{"word": "a", "confidence": "0.4"}, {"word": "b", "confidence": "0.6"}]
    message = {"type": "TurnInfo", "event": "EndOfTurn", "transcript": "a b", "words": words}
    assert list(stt.parse(message)) == [Utterance("a b", 0.5)]


@pytest.mark.parametrize("provider", ["deepgram", "elevenlabs"])
def test_dropped_connection_is_reopened(provider):
    providers = Providers()
    providers.stt_drop = 1
    events = transcribe(providers, provider, b"<final 0.9>hi</>", expect=1)
    assert len(providers.stt_connections) == 2 and events[-1].text == "hi"


@pytest.mark.parametrize(
    "provider, error",
    [
        ("deepgram", {"type": "Error", "code": "BAD", "description": "no credit"}),
        ("elevenlabs", {"message_type": "quota_exceeded", "error": "no credit"}),
    ],
)
def test_provider_that_stays_down_is_reported(provider, error):
    providers = Providers()
    providers.stt_error = error
    with pytest.raises(SttUnavailable, match="no credit"):
        transcribe(providers, provider, expect=1)
    assert len(providers.stt_connections) == 3


def test_unreachable_provider_is_reported(monkeypatch):
    monkeypatch.setattr("voice.stt.RECONNECTS", 0)
    stt = make_stt(voice_settings(deepgram_url="ws://127.0.0.1:9"))
    with pytest.raises(SttUnavailable):
        asyncio.run(asyncio.wait_for(stt.run(print), 30))


def test_stalled_provider_drops_the_oldest_audio():
    async def run():
        stt = make_stt(voice_settings())
        for i in range(1000):
            stt.feed(i.to_bytes(2, "big"))
        return stt._audio.qsize(), stt._audio.get_nowait()

    size, oldest = asyncio.run(run())
    assert size == 250 and int.from_bytes(oldest, "big") == 750


# --- Text to speech -----------------------------------------------------------

LINE = "We have wireless earbuds and a lamp."


def synthesize(providers, text=LINE, previous=""):
    async def run(url):
        tts = ElevenLabsTts(local(url))
        try:
            return [chunk async for chunk in tts.speak(text, previous)]
        finally:
            await tts.close()

    with serve(providers.app) as url:
        return asyncio.run(run(url))


@pytest.mark.parametrize("framing", ["lines", "joined", "sse"])
@pytest.mark.parametrize("times", ["request", "chunk"])
def test_audio_and_timings(framing, times):
    providers = Providers()
    providers.tts_framing, providers.tts_times = framing, times
    chunks = synthesize(providers)
    assert b"".join(c.audio for c in chunks) == voice_audio(LINE)
    ends = [end for c in chunks for end in c.char_ends_ms]
    assert ends == pytest.approx([(i + 1) * 10 for i in range(len(LINE))])


def test_audio_without_timings():
    providers = Providers()
    providers.tts_times = None
    chunks = synthesize(providers)
    assert b"".join(c.audio for c in chunks) == voice_audio(LINE)
    assert all(c.char_ends_ms == [] for c in chunks)


def test_tts_request():
    providers = Providers()
    synthesize(providers, "And a lamp.", "We have mugs.")
    [(body, query)] = providers.tts_requests
    assert query == {"output_format": "ulaw_8000"}
    assert body == {
        "text": "And a lamp.", "model_id": "eleven_flash_v2_5", "previous_text": "We have mugs."
    }


def test_tts_is_retried_once():
    providers = Providers()
    providers.tts_failures = 1
    assert b"".join(c.audio for c in synthesize(providers)) == voice_audio(LINE)


def test_tts_gives_up_after_the_retry_without_leaking_the_key():
    providers = Providers()
    providers.tts_failures = 2
    with pytest.raises(TtsError, match="HTTP 503") as failure:
        synthesize(providers)
    assert "el-key" not in str(failure.value)


def test_tts_is_not_retried_once_audio_was_heard():
    requests = []

    async def half(request):
        requests.append(request)

        async def body():
            yield json.dumps({"audio_base64": "AAAA"}).encode()
            raise httpx2.ReadError("connection lost")

        return httpx2.Response(200, content=body())

    async def run():
        tts = ElevenLabsTts(voice_settings(), httpx2.MockTransport(half))
        async for _ in tts.speak("Hello"):
            pass

    with pytest.raises(TtsError):
        asyncio.run(run())
    assert len(requests) == 1


# --- Claude -------------------------------------------------------------------


def ask(claude, **settings):
    async def run():
        llm = Claude(
            voice_settings(**settings),
            httpx2.AsyncClient(transport=httpx2.MockTransport(claude)),
        )
        conversation = Conversation()
        conversation.add_customer("Hi")
        heard = []
        system = [{"type": "text", "text": "You are Aida."}]
        reply = await llm.reply(system, live_definitions(), conversation, heard.append)
        await llm.close()
        return reply, heard

    return asyncio.run(run())


def test_reply_streams_text_and_keeps_every_block():
    reply, heard = ask(FakeClaude(Reply("Hello there. How are you?")))
    assert len(heard) > 1 and "".join(heard) == "Hello there. How are you?"
    assert [b["type"] for b in reply.content] == ["thinking", "text"]
    assert reply.content[0]["signature"] and reply.stop_reason == "end_turn"


def test_claude_request():
    claude = FakeClaude()
    ask(claude)
    [request] = claude.requests
    assert request["model"] == "claude-opus-5-5"
    assert request["output_config"] == {"effort": "low"}
    assert request["stream"] is True
    assert request["cache_control"] == {"type": "ephemeral"}
    assert "thinking" not in request and "tool_choice" not in request
    assert not any("eager_input_streaming" in tool for tool in request["tools"])


@pytest.mark.parametrize(
    "settings, fallbacks, speed",
    [
        ({}, "default", None),
        ({"aida_model": "claude-sonnet-5-5"}, "default", None),
        ({"aida_model": "claude-haiku-5-5"}, None, None),
        ({"aida_fast_mode": True}, None, "fast"),
    ],
)
def test_model_options(settings, fallbacks, speed):
    claude = FakeClaude()
    ask(claude, **settings)
    [request], [headers] = claude.requests, claude.headers
    assert request.get("fallbacks") == fallbacks and request.get("speed") == speed
    beta = headers.get("anthropic-beta", "")
    assert ("server-side-fallback-2026-07-01" in beta) == bool(fallbacks)


@pytest.mark.parametrize("status", [401, 400, 529])
def test_claude_errors_become_one_kind(status):
    with pytest.raises(LlmError):
        ask(FakeClaude(status, status))


def test_claude_connection_lost_mid_stream():
    async def half(request):
        async def body():
            yield b'event: message_start\ndata: {"type": "message_start", "message": {"id": "m"'
            raise httpx2.ReadError("connection lost")

        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, content=body()
        )

    with pytest.raises(LlmError):
        ask(half)


def test_failed_warmup_is_harmless():
    async def refuse(request):
        return httpx2.Response(500, json={"error": {"message": "no"}})

    async def run():
        llm = Claude(
            voice_settings(), httpx2.AsyncClient(transport=httpx2.MockTransport(refuse))
        )
        await llm.warm([{"type": "text", "text": "x"}], live_definitions())

    asyncio.run(run())


def test_the_fake_claude_is_strict():
    """The session tests lean on these checks, so make sure they bite."""
    base = {"system": [], "tools": []}
    user = {"role": "user", "content": [{"type": "text", "text": "hi"}]}
    call = {"type": "tool_use", "id": "t", "name": "x", "input": {}}
    think = {"type": "thinking", "thinking": "", "signature": "wrong"}
    for messages in (
        [user, user],
        [user, {"role": "assistant", "content": [{"type": "text", "text": "x"}]}],
        [user, {"role": "assistant", "content": [call]}, user],
        [user, {"role": "assistant", "content": [{"type": "text", "text": " "}]}, user],
        [user, {"role": "assistant", "content": [think]}, user],
        [user, {"role": "assistant", "content": []}, user],
    ):
        assert request_problem({**base, "messages": messages}), messages
    assert request_problem({**base, "messages": [user]}) is None
