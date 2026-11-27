"""The store console: its page, and the read-only data behind it."""

import json

from sqlalchemy.orm import Session

from app.chats import describe, entries_of
from app.models import PaymentTransaction
from chat.storage import Chat, Storage

from .conftest import CUSTOMER, invoice
from .test_holds import expire


def console(client) -> dict:
    response = client.get("/api/console")
    assert response.status_code == 200, response.text
    return response.json()


def shelf(data: dict, product_id: int) -> dict:
    return next(p for p in data["shelf"] if p["product_id"] == product_id)


# --- The page ---------------------------------------------------------------------


def test_page_opens_without_a_key(make_client):
    client = make_client(agent_api_key="secret")
    page = client.get("/console")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "/api/console" in page.text
    assert client.get("/", follow_redirects=False).headers["location"] == "/console"


def test_data_needs_the_key(make_client):
    client = make_client(agent_api_key="secret")
    assert client.get("/api/console").status_code == 401
    assert client.get("/api/console/calls/CA1").status_code == 401
    assert client.get("/api/console", headers={"X-API-Key": "secret"}).status_code == 200


# --- Overview ---------------------------------------------------------------------


def test_new_store(client):
    data = console(client)
    assert data["orders_total"] == 0
    assert data["orders"] == [] and data["calls"] == []
    assert all(c == {"count": 0, "total": 0.0} for c in data["counts"].values())
    assert [p["title"] for p in data["shelf"]][:2] == ["Wireless Earbuds", "Coffee Beans"]
    assert shelf(data, 1) | {} == {
        "product_id": 1, "title": "Wireless Earbuds", "category": "Electronics",
        "price": 49.99, "on_shelf": 25, "held": 0, "sold": 0,
    }  # fmt: skip
    assert data["chats"] == {
        "available": False,
        "note": "The chat hasn't run yet, so there are no conversations.",
        "items": [],
    }


def test_store_status(make_client):
    data = console(make_client(telegram_bot_token=None, admin_chat_id=None))
    assert data["store"] | {} == {
        "name": "Test Store", "telegram": "dry_run", "customer_messages": False,
        "payments": "demo", "gateways": [], "summaries": False, "ai": "claude",
        "language": "en", "api_key": False, "hold_minutes": 15, "expiry_check_seconds": 60,
    }  # fmt: skip
    real = console(make_client(payme_key="k", uzs_per_usd="12500"))["store"]
    assert (real["telegram"], real["payments"], real["gateways"]) == ("live", "real", ["payme"])


def test_order_on_hold(client, order):
    data = console(client)
    assert data["counts"]["pending"] == {"count": 1, "total": 184.98}
    assert data["orders_total"] == 1
    (shown,) = data["orders"]
    assert shown["order_id"] == order["order_id"]
    assert shown["reserved_until"] == order["reserved_until"]
    assert shown["transactions"] == []
    # Two earbuds and a backpack left the shelf and are held, not sold.
    assert (shelf(data, 1)["on_shelf"], shelf(data, 1)["held"], shelf(data, 1)["sold"]) == (23, 2, 0)
    assert (shelf(data, 3)["on_shelf"], shelf(data, 3)["held"]) == (11, 1)


def test_paid_order(client, order):
    link = invoice(client, order["order_id"])["invoice_id"]
    client.post(f"/pay/{link}")
    data = console(client)
    assert data["counts"]["pending"]["count"] == 0
    assert data["counts"]["paid"] == {"count": 1, "total": 184.98}
    assert (shelf(data, 1)["on_shelf"], shelf(data, 1)["held"], shelf(data, 1)["sold"]) == (23, 0, 2)
    assert data["orders"][0]["invoices"][0]["status"] == "paid"


def test_expired_order(client, order):
    expire(client)
    data = console(client)
    assert data["counts"]["cancelled_expired"] == {"count": 1, "total": 184.98}
    assert (shelf(data, 1)["on_shelf"], shelf(data, 1)["held"], shelf(data, 1)["sold"]) == (25, 0, 0)


def test_newest_orders_first_and_limited(client, order_body):
    for _ in range(3):
        assert client.post("/api/orders", json=order_body).status_code == 201
    data = client.get("/api/console", params={"limit": 2}).json()
    assert [o["order_id"] for o in data["orders"]] == [3, 2]
    assert data["orders_total"] == 3


def test_gateway_payments_in_soum(client, order):
    with Session(client.app.state.engine) as db:
        db.add(
            PaymentTransaction(
                provider="payme", external_id="63a1f0c2", order_id=order["order_id"],
                amount_tiyin=231_225_000, state="created", requested_ms=1_764_000_000_000,
                created_ms=1_764_000_000_000,
            )
        )  # fmt: skip
        db.commit()
    (tx,) = console(client)["orders"][0]["transactions"]
    assert tx == {
        "provider": "payme", "external_id": "63a1f0c2", "state": "created",
        "amount_uzs": 2_312_250.0, "requested_at": "2025-11-24T16:00:00Z",
    }  # fmt: skip


# --- Calls ------------------------------------------------------------------------


def test_finished_call(client, order):
    report = {
        "call_id": "CA77", "caller_telegram_id": CUSTOMER, "call_duration": 134,
        "raw_audio_transcript": "Aida: Hello!\nCaller: Two earbuds and a backpack, please.",
    }  # fmt: skip
    assert client.post("/api/telephony/call-ended", json=report).status_code == 200
    (call,) = console(client)["calls"]
    assert call == {
        "call_id": "CA77", "caller_telegram_id": CUSTOMER, "caller_name": "Dilnoza Karimova",
        "caller_username": "dilnoza_k", "duration_seconds": 134, "outcome": "Order Placed",
        "customer_intent": None, "items_discussed": None, "created_at": call["created_at"],
        "reported": True,
    }  # fmt: skip
    detail = client.get("/api/console/calls/CA77").json()
    assert detail["transcript"] == report["raw_audio_transcript"]
    assert client.get("/api/console/calls/CA78").json()["detail"]["code"] == "call_not_found"


# --- Chats ------------------------------------------------------------------------

CONVERSATION = [
    {"role": "user", "content": [
        {"type": "text", "text": "[The customer pressed Start.]"},
        {"type": "text", "text": "Do you have earbuds?"},
    ]},
    {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "Search first.", "signature": "x"},
        {"type": "text", "text": "Let me look."},
        {"type": "tool_use", "id": "t1", "name": "search_catalog", "input": {"query": "earbuds"}},
    ]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1",
         "content": json.dumps({"ok": True, "data": {"count": 1, "products": []}})},
    ]},
    {"role": "assistant", "content": [
        {"type": "tool_use", "id": "t2", "name": "create_order", "input": {"items": []}},
    ]},
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t2", "is_error": True, "content": json.dumps(
            {"ok": False, "error": {"code": "insufficient_stock", "message": "Not enough stock."}}
        )},
    ]},
    {"role": "assistant", "content": [{"type": "text", "text": "Sorry, **they** just sold out."}]},
]  # fmt: skip


def save_chat(client, chat: Chat) -> None:
    storage = Storage(client.app.state.settings.chat_database, clock=lambda: 1_764_000_000)
    storage.save(chat)
    storage.close()


def test_chat_conversation(client, order):
    chat = Chat(int(CUSTOMER), "dilnoza_k", active_at=1_764_000_000)
    chat.conversation.messages = CONVERSATION
    save_chat(client, chat)

    (row,) = console(client)["chats"]["items"]
    assert row == {
        "chat_id": int(CUSTOMER), "username": "dilnoza_k", "customer_name": "Dilnoza Karimova",
        "state": "aida", "active_at": "2025-11-24T16:00:00Z", "paused_at": None, "failures": 0,
        "waiting": 0, "messages": 3, "last_text": "Sorry, **they** just sold out.", "last_from": "aida",
    }  # fmt: skip

    detail = client.get(f"/api/console/chats/{CUSTOMER}").json()
    assert [(e["kind"], e["text"], e["ok"]) for e in detail["entries"]] == [
        ("note", "The customer pressed Start.", None),
        ("customer", "Do you have earbuds?", None),
        ("aida", "Let me look.", None),
        ("tool", "Searched the catalog for “earbuds”: 1 found", True),
        ("tool", "Order failed: Not enough stock.", False),
        ("aida", "Sorry, **they** just sold out.", None),
    ]
    # Shown the way Telegram showed it, escaped.
    assert detail["entries"][-1]["html"] == "Sorry, <b>they</b> just sold out."
    assert detail["entries"][1]["html"] is None


def test_chat_with_a_person(client):
    save_chat(client, Chat(42, None, active_at=1_764_000_000, paused_at=1_764_000_100))
    (row,) = console(client)["chats"]["items"]
    assert (row["state"], row["customer_name"], row["last_text"]) == ("with_person", None, None)
    assert client.get("/api/console/chats/43").json()["detail"]["code"] == "chat_not_found"


def test_chats_newest_first(client):
    for chat_id, active in ((1, 100), (2, 300), (3, 200)):
        save_chat(client, Chat(chat_id, active_at=1_764_000_000 + active))
    assert [c["chat_id"] for c in console(client)["chats"]["items"]] == [2, 3, 1]


def test_unreadable_chat_database(client):
    client.app.state.settings.chat_database.write_text("not a database")
    chats = console(client)["chats"]
    assert chats["available"] is False and "can't be read" in chats["note"]
    assert client.get("/api/console/chats/1").status_code == 503


def test_tool_lines():
    paid = {"ok": True, "data": {"order_id": 4, "status": "paid"}}
    assert describe("get_order_status", {"order_id": "4"}, paid) == "Checked order #4: paid"
    sent = {"ok": True, "data": {"sent_to_customer": False}}
    assert describe("generate_invoice", {"order_id": 4, "payment_provider": "payme"}, sent) == (
        "Made a Payme payment link for order #4, not sent to the chat"
    )
    dry = {"ok": True, "data": {"delivered": False}}
    assert describe("escalate_to_human", {"reason": "Asked for a person"}, dry) == (
        "Handed over to a person: Asked for a person (dry run, not delivered)"
    )
    # A call that never got a result still shows what was asked.
    entries = entries_of([{"role": "assistant", "content": [
        {"type": "tool_use", "id": "x", "name": "check_stock", "input": {"product_id": "7"}},
    ]}])  # fmt: skip
    assert [(e.text, e.ok) for e in entries] == [("Checked stock of product 7", None)]
