"""Stock held for unpaid orders, and what happens when the hold runs out."""

import sqlite3
import time
from datetime import datetime, timedelta

import httpx2
import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

from app import lifecycle
from app.db import OldDatabase
from app.lifecycle import HOLD, expire_and_tell, expire_orders, mark_paid
from app.models import Order, utcnow
from app.seed import main as seed_main

from .conftest import ADMIN, CUSTOMER, invoice, stock


def expire(client, after: timedelta = HOLD + timedelta(seconds=1)) -> None:
    """Run the worker's job as if `after` had gone by."""
    state = client.app.state
    expire_and_tell(state.engine, state.telegram, utcnow() + after)


def age(client, order_id: int, minutes: float) -> None:
    with Session(client.app.state.engine) as db:
        created = utcnow() - timedelta(minutes=minutes)
        db.execute(update(Order).where(Order.id == order_id).values(created_at=created))
        db.commit()


def read(client, order_id: int = 1) -> dict:
    return client.get(f"/api/orders/{order_id}").json()


def until(condition) -> None:
    deadline = time.monotonic() + 5
    while not condition():
        assert time.monotonic() < deadline, "never happened"
        time.sleep(0.02)


# --- Holding ------------------------------------------------------------------


def test_order_holds_its_stock_for_fifteen_minutes(client, order):
    held = datetime.fromisoformat(order["reserved_until"]) - datetime.fromisoformat(
        order["created_at"]
    )
    assert held == timedelta(minutes=15)
    assert {item["state"] for item in order["items"]} == {"reserved"}
    assert (stock(client, 1), stock(client, 3)) == (23, 11)


def test_hold_lasts_the_full_fifteen_minutes(client, telegram, order):
    expire(client, timedelta(minutes=14, seconds=58))
    assert read(client)["status"] == "pending"
    assert stock(client, 1) == 23 and telegram.requests == []


def test_unpaid_order_expires_and_gives_its_stock_back(client, telegram, order):
    invoice(client, 1)
    expire(client)
    body = read(client)
    assert body["status"] == "cancelled_expired" and body["reserved_until"] is None
    assert {item["state"] for item in body["items"]} == {"released"}
    assert (stock(client, 1), stock(client, 3)) == (25, 12)
    assert telegram.to(CUSTOMER) == [
        "Your payment link for order #1 has expired. "
        "Let us know when you're ready to order again!"
    ]
    assert telegram.to(ADMIN) == []


def test_customer_without_a_link_is_not_told_about_one(client, telegram, order):
    expire(client)
    assert telegram.last_text.startswith("Your reservation for order #1 has expired.")


def test_stock_is_released_once(client, telegram, order):
    for _ in range(3):
        expire(client)
    assert stock(client, 1) == 25
    assert len(telegram.requests) == 1


def test_same_product_on_two_lines_is_all_released(client, order_body):
    order_body["items"] = [
        {"product_id": 8, "quantity": 2, "variant": "blue"},
        {"product_id": 8, "quantity": 1, "variant": "green"},
    ]
    client.post("/api/orders", json=order_body)
    assert stock(client, 8) == 0
    expire(client)
    assert stock(client, 8) == 3


def test_paid_and_escalated_orders_keep_their_stock(client, order_body, order):
    client.post("/api/orders", json=order_body)
    client.post(f"/pay/{invoice(client, 1)['invoice_id']}")
    client.post(
        "/api/admin/escalate", json={"reason": "r", "context_summary": "s", "order_id": 2}
    )
    expire(client)
    assert [read(client, i)["status"] for i in (1, 2)] == ["paid", "escalated"]
    assert stock(client, 1) == 21


def test_payment_during_the_sweep_wins(client, order_body, order):
    client.post("/api/orders", json=order_body)
    with Session(client.app.state.engine) as db:
        sweep = expire_orders(db, utcnow() + HOLD + timedelta(seconds=1))
        assert next(sweep).id == 1
        client.post(f"/pay/{invoice(client, 2)['invoice_id']}")
        assert list(sweep) == []
    assert read(client, 2)["status"] == "paid"
    assert stock(client, 1) == 23


def test_telegram_being_down_does_not_keep_items_off_the_shelf(client, telegram, order_body, order):
    client.post("/api/orders", json=order_body)
    telegram.reply = lambda request: httpx2.Response(
        403, json={"ok": False, "description": "Forbidden: bot was blocked by the user"}
    )
    expire(client)
    assert [read(client, i)["status"] for i in (1, 2)] == ["cancelled_expired"] * 2
    assert stock(client, 1) == 25


# --- The worker ---------------------------------------------------------------


def test_worker_checks_every_minute():
    assert lifecycle.EXPIRY_CHECK_SECONDS == 60


def test_worker_runs_by_itself_and_survives_an_error(client, make_client, monkeypatch, order):
    age(client, 1, minutes=16)
    real, calls = lifecycle.expire_and_tell, []

    def failing_once(*args):
        calls.append(args)
        if len(calls) == 1:
            raise OSError("disk trouble")
        real(*args)

    monkeypatch.setattr(lifecycle, "expire_and_tell", failing_once)
    monkeypatch.setattr(lifecycle, "EXPIRY_CHECK_SECONDS", 0.05)
    second = make_client()  # same database, with a worker of its own
    until(lambda: read(second)["status"] == "cancelled_expired")
    assert stock(second, 1) == 25


# --- Expired orders -----------------------------------------------------------


@pytest.fixture
def expired(client, telegram, order):
    link = invoice(client, 1)["invoice_id"]
    expire(client)
    telegram.requests.clear()
    return link


def test_expired_order_gets_no_new_link(client, expired):
    for provider in ("click", "payme"):
        response = client.post(
            "/api/payments/create-invoice", json={"order_id": 1, "provider": provider}
        )
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "order_expired"


def test_expired_link_says_so_and_takes_no_payment(client, telegram, expired):
    page = client.get(f"/pay/{expired}").text
    assert "This payment link has expired" in page and "<button" not in page
    assert "This payment link has expired" in client.post(f"/pay/{expired}").text
    assert read(client)["status"] == "cancelled_expired"
    assert telegram.requests == []


def test_expired_order_is_not_announced_or_revived(client, telegram, expired):
    response = client.post("/api/admin/notify-order", json={"order_id": 1})
    assert response.json()["detail"]["code"] == "order_expired"
    body = {"reason": "r", "context_summary": "s", "order_id": 1}
    assert client.post("/api/admin/escalate", json=body).json()["order_status"] == (
        "cancelled_expired"
    )


# --- Paying -------------------------------------------------------------------


def test_paying_deducts_stock_once_and_tells_both_sides(client, telegram, order):
    link = f"/pay/{invoice(client, 1)['invoice_id']}"
    for _ in range(3):
        client.post(link)
    body = read(client)
    assert body["status"] == "paid" and body["reserved_until"] is None
    assert {item["state"] for item in body["items"]} == {"sold"}
    assert (stock(client, 1), stock(client, 3)) == (23, 11)
    assert telegram.to(CUSTOMER) == [
        "✅ Payment Received! Your order #1 is confirmed and being prepared for shipment."
    ]
    assert telegram.to(ADMIN) == ["💳 PAYMENT CONFIRMED #1 | $184.98 received via Click."]


def test_payment_stands_when_nobody_can_be_told(client, telegram, order):
    telegram.reply = lambda request: httpx2.Response(500, text="down")
    client.post(f"/pay/{invoice(client, 1)['invoice_id']}")
    assert read(client)["status"] == "paid"


def test_demo_checkout_is_off_when_real_payments_are_on(make_client, telegram, order_body):
    client = make_client(payme_key="secret", uzs_per_usd=12800)
    client.post("/api/orders", json=order_body)
    link = f"/pay/{invoice(client, 1)['invoice_id']}"
    page = client.get(link).text
    assert "can't take payments" in page and "<button" not in page
    client.post(link)
    assert read(client)["status"] == "pending" and telegram.requests == []


def test_late_payment_takes_the_stock_again(client, order):
    expire(client)
    with Session(client.app.state.engine) as db:
        assert mark_paid(db, 1, "click") == "paid"
        db.commit()
        assert mark_paid(db, 1, "click") == "already_paid"
    body = read(client)
    assert body["status"] == "paid" and {i["state"] for i in body["items"]} == {"sold"}
    assert (stock(client, 1), stock(client, 3)) == (23, 11)


def test_late_payment_is_refused_when_the_items_have_sold(client, order_body):
    order_body["items"] = [{"product_id": 1, "quantity": 1}, {"product_id": 8, "quantity": 3}]
    client.post("/api/orders", json=order_body)
    expire(client)
    order_body["items"] = [{"product_id": 8, "quantity": 1}]
    client.post("/api/orders", json=order_body)
    with Session(client.app.state.engine) as db:
        assert mark_paid(db, 1, "click") == "out_of_stock"
        db.commit()
    assert read(client, 1)["status"] == "cancelled_expired"
    assert (stock(client, 1), stock(client, 8)) == (25, 2)  # nothing half-taken


# --- A customer asking about their order --------------------------------------


def status(client, order_id=1, **params):
    return client.get(f"/api/orders/{order_id}/status", params=params)


def test_customer_can_ask_about_their_order(client, order):
    body = status(client, customer_telegram_id=CUSTOMER).json()
    assert body == {
        "order_id": 1,
        "status": "pending",
        "total_amount": 184.98,
        "reserved_until": order["reserved_until"],
    }
    client.post(f"/pay/{invoice(client, 1)['invoice_id']}")
    assert status(client, customer_telegram_id=CUSTOMER).json()["status"] == "paid"


@pytest.mark.parametrize("params", [{"customer_telegram_id": "42"}, {}])
def test_nobody_else_learns_anything(client, order, params):
    theirs, missing = status(client, 1, **params), status(client, 99, **params)
    assert theirs.status_code == missing.status_code == 404
    assert theirs.text == missing.text.replace("99", "1")


# --- Databases from before Stage 3 --------------------------------------------


@pytest.fixture
def old_database(tmp_path):
    path = tmp_path / "test.db"
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE products (id INTEGER PRIMARY KEY, title VARCHAR(120) NOT NULL,
          price INTEGER NOT NULL, stock_quantity INTEGER NOT NULL, category VARCHAR(60) NOT NULL,
          description VARCHAR NOT NULL);
        INSERT INTO products VALUES (1, 'Hand-made Vase', 4200, 2, 'Home', '');
        CREATE TABLE orders (id INTEGER PRIMARY KEY, status VARCHAR(10),
          CHECK (status IN ('pending', 'paid', 'escalated')));
        CREATE TABLE order_items (id INTEGER PRIMARY KEY, order_id INTEGER);
        CREATE TABLE invoices (id VARCHAR(40) PRIMARY KEY, order_id INTEGER);
        """
    )
    db.commit()
    yield db
    db.close()


def test_old_database_without_orders_is_upgraded(old_database, make_client, order_body):
    client = make_client()
    [product] = client.get("/api/products/search").json()["products"]
    assert product["title"] == "Hand-made Vase"  # the catalog is kept
    order_body["items"] = [{"product_id": 1, "quantity": 2}]
    assert client.post("/api/orders", json=order_body).status_code == 201
    expire(client)
    assert stock(client, 1) == 2


def test_old_database_with_orders_is_left_alone(old_database, make_client):
    old_database.execute("INSERT INTO orders VALUES (1, 'paid')")
    old_database.commit()
    with pytest.raises(OldDatabase, match="app.seed --reset"):
        make_client()
    assert old_database.execute("SELECT status FROM orders").fetchall() == [("paid",)]


def test_reset_works_on_an_old_database(old_database, make_client, monkeypatch, tmp_path):
    old_database.execute("INSERT INTO orders VALUES (1, 'paid')")
    old_database.commit()
    old_database.close()
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    monkeypatch.setattr("app.seed.get_settings", lambda: type("S", (), {"database_url": url}))
    monkeypatch.setattr("sys.argv", ["seed", "--reset"])
    seed_main()
    assert len(make_client().get("/api/products/search").json()["products"]) == 8
