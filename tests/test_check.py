"""check.py: one small request per service, and a plain answer about what is wrong."""

import json

import httpx2
import pytest

from app.config import Settings
from check import Result, check_claude, check_store, check_telegram, check_voicelab, lines
from tests.fakes import voice_settings
from tests.simulation import serve
from tests.test_voicelab import VoiceLab
from tests.test_voicelab import settings as voicelab_settings


def claude_answering(status: int, error: str = "", message: str = "") -> httpx2.Client:
    def answer(request: httpx2.Request) -> httpx2.Response:
        assert json.loads(request.content)["max_tokens"] == 16
        if status == 200:
            return httpx2.Response(200, json={
                "id": "m", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
                "content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn",
                "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1},
            })  # fmt: skip
        return httpx2.Response(
            status, json={"type": "error", "error": {"type": error, "message": message}}
        )

    return httpx2.Client(transport=httpx2.MockTransport(answer))


@pytest.mark.parametrize(
    "status, error, message, says",
    [
        (200, "", "", "answers, with claude-opus-5-5"),
        (401, "authentication_error", "invalid x-api-key", "was rejected"),
        (400, "invalid_request_error", "Your credit balance is too low", "no credit left"),
        (404, "not_found_error", "model: nope", "There is no model called"),
        (429, "rate_limit_error", "slow down", "Too many requests"),
        (529, "overloaded_error", "busy", "HTTP 529"),
    ],
)
def test_claude(status, error, message, says):
    result = check_claude(voice_settings(), claude_answering(status, error, message))
    assert result.ok is (status == 200) and says in result.detail


def test_claude_without_a_key():
    result = check_claude(voice_settings(anthropic_api_key=None))
    assert result.ok is False and "ANTHROPIC_API_KEY is empty" in result.detail


def store_settings(**overrides) -> Settings:
    values = {"telegram_bot_token": "111:SECRET", "admin_chat_id": "555"} | overrides
    return Settings(_env_file=None, **values)


def telegram(me: int = 200, chat: int = 200) -> httpx2.MockTransport:
    def answer(request: httpx2.Request) -> httpx2.Response:
        assert "sendMessage" not in request.url.path  # nobody is messaged
        if request.url.path.endswith("/getMe"):
            return httpx2.Response(me, json={"ok": me == 200, "result": {"username": "aida_bot"}}
                                   if me == 200 else {"ok": False})  # fmt: skip
        assert request.url.params["chat_id"] == "555"
        return httpx2.Response(chat, json={"ok": chat == 200, "result": {"id": 555}})

    return httpx2.MockTransport(answer)


def test_telegram():
    assert check_telegram(store_settings(), telegram()) == Result(
        "Telegram", True, "the bot @aida_bot works and can reach the admin chat"
    )
    assert "was rejected" in check_telegram(store_settings(), telegram(me=401)).detail
    assert "can't see the admin chat 555" in check_telegram(store_settings(), telegram(chat=400)).detail
    no_admin = check_telegram(store_settings(admin_chat_id=None), telegram())
    assert no_admin.ok is False and "ADMIN_CHAT_ID is empty" in no_admin.detail
    assert "SECRET" not in str(check_telegram(store_settings(), telegram(me=401)))


def test_store():
    def answer(request: httpx2.Request) -> httpx2.Response:
        ok = request.headers.get("X-API-Key") == "right"
        return httpx2.Response(200 if ok else 401, json={})

    transport = httpx2.MockTransport(answer)
    assert check_store(voice_settings(agent_api_key="right"), transport).ok
    assert "different AGENT_API_KEY" in check_store(voice_settings(agent_api_key="x"), transport).detail
    down = check_store(voice_settings(backend_url="http://127.0.0.1:9"))
    assert down.ok is False and "Nothing answers" in down.detail


def test_voicelab():
    lab = VoiceLab()
    with serve(lab.app) as url:
        result = check_voicelab(voicelab_settings(url))
    assert result.ok and result.detail == 'speaks, and heard its own "Salom." as "Salom"'
    assert [r["text"] for r in lab.tts_requests] == ["Salom."]
    assert lab.stt_keys == ["vl-key"] and lab.stt_starts[0]["language"] == "uz"
    assert len(lab.stt_phrases[0]) == 1920  # the 60 ms "Salom." as 16 kHz, 2 bytes a sample


def test_voicelab_problems():
    assert check_voicelab(voice_settings()).ok is None  # not used
    empty = check_voicelab(voicelab_settings("http://x", voicelab_api_key=None))
    assert "VOICELAB_API_KEY is empty" in empty.detail
    lab = VoiceLab()
    lab.tts_errors = 2
    with serve(lab.app) as url:
        failed = check_voicelab(voicelab_settings(url))
    assert failed.ok is False and "vl-key" not in failed.detail


def test_what_can_run():
    works = {name: Result(name, True, "") for name in ("Claude", "Telegram", "Store")}
    assert lines(works, voice_settings()) == [
        "Telegram chat: ready", "Phone over Bluetooth: ready", "Phone over Twilio: ready",
    ]  # fmt: skip
    voicelab = voicelab_settings("http://x", voicelab_api_key=None)
    assert lines(works, voicelab) == [
        "Telegram chat: ready", "Phone over Bluetooth: not ready", "Phone over Twilio: not ready",
        "The phone line still needs: VOICELAB_API_KEY",
    ]  # fmt: skip
