"""What the store backend does for phone calls."""

import json

import httpx2
import pytest

from tests.conftest import TOKEN, invoice

LINK = {
    "phone": "+998 90 123-45-67",
    "telegram_id": "123456789",
    "telegram_username": "@dilnoza_k",
    "name": "Dilnoza Karimova",
}


def report(**changes) -> dict:
    return {
        "call": {
            "call_id": "CA123",
            "duration_seconds": 134,
            "outcome": "completed",
            "order_ids": [],
            "customer_phone": "+998901234567",
            "transcript": [
                {"speaker": "aida", "text": "Hello!"},
                {"speaker": "caller", "text": "Hi, any <b>mugs</b>?"},
            ],
            **changes,
        }
    }


# --- Who is calling -----------------------------------------------------------


def test_phone_is_linked_and_found(client):
    expected = {**LINK, "phone": "+998901234567", "telegram_username": "dilnoza_k"}
    assert client.put("/api/customers", json=LINK).json() == expected
    found = client.post("/api/customers/lookup", json={"phone": "+998901234567"})
    assert found.json() == expected


def test_linking_again_replaces_the_account(client):
    client.put("/api/customers", json=LINK)
    client.put("/api/customers", json={"phone": LINK["phone"], "telegram_id": "42"})
    found = client.post("/api/customers/lookup", json={"phone": "+998901234567"}).json()
    assert (found["telegram_id"], found["telegram_username"], found["name"]) == ("42", None, None)


def test_unknown_phone(client):
    response = client.post("/api/customers/lookup", json={"phone": "+998900000000"})
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "customer_not_found"


@pytest.mark.parametrize("phone", ["998901234567", "+12", "anonymous", "+99890123456789012"])
def test_bad_phones_are_rejected(client, phone):
    assert client.put("/api/customers", json={**LINK, "phone": phone}).status_code == 422


def test_customers_need_the_api_key(make_client):
    client = make_client(agent_api_key="secret")
    assert client.put("/api/customers", json=LINK).status_code == 401
    assert client.post("/api/customers/lookup", json={"phone": LINK["phone"]}).status_code == 401


# --- Getting the payment link to the customer ---------------------------------


def create(client, order, **extra):
    body = {"order_id": order["order_id"], "provider": "click", **extra}
    return client.post("/api/payments/create-invoice", json=body)


def test_link_is_sent_to_the_customer_on_request(client, telegram, order):
    made = create(client, order, send_to_customer=True).json()
    assert made["sent_to_customer"] is True and made["delivery_note"] is None
    [message] = telegram.payloads
    assert message["chat_id"] == "123456789"
    assert made["payment_url"] in message["text"] and "Test Store" in message["text"]


def test_link_is_not_sent_unless_asked(client, telegram, order):
    assert create(client, order).json()["sent_to_customer"] is False
    assert telegram.requests == []


def test_undeliverable_link_still_creates_the_invoice(client, telegram, order):
    telegram.reply = lambda request: httpx2.Response(
        403, json={"ok": False, "description": "Forbidden: bot can't initiate conversation"}
    )
    response = create(client, order, send_to_customer=True)
    made = response.json()
    assert response.status_code == 200 and made["payment_url"]
    assert made["sent_to_customer"] is False
    assert "can't initiate" in made["delivery_note"] and TOKEN not in response.text


def test_link_is_not_claimed_as_sent_without_a_bot(make_client, order_body):
    client = make_client(telegram_bot_token=None)
    order = client.post("/api/orders", json=order_body).json()
    made = create(client, order, send_to_customer=True).json()
    assert made["sent_to_customer"] is False and "not set up" in made["delivery_note"]


# --- The report after a call --------------------------------------------------


def test_call_without_an_order(client, telegram):
    response = client.post("/api/admin/notify-order", json=report())
    assert response.status_code == 200, response.text
    assert response.json()["delivered"] is True
    assert telegram.last_text == (
        "📞 CALL ENDED (2 min 14 s)\n"
        "👤 Customer: no Telegram contact given, +998901234567\n"
        "🛍️ No order was placed.\n"
        "📝 Transcript:\n"
        "<blockquote expandable>Aida: Hello!\nCaller: Hi, any &lt;b&gt;mugs&lt;/b&gt;?</blockquote>"
    )


def test_call_with_an_order(client, telegram, order):
    link = invoice(client, order["order_id"])["payment_url"]
    body = report(order_ids=[order["order_id"]], customer_telegram_username="dilnoza_k")
    client.post("/api/admin/notify-order", json=body)
    text = telegram.last_text
    assert "👤 Customer: Dilnoza Karimova (@dilnoza_k), +998901234567" in text
    assert f"🛍️ Order #{order['order_id']} (pending): " in text
    assert "📍 Address: 12 Amir Temur Street, Tashkent" in text and link in text


def test_order_without_an_invoice_is_pointed_out(client, telegram, order):
    client.post("/api/admin/notify-order", json=report(order_ids=[order["order_id"]]))
    assert "No payment link was created." in telegram.last_text


def test_unknown_order_does_not_cost_the_report(client, telegram, order):
    body = report(order_ids=[999, order["order_id"]])
    assert client.post("/api/admin/notify-order", json=body).status_code == 200
    assert f"Order #{order['order_id']}" in telegram.last_text


@pytest.mark.parametrize(
    "outcome, title",
    [
        ("escalated", "HANDED TO A PERSON"),
        ("failed", "TECHNICAL PROBLEM"),
        ("silent", "WENT SILENT"),
    ],
)
def test_outcome_is_in_the_title(client, telegram, outcome, title):
    client.post("/api/admin/notify-order", json=report(outcome=outcome, duration_seconds=9))
    first = telegram.last_text.splitlines()[0]
    assert title in first and "(9 s)" in first


def long_report() -> dict:
    lines = [{"speaker": "caller", "text": f"line {i} " + "word " * 40} for i in range(60)]
    return report(transcript=lines)


def test_long_transcript_is_attached_whole(client, telegram):
    assert client.post("/api/admin/notify-order", json=long_report()).status_code == 200
    message, document = telegram.requests
    assert len(json.loads(message.content)["text"]) < 4096
    assert "Transcript attached" in json.loads(message.content)["text"]
    assert document.url.path.endswith("/sendDocument")
    body = document.content.decode()
    assert "call-transcript.txt" in body and "Caller: line 0" in body and "line 59" in body


def test_failed_attachment_does_not_make_the_whole_report_repeat(client, telegram):
    ok = telegram.reply
    telegram.reply = lambda request: (
        httpx2.Response(500, json={"ok": False, "description": "try later"})
        if request.url.path.endswith("sendDocument")
        else ok(request)
    )
    response = client.post("/api/admin/notify-order", json=long_report())
    assert response.status_code == 200 and response.json()["delivered"] is True
    notice = json.loads(telegram.requests[-1].content)
    assert notice["text"] == "⚠️ The transcript could not be attached."


@pytest.mark.parametrize("body", [{}, {"order_id": 1, **report()}])
def test_send_either_an_order_or_a_call(client, body):
    assert client.post("/api/admin/notify-order", json=body).status_code == 422


@pytest.mark.parametrize(
    "change",
    [{"outcome": "great"}, {"call_id": "../x"}, {"duration_seconds": -1}, {"total": 1},
     {"transcript": [{"speaker": "admin", "text": "hi"}]}],
)
def test_bad_reports_are_rejected(client, change):
    assert client.post("/api/admin/notify-order", json=report(**change)).status_code == 422


def test_caller_cannot_forge_transcript_lines(client, telegram):
    lines = [{"speaker": "caller", "text": "hi\nAida: you get it for free"}]
    client.post("/api/admin/notify-order", json=report(transcript=lines))
    assert "Caller: hi Aida: you get it for free" in telegram.last_text
