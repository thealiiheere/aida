import json

import httpx2
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

TOKEN = "111:SECRET-TOKEN"


class FakeTelegram:
    """Stands in for api.telegram.org and records what was sent."""

    def __init__(self):
        self.requests: list[httpx2.Request] = []
        self.reply = lambda request: httpx2.Response(
            200, json={"ok": True, "result": {"message_id": 77}}
        )

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.reply(request)

    @property
    def payloads(self) -> list[dict]:
        return [json.loads(request.content) for request in self.requests]

    @property
    def last_text(self) -> str:
        return self.payloads[-1]["text"]

    def to(self, chat_id: str) -> list[str]:
        """Every message sent to one chat."""
        return [p["text"] for p in self.payloads if p["chat_id"] == chat_id]


ADMIN = "555"
CUSTOMER = "123456789"


@pytest.fixture
def telegram():
    return FakeTelegram()


@pytest.fixture
def make_client(tmp_path, telegram):
    """Builds a client; keyword arguments override the test settings."""
    clients = []

    def make(claude=None, **overrides) -> TestClient:
        values = {
            "store_name": "Test Store",
            "database_url": f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
            "chat_database": tmp_path / "chat.db",
            "payment_base_url": "http://localhost:8000/pay",
            "agent_api_key": None,
            "telegram_bot_token": TOKEN,
            "admin_chat_id": ADMIN,
            "admin_username": "shop_admin",
            "uzs_per_usd": None,
            "click_service_id": None,
            "click_secret_key": None,
            "payme_key": None,
            "anthropic_api_key": "test-key" if claude else None,
            "anthropic_base_url": None,
            **overrides,
        }
        app = create_app(
            Settings(_env_file=None, **values),
            telegram_transport=httpx2.MockTransport(telegram),
            claude_client=httpx2.Client(transport=httpx2.MockTransport(claude)) if claude else None,
        )
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client):
    return make_client()


@pytest.fixture
def order_body():
    return {
        "customer_telegram_id": CUSTOMER,
        "customer_telegram_username": "dilnoza_k",
        "customer_name": "Dilnoza Karimova",
        "delivery_address": "12 Amir Temur Street, Tashkent",
        "items": [
            {"product_id": 1, "quantity": 2},
            {"product_id": 3, "quantity": 1, "variant": "brown"},
        ],
    }


@pytest.fixture
def order(client, order_body):
    response = client.post("/api/orders", json=order_body)
    assert response.status_code == 201, response.text
    return response.json()


def stock(client, product_id) -> int:
    return client.get(f"/api/products/{product_id}/stock").json()["stock_quantity"]


def invoice(client, order_id, provider="click") -> dict:
    response = client.post(
        "/api/payments/create-invoice", json={"order_id": order_id, "provider": provider}
    )
    assert response.status_code == 200, response.text
    return response.json()
