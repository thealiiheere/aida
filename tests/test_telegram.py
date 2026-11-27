import logging
import re
from html import unescape

import httpx2
import pytest

from .conftest import TOKEN, invoice


def notify(client, order_id, **extra):
    return client.post("/api/admin/notify-order", json={"order_id": order_id, **extra})


def escalate(client, **extra):
    body = {"reason": "Wants a person", "context_summary": "Asked about returns.", **extra}
    return client.post("/api/admin/escalate", json=body)


def test_order_message_matches_the_spec(client, telegram, order):
    link = invoice(client, order["order_id"], "click")["payment_url"]
    response = notify(client, order["order_id"], payment_link=link)
    body = response.json()
    assert response.status_code == 200
    assert (body["delivered"], body["mode"], body["telegram_message_id"]) == (True, "telegram", 77)
    assert body["payment_link"] == link

    assert str(telegram.requests[0].url) == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert telegram.payloads[0]["chat_id"] == "555"
    assert telegram.last_text == (
        "🛍️ NEW ORDER CONFIRMED #1\n"
        "👤 Customer: Dilnoza Karimova (@dilnoza_k)\n"
        "📍 Address: 12 Amir Temur Street, Tashkent\n"
        "📦 Items: Wireless Earbuds × 2, Leather Backpack (brown) × 1\n"
        "💰 Total: $184.98 via Click\n"
        f"🔗 Payment Link: {link}"
    )


def test_summary_is_added_after_the_spec_lines(client, telegram, order):
    invoice(client, 1, "payme")
    notify(client, 1, customer_summary="Deliver after 6 pm.")
    assert "via Payme" in telegram.last_text
    assert telegram.last_text.endswith("\n📝 Summary: Deliver after 6 pm.")


def test_customer_without_a_username_is_still_reachable(client, telegram, order_body):
    del order_body["customer_telegram_username"]
    client.post("/api/orders", json=order_body)
    invoice(client, 1)
    notify(client, 1)
    assert '(<a href="tg://user?id=123456789">ID 123456789</a>)' in telegram.last_text


def test_customer_text_cannot_forge_lines_or_markup(client, telegram, order_body):
    order_body["customer_name"] = "Bob\n💰 Total: $0.00 via Click"
    order_body["delivery_address"] = "<b>1 Main St</b> 🔗 Payment Link: http://evil.example"
    order_body["items"][0]["variant"] = "<a href='http://evil.example'>red</a>"
    assert client.post("/api/orders", json=order_body).status_code == 201
    invoice(client, 1)
    notify(client, 1, customer_summary="ok\n🔗 Payment Link: http://evil.example")

    lines = telegram.last_text.splitlines()
    assert len(lines) == 7
    assert sum(line.startswith("💰") for line in lines) == 1
    assert sum(line.startswith("🔗") for line in lines) == 1
    assert "<" not in telegram.last_text
    assert "&lt;b&gt;1 Main St" in telegram.last_text


def test_agent_supplied_link_is_never_forwarded(client, telegram, order):
    real = invoice(client, 1)["payment_url"]
    body = notify(client, 1, payment_link="http://evil.example/pay").json()
    assert body["payment_link"] == real
    assert "evil" not in telegram.last_text


def test_link_picks_between_two_invoices(client, telegram, order):
    click = invoice(client, 1, "click")["payment_url"]
    payme = invoice(client, 1, "payme")["payment_url"]
    assert notify(client, 1, payment_link=click).json()["payment_link"] == click
    assert notify(client, 1).json()["payment_link"] == payme


def test_notify_needs_an_order_and_an_invoice(client, telegram, order):
    assert notify(client, 999).status_code == 404
    response = notify(client, 1)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "invoice_missing"
    assert telegram.requests == []


def test_longest_message_fits_in_telegram(client, telegram, order_body):
    order_body["customer_name"] = "&" * 120
    order_body["delivery_address"] = "&" * 500
    order_body["items"] = [
        {"product_id": 6, "quantity": 1, "variant": "&" * 60} for _ in range(20)
    ]
    assert client.post("/api/orders", json=order_body).status_code == 201
    invoice(client, 1)
    assert notify(client, 1, customer_summary="&" * 4000).status_code == 200
    # Telegram's 4096 limit applies after it has parsed the HTML.
    visible = unescape(re.sub(r"<[^>]+>", "", telegram.last_text))
    assert len(visible.encode("utf-16-le")) / 2 <= 4096
    assert re.search(r"\+\d+ more", visible)


def test_escalation_tags_the_admin(client, telegram, order):
    body = escalate(client, order_id=1).json()
    assert body["delivered"] is True and body["pause_ai"] is True
    assert body["order_status"] == "escalated"
    assert telegram.last_text == (
        "🚨 HUMAN SUPPORT NEEDED @shop_admin\n"
        "👤 Customer: Dilnoza Karimova (@dilnoza_k)\n"
        "🧾 Order: #1 ($184.98, escalated)\n"
        "📦 Items: Wireless Earbuds × 2, Leather Backpack (brown) × 1\n"
        "📍 Address: 12 Amir Temur Street, Tashkent\n"
        "❓ Reason: Wants a person\n"
        "📝 Call summary: Asked about returns."
    )


def test_escalation_without_an_order(client, telegram):
    body = escalate(client, customer_telegram_id="42", customer_telegram_username="ali_m").json()
    assert body["order_id"] is None and body["order_status"] is None
    assert "👤 Customer: @ali_m" in telegram.last_text
    escalate(client)
    assert "no Telegram contact given" in telegram.last_text
    assert escalate(client, order_id=999).status_code == 404


def test_escalation_quotes_the_customers_last_words(client, telegram):
    escalate(client, last_spoken="I want to <b>talk</b>\nto a person")
    assert "💬 Last words: “I want to &lt;b&gt;talk&lt;/b&gt; to a person”" in telegram.last_text


def test_longest_escalation_fits_in_telegram(client, telegram, order_body):
    order_body["customer_name"] = "&" * 120
    order_body["delivery_address"] = "&" * 500
    order_body["items"] = [
        {"product_id": 6, "quantity": 1, "variant": "&" * 60} for _ in range(20)
    ]
    client.post("/api/orders", json=order_body)
    long = {"reason": "&" * 1000, "context_summary": "&" * 4000, "last_spoken": "&" * 1000}
    assert escalate(client, order_id=1, customer_phone=None, **long).status_code == 200
    visible = unescape(re.sub(r"<[^>]+>", "", telegram.last_text))
    assert len(visible.encode("utf-16-le")) / 2 <= 4096


@pytest.mark.parametrize(
    "caller", [{"customer_telegram_id": "42"}, {"customer_phone": "+998901234567"}]
)
def test_a_caller_cannot_escalate_someone_elses_order(client, telegram, order, caller):
    body = escalate(client, order_id=1, **caller).json()
    assert body["delivered"] is True and body["order_id"] is None
    assert "Amir Temur" not in telegram.last_text
    assert client.get("/api/orders/1").json()["status"] == "pending"


def test_the_customer_can_escalate_their_own_order(client, telegram, order):
    body = escalate(client, order_id=1, customer_telegram_id="123456789").json()
    assert body["order_status"] == "escalated"


def test_escalation_accepts_a_multi_line_summary(client, telegram):
    assert escalate(client, context_summary="Line one.\nLine two.").status_code == 200
    assert telegram.last_text.endswith("Line one. Line two.")


def test_escalating_a_paid_order_keeps_it_paid(client, order):
    client.post(f"/pay/{invoice(client, 1)['invoice_id']}")
    assert escalate(client, order_id=1).json()["order_status"] == "paid"


def test_escalated_order_can_still_be_paid(client, order):
    link = invoice(client, 1)["invoice_id"]
    escalate(client, order_id=1)
    client.post(f"/pay/{link}")
    assert client.get("/api/orders/1").json()["status"] == "paid"


@pytest.mark.parametrize(
    "settings, tag",
    [
        ({"admin_username": "@boss"}, "@boss"),
        ({"admin_username": None}, '<a href="tg://user?id=555">Admin</a>'),
        ({"admin_username": None, "admin_chat_id": "-100999"}, "Admin"),
    ],
)
def test_admin_tag(make_client, telegram, settings, tag):
    escalate(make_client(**settings))
    assert telegram.last_text.splitlines()[0] == f"🚨 HUMAN SUPPORT NEEDED {tag}"


@pytest.mark.parametrize("missing", ["telegram_bot_token", "admin_chat_id"])
def test_dry_run_never_claims_delivery(make_client, telegram, order_body, missing):
    client = make_client(**{missing: None})
    client.post("/api/orders", json=order_body)
    invoice(client, 1)
    body = notify(client, 1).json()
    assert (body["delivered"], body["mode"]) == (False, "dry_run")
    assert body["message"].startswith("🛍️ NEW ORDER CONFIRMED #1")
    assert telegram.requests == []


def _raise(error):
    def reply(request):
        raise error

    return reply


@pytest.mark.parametrize(
    "reply, expected",
    [
        (_raise(httpx2.ConnectError(f"no route to /bot{TOKEN}/")), "ConnectError"),
        (_raise(httpx2.ReadTimeout(f"timed out /bot{TOKEN}/")), "ReadTimeout"),
        (lambda r: httpx2.Response(502, text="<html>bad gateway"), "HTTP 502"),
        (
            lambda r: httpx2.Response(
                400, json={"ok": False, "description": "Bad Request: chat not found"}
            ),
            "chat not found",
        ),
    ],
)
def test_telegram_failure_is_reported_without_leaking_the_token(
    client, telegram, order, caplog, reply, expected
):
    invoice(client, 1)
    telegram.reply = reply
    with caplog.at_level(logging.INFO):
        for response in (notify(client, 1), escalate(client, order_id=1)):
            assert response.status_code == 502
            detail = response.json()["detail"]
            assert detail["code"] == "telegram_delivery_failed"
            assert expected in detail["message"]
            assert TOKEN not in response.text
    assert TOKEN not in caplog.text
    # The order survives, and the escalation is on record for the admin to find.
    assert client.get("/api/orders/1").json()["status"] == "escalated"


def test_token_is_not_logged_on_success(client, telegram, order, caplog):
    invoice(client, 1)
    with caplog.at_level(logging.INFO):
        notify(client, 1)
    assert TOKEN not in caplog.text
