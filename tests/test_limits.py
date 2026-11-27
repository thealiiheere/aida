"""No more than five calls a minute for one Telegram account."""

import pytest

from app import limits
from app.limits import RateLimiter

from .conftest import CUSTOMER, invoice


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


def test_five_a_minute(clock):
    limiter = RateLimiter(clock=clock)
    assert [limiter.wait("a") for _ in range(5)] == [0] * 5
    assert limiter.wait("a") == 60
    assert limiter.wait("b") == 0
    clock.now += 59.5
    assert limiter.wait("a") == 1
    clock.now += 0.5
    assert limiter.wait("a") == 0


def test_the_minute_slides(clock):
    limiter = RateLimiter(clock=clock)
    for _ in range(5):
        assert limiter.wait("a") == 0
        clock.now += 10
    assert limiter.wait("a") == 10
    clock.now += 10
    assert limiter.wait("a") == 0
    assert limiter.wait("a") == 10


def test_refused_calls_do_not_extend_the_wait(clock):
    limiter = RateLimiter(clock=clock)
    for _ in range(50):
        limiter.wait("a")
    clock.now += 60
    assert limiter.wait("a") == 0


def test_old_accounts_are_forgotten(clock, monkeypatch):
    monkeypatch.setattr(limits, "MAX_KEYS", 100)
    limiter = RateLimiter(clock=clock)
    for i in range(1000):
        limiter.wait(i)
        clock.now += 1
    assert len(limiter._calls) <= 101


def place(client, body, **changes):
    return client.post("/api/orders", json={**body, **changes})


def test_sixth_order_in_a_minute_is_refused(client, order_body):
    order_body["items"] = [{"product_id": 2, "quantity": 1}]
    assert [place(client, order_body).status_code for _ in range(6)] == [201] * 5 + [429]
    refused = place(client, order_body)
    assert refused.json()["detail"]["code"] == "rate_limited"
    assert 1 <= int(refused.headers["Retry-After"]) <= 60
    assert place(client, order_body, customer_telegram_id="42").status_code == 201
    client.app.state.limiter._clock = lambda: 1e9  # much later
    assert place(client, order_body).status_code == 201


def test_every_endpoint_tied_to_an_account_is_limited(client, order):
    invoice(client, 1)
    calls = {
        "create-invoice": lambda: client.post(
            "/api/payments/create-invoice", json={"order_id": 1, "provider": "click"}
        ),
        "notify-order": lambda: client.post("/api/admin/notify-order", json={"order_id": 1}),
        "escalate": lambda: client.post(
            "/api/admin/escalate",
            json={"reason": "r", "context_summary": "s", "customer_telegram_id": CUSTOMER},
        ),
        "status": lambda: client.get(
            "/api/orders/1/status", params={"customer_telegram_id": CUSTOMER}
        ),
        "call-ended": lambda: client.post(
            "/api/telephony/call-ended",
            json={
                "call_id": "CA1", "caller_telegram_id": CUSTOMER, "raw_audio_transcript": "",
                "call_duration": 1,
            },
        ),
    }  # fmt: skip
    for name, call in calls.items():
        codes = [call().status_code for _ in range(7)]
        # One invoice was made above, so that limit is nearer.
        allowed = 4 if name == "create-invoice" else 5
        assert codes == [200] * allowed + [429] * (7 - allowed), name


def test_a_whole_order_fits_within_the_limits(client, order_body):
    """Each endpoint has its own count, so a normal conversation never hits one."""
    order_body["items"] = [{"product_id": 2, "quantity": 1}]
    for _ in range(5):
        order_id = place(client, order_body).json()["order_id"]
        invoice(client, order_id)
        told = client.post("/api/admin/notify-order", json={"order_id": order_id})
        assert told.status_code == 200


def test_browsing_is_not_limited(client):
    for _ in range(20):
        assert client.get("/api/products/search", params={"q": "mug"}).status_code == 200
        assert client.get("/api/products/1/stock").status_code == 200
