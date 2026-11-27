"""Twilio's webhooks, and who may open the audio stream."""

import json
import time
from xml.etree import ElementTree

import httpx2
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tests.fakes import voice_settings
from voice.backend import StoreBackend, caller_phone
from voice.server import MAX_REMEMBERED, PENDING_SECONDS, create_app, signature

URL = "https://voice.example"
FORM = {"CallSid": "CA1", "From": "+998901234567", "To": "+15550001111"}


@pytest.fixture
def make_line():
    clients = []

    def make(**settings):
        # Nothing listens on port 9, so the store backend is unreachable here.
        app = create_app(voice_settings(backend_url="http://127.0.0.1:9", **settings))
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def line(make_line, monkeypatch):
    async def nobody(self, phone):
        return None

    monkeypatch.setattr(StoreBackend, "find_customer", nobody)
    return make_line()


def post(line, path, form=FORM, token="twilio-token", url=URL):
    headers = {"X-Twilio-Signature": signature(token, url + path, list(form.items()))}
    return line.post(path, data=form, headers=headers)


def answer(line, **form):
    """Answer a call. Returns the stream's token."""
    response = post(line, "/voice/incoming", {**FORM, **form})
    assert response.status_code == 200, response.text
    return ElementTree.fromstring(response.text).find("Connect/Stream/Parameter").get("value")


def start(token, sid="CA1", **changes):
    body = {
        "streamSid": "MZ1",
        "callSid": sid,
        "customParameters": {"token": token},
        "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
        **changes,
    }
    return {"event": "start", "streamSid": "MZ1", "start": body}


def refused(line, message) -> bool:
    with line.websocket_connect("/voice/stream") as ws:
        ws.send_json({"event": "connected"})
        ws.send_json(message)
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    return closed.value.code == 1008


# --- Signatures ---------------------------------------------------------------


def test_signature_matches_twilios_published_example():
    # https://www.twilio.com/docs/usage/security#validating-requests
    form = {
        "CallSid": "CA1234567890ABCDE",
        "Caller": "+12349013030",
        "Digits": "1234",
        "From": "+12349013030",
        "To": "+18005551212",
    }
    url = "https://mycompany.com/myapp.php?foo=1&bar=2"
    assert signature("12345", url, list(form.items())) == "0/KCTR6DLpKmkAf8muzZqo1nDgQ="


@pytest.mark.parametrize("path", ["/voice/incoming", "/voice/after"])
def test_unsigned_and_wrongly_signed_requests_are_refused(line, path):
    assert line.post(path, data=FORM).status_code == 403
    assert post(line, path, token="other").status_code == 403
    assert post(line, path, url="https://elsewhere.example").status_code == 403
    signed = signature("twilio-token", URL + path, list(FORM.items()))
    tampered = {**FORM, "From": "+998900000000"}
    headers = {"X-Twilio-Signature": signed}
    assert line.post(path, data=tampered, headers=headers).status_code == 403
    assert line.post(path, data=FORM, headers=headers).status_code == 200


@pytest.mark.parametrize("missing", ["twilio_auth_token", "voice_public_url"])
def test_webhooks_stay_closed_without_a_way_to_check_signatures(make_line, missing):
    line = make_line(**{missing: None})
    assert post(line, "/voice/incoming").status_code == 503


# --- Answering ----------------------------------------------------------------


def test_answering_opens_the_stream(line):
    response = post(line, "/voice/incoming")
    assert response.headers["content-type"].startswith("text/xml")
    connect = ElementTree.fromstring(response.text).find("Connect")
    assert connect.get("action") == "https://voice.example/voice/after"
    assert connect.find("Stream").get("url") == "wss://voice.example/voice/stream"
    # Who is calling stays on this server; only a one-time token goes through Twilio.
    assert [p.get("name") for p in connect.findall("Stream/Parameter")] == ["token"]
    assert "998901234567" not in response.text


@pytest.mark.parametrize(
    "missing", ["elevenlabs_api_key", "elevenlabs_voice_id", "deepgram_api_key"]
)
def test_unconfigured_line_apologises(make_line, missing):
    line = make_line(**{missing: None})
    assert line.get("/health").json() == {"ready": False, "active_calls": 0}
    response = post(line, "/voice/incoming")
    assert "isn't available right now" in response.text and "Connect" not in response.text


def test_missing_claude_credentials_are_noticed(make_line, monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    line = make_line(anthropic_api_key=None)
    if line.app.state.llm.has_credentials:
        pytest.skip("this machine has other Anthropic credentials")
    assert line.get("/health").json()["ready"] is False


def test_unreachable_backend_does_not_stop_the_call(make_line):
    line = make_line()
    assert answer(line)
    assert line.app.state.expected["CA1"].caller.phone == "+998901234567"


def test_caller_is_looked_up(make_line, monkeypatch):
    async def found(self, phone):
        assert phone == "+998901234567"
        return {"phone": phone, "telegram_id": "42", "telegram_username": "d_k", "name": "Dil"}

    monkeypatch.setattr(StoreBackend, "find_customer", found)
    line = make_line()
    answer(line)
    caller = line.app.state.expected["CA1"].caller
    assert (caller.telegram_id, caller.telegram_username, caller.name) == ("42", "d_k", "Dil")


@pytest.mark.parametrize("number", ["", "anonymous", "+266696687", "client:bob", "+12"])
def test_hidden_numbers_identify_nobody(line, number):
    assert caller_phone(number) is None
    answer(line, From=number)
    assert line.app.state.expected["CA1"].caller.phone is None


# --- Too many calls -----------------------------------------------------------


BUSY = '<Reject reason="busy"/>'


def test_sixth_call_in_a_minute_is_turned_away(line):
    for i in range(5):
        answer(line, CallSid=f"CA{i}")
    response = post(line, "/voice/incoming", {**FORM, "CallSid": "CA6"})
    assert BUSY in response.text and "Connect" not in response.text
    assert "CA6" not in line.app.state.expected
    answer(line, CallSid="CA7", From="+998907654321")  # someone else gets through


def test_calls_are_counted_per_telegram_account(make_line, monkeypatch):
    async def found(self, phone):
        return {"phone": phone, "telegram_id": "42"}

    monkeypatch.setattr(StoreBackend, "find_customer", found)
    line = make_line()
    for i in range(5):
        answer(line, CallSid=f"CA{i}", From=f"+99890000000{i}")
    form = {**FORM, "CallSid": "CA6", "From": "+998900000009"}
    assert BUSY in post(line, "/voice/incoming", form).text


def test_hidden_numbers_are_not_lumped_together(line):
    for i in range(8):
        answer(line, CallSid=f"CA{i}", From="+266696687")


# --- Opening the stream -------------------------------------------------------


def test_stream_needs_a_call_we_answered(line):
    assert refused(line, start("made-up"))


def test_stream_needs_that_calls_token(line):
    answer(line)
    assert refused(line, start("made-up"))


def test_token_only_fits_its_own_call(line):
    token = answer(line)
    answer(line, CallSid="CA2")
    assert refused(line, start(token, sid="CA2"))


def test_a_wrong_token_uses_up_the_call(line):
    token = answer(line)
    assert refused(line, start("guess"))
    assert refused(line, start(token))


def test_tokens_expire(line, monkeypatch):
    token = answer(line)
    later = time.monotonic() + PENDING_SECONDS + 1
    monkeypatch.setattr("voice.server.time.monotonic", lambda: later)
    assert refused(line, start(token))


def test_expired_calls_are_forgotten(line, monkeypatch):
    answer(line)
    later = time.monotonic() + PENDING_SECONDS + 1
    monkeypatch.setattr("voice.server.time.monotonic", lambda: later)
    answer(line, CallSid="CA2")
    assert list(line.app.state.expected) == ["CA2"]


@pytest.mark.parametrize(
    "message",
    [
        {"event": "start"},
        {"event": "start", "start": None},
        {"event": "start", "start": {"callSid": "CA1"}},
        {"event": "start", "start": {"callSid": ["x"]}},
        start("x", customParameters=None),
        start("x", mediaFormat={}),
    ],
)
def test_malformed_starts_are_refused(line, message):
    answer(line)
    assert refused(line, message)


def test_other_audio_formats_are_refused(line):
    token = answer(line)
    audio = {"encoding": "audio/x-l16", "sampleRate": 16000, "channels": 1}
    assert refused(line, start(token, mediaFormat=audio))


def test_stream_that_never_starts_is_closed(line, monkeypatch):
    monkeypatch.setattr("voice.server.START_TIMEOUT_SECONDS", 0.05)
    with line.websocket_connect("/voice/stream") as ws:
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


# --- After Aida's part --------------------------------------------------------


@pytest.mark.parametrize(
    "outcome, admin, expected, unexpected",
    [
        ("escalated", "+998900000000", "<Dial>+998900000000</Dial>", "technical"),
        ("failed", "+998900000000", "<Dial>+998900000000</Dial>", "<Hangup"),
        ("failed", None, "technical problem", "<Dial>"),
        ("escalated", None, "<Hangup/>", "<Dial>"),
        ("completed", "+998900000000", "<Hangup/>", "<Dial>"),
        ("silent", "+998900000000", "<Hangup/>", "<Dial>"),
        (None, "+998900000000", "<Hangup/>", "<Dial>"),
    ],
)
def test_after_the_call(make_line, outcome, admin, expected, unexpected):
    line = make_line(admin_phone=admin)
    if outcome:
        line.app.state.outcomes["CA1"] = outcome
    response = post(line, "/voice/after")
    assert expected in response.text and unexpected not in response.text
    assert "CA1" not in line.app.state.outcomes
    assert MAX_REMEMBERED == 1000


# --- Reporting ----------------------------------------------------------------


def report(*statuses, send="report_call"):
    import asyncio

    asked = []

    def backend(request):
        asked.append(request)
        status = statuses[len(asked) - 1]
        if status is None:
            raise httpx2.ConnectError("refused")
        return httpx2.Response(status, json={}, headers={"Retry-After": "37"})

    async def run():
        store = StoreBackend(
            voice_settings(agent_api_key="secret"), httpx2.MockTransport(backend)
        )
        delivered = await getattr(store, send)({"call_id": "CA1"})
        await store.close()
        return delivered

    return asyncio.run(run()), asked


@pytest.fixture
def no_waiting(monkeypatch):
    waits = []

    async def skip(seconds):
        waits.append(seconds)

    monkeypatch.setattr("voice.backend.asyncio.sleep", skip)
    return waits


def test_summary_request_goes_to_the_post_call_engine():
    delivered, [request] = report(200, send="call_ended")
    assert delivered and request.url.path == "/api/telephony/call-ended"
    assert json.loads(request.content) == {"call_id": "CA1"}


def test_a_busy_backend_is_given_the_time_it_asks_for(no_waiting):
    delivered, asked = report(429, 200, send="call_ended")
    assert delivered and len(asked) == 2 and no_waiting == [37]


def test_report_carries_the_api_key():
    delivered, [request] = report(200)
    assert delivered and request.headers["X-API-Key"] == "secret"
    assert request.url.path == "/api/admin/notify-order"


def test_report_is_retried_until_delivered(no_waiting):
    delivered, asked = report(502, None, 200)
    assert delivered and len(asked) == 3


def test_report_gives_up(no_waiting):
    delivered, asked = report(502, 502, 502, 200)
    assert not delivered and len(asked) == 3


def test_rejected_report_is_not_retried(no_waiting):
    delivered, asked = report(422, 200)
    assert not delivered and len(asked) == 1
