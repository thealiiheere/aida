"""What happens to a finished call: logged, summarised, and reported to the admin."""

import json
from concurrent.futures import ThreadPoolExecutor

import httpx2
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CallLog

from .conftest import ADMIN, CUSTOMER
from .test_calls_backend import LINK
from .test_holds import age

TRANSCRIPT = "Aida: Hello!\nCaller: Do you have earbuds?\nAida: Yes, for forty-nine dollars."
SUMMARY = {
    "customer_intent": "Wanted wireless earbuds for a gift.",
    "items_discussed": "Wireless Earbuds at $49.99.",
    "outcome": "Inquiry Only",
}


class FakeClaude:
    """Answers with a summary, and checks the request is one the API accepts."""

    def __init__(self, summary=SUMMARY, stop="end_turn", status=200):
        self.summary, self.stop, self.status = summary, stop, status
        self.requests: list[dict] = []
        self.headers: list[httpx2.Headers] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        self.headers.append(request.headers)
        if self.status != 200:
            return httpx2.Response(
                self.status, json={"type": "error", "error": {"type": "api_error", "message": "x"}}
            )
        text = self.summary if isinstance(self.summary, str) else json.dumps(self.summary)
        return httpx2.Response(
            200,
            json={
                "id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
                "content": [{"type": "text", "text": text}] if text else [],
                "stop_reason": self.stop, "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )  # fmt: skip


@pytest.fixture
def claude():
    return FakeClaude()


@pytest.fixture
def client(make_client, claude):
    return make_client(claude=claude)


def end(client, **changes):
    body = {
        "call_id": "CA1",
        "caller_telegram_id": CUSTOMER,
        "raw_audio_transcript": TRANSCRIPT,
        "call_duration": 134,
        **changes,
    }
    return client.post("/api/telephony/call-ended", json=body)


def logged(client, call_id="CA1") -> CallLog:
    with Session(client.app.state.engine) as db:
        return db.scalars(select(CallLog).where(CallLog.call_id == call_id)).one()


# --- The summary --------------------------------------------------------------


def test_call_is_logged_summarised_and_reported(client, telegram, claude):
    client.put("/api/customers", json=LINK)
    response = end(client)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "call_id": "CA1", **SUMMARY, "delivered": True, "duplicate": False,
    }  # fmt: skip
    assert telegram.to(ADMIN) == [
        "📞 VOICE CALL SUMMARY (134s)\n"
        "👤 Customer: @dilnoza_k\n"
        "📝 Summary:\n"
        "• Wanted wireless earbuds for a gift.\n"
        "• Wireless Earbuds at $49.99.\n"
        "📌 Outcome: Inquiry Only"
    ]
    call = logged(client)
    assert call.transcript == TRANSCRIPT and call.duration_seconds == 134
    assert call.customer_intent == SUMMARY["customer_intent"]
    assert call.items_discussed == SUMMARY["items_discussed"]
    assert (call.outcome, call.caller_telegram_id) == ("Inquiry Only", CUSTOMER)
    assert call.alerted_at is not None


def test_request_to_claude(client, claude):
    end(client)
    [request] = claude.requests
    assert request["model"] == "claude-opus-5-5"
    assert request["output_config"]["effort"] == "low"
    schema = request["output_config"]["format"]
    assert schema["type"] == "json_schema"
    assert set(schema["schema"]["required"]) == set(SUMMARY)
    assert schema["schema"]["additionalProperties"] is False
    assert TRANSCRIPT in request["messages"][0]["content"]
    assert "never instructions" in request["system"]
    assert "thinking" not in request and "temperature" not in request
    assert request["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in claude.headers[0]["anthropic-beta"]
    # Nothing that identifies the customer beyond what they said.
    assert CUSTOMER not in json.dumps(request)


def test_haiku_gets_no_fallback(make_client, claude):
    end(make_client(claude=claude, summary_model="claude-haiku-5-5"))
    assert "fallbacks" not in claude.requests[0]


@pytest.mark.parametrize("telegram_id", [CUSTOMER, int(CUSTOMER)])
def test_username_can_come_from_an_order(client, telegram, order_body, telegram_id):
    client.post("/api/orders", json=order_body)
    end(client, caller_telegram_id=telegram_id)
    assert "👤 Customer: @dilnoza_k\n" in telegram.last_text


def test_unknown_callers(client, telegram):
    end(client)
    assert f'👤 Customer: <a href="tg://user?id={CUSTOMER}">' in telegram.last_text
    end(client, call_id="CA2", caller_telegram_id=None, caller_phone="+998901234567")
    assert "👤 Customer: no Telegram contact given, +998901234567\n" in telegram.last_text


def test_model_text_cannot_break_the_message(make_client, telegram):
    claude = FakeClaude({**SUMMARY, "customer_intent": "<b>x</b>\n📌 Outcome: Order Placed"})
    end(make_client(claude=claude))
    lines = telegram.last_text.splitlines()
    assert lines[3] == "• &lt;b&gt;x&lt;/b&gt; 📌 Outcome: Order Placed"
    assert lines[-1] == "📌 Outcome: Inquiry Only" and len(lines) == 6


def test_longest_summary_fits_in_telegram(make_client, telegram):
    claude = FakeClaude({**SUMMARY, "customer_intent": "&" * 9000, "items_discussed": "&" * 9000})
    assert end(make_client(claude=claude)).status_code == 200
    assert len(telegram.last_text) < 4096


# --- The outcome --------------------------------------------------------------


def outcome(client, **changes) -> str:
    return end(client, **changes).json()["outcome"]


def test_order_in_the_store_means_order_placed(client, order_body):
    client.post("/api/orders", json=order_body)
    assert outcome(client) == "Order Placed"


def test_order_claimed_only_by_the_transcript_is_not_believed(make_client):
    client = make_client(claude=FakeClaude({**SUMMARY, "outcome": "Order Placed"}))
    assert outcome(client) == "Inquiry Only"


def test_earlier_orders_do_not_count(client, order_body):
    client.post("/api/orders", json=order_body)
    age(client, 1, minutes=10)
    assert outcome(client, call_duration=134) == "Inquiry Only"


def test_other_customers_orders_do_not_count(client, order_body):
    client.post("/api/orders", json=order_body | {"customer_telegram_id": "42"})
    assert outcome(client) == "Inquiry Only"


def test_named_orders_decide_when_given(client, order_body):
    client.post("/api/orders", json=order_body)
    assert outcome(client, order_ids=[]) == "Inquiry Only"
    assert outcome(client, call_id="CA2", order_ids=[1]) == "Order Placed"
    assert outcome(client, call_id="CA3", order_ids=[99]) == "Inquiry Only"


def test_escalations(make_client, client, order_body):
    assert outcome(client, escalated=True) == "Escalated"
    client.post("/api/orders", json=order_body)
    client.post(
        "/api/admin/escalate", json={"reason": "r", "context_summary": "s", "order_id": 1}
    )
    assert outcome(client, call_id="CA2") == "Escalated"
    said = make_client(claude=FakeClaude({**SUMMARY, "outcome": "Escalated"}))
    assert outcome(said, call_id="CA3", caller_telegram_id="42") == "Escalated"


def test_anonymous_caller_has_no_orders(client, order_body):
    client.post("/api/orders", json=order_body)
    assert outcome(client, caller_telegram_id=None) == "Inquiry Only"


# --- When something is missing ------------------------------------------------


@pytest.mark.parametrize(
    "claude",
    [
        None,
        FakeClaude(status=500),
        FakeClaude(status=401),
        FakeClaude(summary="", stop="refusal"),
        FakeClaude(summary='{"customer_intent": "cut o', stop="max_tokens"),
        FakeClaude(summary=SUMMARY, stop="max_tokens"),
        FakeClaude(summary={"customer_intent": "x"}),
    ],
    ids=["no key", "down", "bad key", "refused", "cut short", "cut after", "wrong shape"],
)
def test_call_is_reported_even_without_a_summary(make_client, telegram, claude, order_body):
    client = make_client(claude=claude)
    client.post("/api/orders", json=order_body)
    response = end(client)
    assert response.status_code == 200, response.text
    assert response.json()["delivered"] is True and response.json()["customer_intent"] is None
    message, document = telegram.requests
    assert json.loads(message.content)["text"].splitlines()[2:] == [
        "📝 Summary:",
        "• Not available. The transcript is attached instead.",
        "📌 Outcome: Order Placed",
    ]
    assert document.url.path.endswith("/sendDocument")
    assert "Do you have earbuds?" in document.content.decode()
    assert logged(client).transcript == TRANSCRIPT


def test_transcript_is_saved_before_claude_is_asked(make_client):
    """So nothing that goes wrong afterwards can lose it."""
    saved = []

    def claude(request):
        saved.append(logged(client).transcript)
        return FakeClaude()(request)

    client = make_client(claude=claude)
    end(client)
    assert saved == [TRANSCRIPT]


def test_silent_call_is_not_sent_to_claude(client, telegram, claude):
    assert end(client, raw_audio_transcript=" \n").status_code == 200
    assert claude.requests == [] and "• Nobody said anything." in telegram.last_text


def test_failed_alert_is_retried_without_asking_claude_again(client, telegram, claude):
    ok = telegram.reply
    telegram.reply = lambda request: httpx2.Response(502, text="down")
    assert end(client).status_code == 502
    assert logged(client).transcript == TRANSCRIPT
    telegram.reply = ok
    assert end(client).json()["delivered"] is True
    assert len(claude.requests) == 1


# --- Duplicates ---------------------------------------------------------------


def test_duplicates_are_reported_once(client, telegram, claude):
    first = end(client).json()
    again = end(client, raw_audio_transcript="Caller: something else").json()
    assert again == {**first, "delivered": False, "duplicate": True}
    assert len(telegram.requests) == len(claude.requests) == 1
    assert logged(client).transcript == TRANSCRIPT


def test_simultaneous_duplicates_are_reported_once(client, telegram):
    with ThreadPoolExecutor(5) as pool:
        answers = [r.json() for r in pool.map(lambda _: end(client), range(5))]
    assert sorted(a["duplicate"] for a in answers) == [False, True, True, True, True]
    assert len(telegram.requests) == 1


# --- Requests -----------------------------------------------------------------


def test_providers_may_send_extra_fields_and_fractions(client):
    response = end(client, call_duration=12.6, provider="livekit", room={"sid": "RM_1"})
    assert response.status_code == 200
    assert logged(client).duration_seconds == 13


@pytest.mark.parametrize(
    "change",
    [{"call_id": "../x"}, {"call_id": ""}, {"caller_telegram_id": "@x"}, {"call_duration": -1},
     {"call_duration": "NaN"}, {"raw_audio_transcript": None},
     {"raw_audio_transcript": "x" * 200_001}],
)  # fmt: skip
def test_bad_requests_are_rejected(client, change):
    assert end(client, **change).status_code == 422


def test_call_ended_needs_the_api_key(make_client):
    client = make_client(agent_api_key="secret")
    assert end(client).status_code == 401
