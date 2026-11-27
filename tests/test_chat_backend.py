"""What the store backend does for chats."""

import httpx2
import pytest

from app.config import Settings
from app.telegram import TelegramNotifier

from .conftest import CUSTOMER, TOKEN

ORDERS = f"/api/customers/{CUSTOMER}/orders"


def test_recent_orders_are_the_customers_own_newest_first(client, order_body):
    client.post("/api/orders", json={**order_body, "customer_telegram_id": "42"})
    for quantity in (1, 2):
        items = [{"product_id": 2, "quantity": quantity}]
        assert client.post("/api/orders", json={**order_body, "items": items}).is_success
    found = client.get(ORDERS).json()
    assert [order["order_id"] for order in found] == [3, 2]
    assert found[0] == client.get("/api/orders/3").json()
    assert client.get("/api/customers/7/orders").json() == []


def test_only_five_orders_are_listed(client, order_body, monkeypatch):
    monkeypatch.setattr("app.limits.RateLimiter.wait", lambda self, *key: 0)
    for _ in range(6):
        items = [{"product_id": 2, "quantity": 1}]
        assert client.post("/api/orders", json={**order_body, "items": items}).is_success
    assert [order["order_id"] for order in client.get(ORDERS).json()] == [6, 5, 4, 3, 2]


def test_recent_orders_need_the_api_key(make_client):
    client = make_client(agent_api_key="secret")
    assert client.get(ORDERS).status_code == 401
    assert client.get(ORDERS, headers={"X-API-Key": "secret"}).json() == []


@pytest.mark.parametrize("telegram_id", ["abc", "-5", "1' OR '1'='1", "1" * 30])
def test_bad_customer_ids_are_rejected(client, telegram_id):
    assert client.get(f"/api/customers/{telegram_id}/orders").status_code == 422


def test_recent_orders_are_rate_limited(client):
    assert [client.get(ORDERS).status_code for _ in range(6)] == [200] * 5 + [429]
    assert client.get("/api/customers/42/orders").status_code == 200


def test_telegram_server_can_be_replaced():
    asked = []

    def server(request):
        asked.append(str(request.url))
        return httpx2.Response(200, json={"ok": True, "result": {"message_id": 1}})

    settings = Settings(
        _env_file=None,
        telegram_bot_token=TOKEN,
        admin_chat_id="5",
        telegram_api_url="http://127.0.0.1:8081/",
    )
    notifier = TelegramNotifier(settings, httpx2.MockTransport(server))
    assert notifier.send_admin("Hi").delivered
    notifier.close()
    assert asked == [f"http://127.0.0.1:8081/bot{TOKEN}/sendMessage"]
