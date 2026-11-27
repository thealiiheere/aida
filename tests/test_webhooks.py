"""Click and Payme confirming payments."""

import base64
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from urllib.parse import urlencode

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.lifecycle import HOLD
from app.models import PaymentTransaction
from app.transactions import start

from .conftest import ADMIN, CUSTOMER, stock
from .test_holds import expire, read

SECRET, SERVICE, KEY = "click-secret", "4321", "payme-key"
# $184.98 at 12,800 soum is 2,367,744.
SOUM, TIYIN = "2367744", 236774400

PAID = "✅ Payment Received! Your order #1 is confirmed and being prepared for shipment."


def confirmed(provider: str) -> str:
    return f"💳 PAYMENT CONFIRMED #1 | $184.98 received via {provider}."


@pytest.fixture
def client(make_client):
    return make_client(
        uzs_per_usd="12800",
        click_service_id=SERVICE,
        click_secret_key=SECRET,
        payme_key=KEY,
        agent_api_key="store-key",  # which gateways don't have
    )


@pytest.fixture(autouse=True)
def order(client, order_body):
    client.headers["X-API-Key"] = "store-key"
    assert client.post("/api/orders", json=order_body).status_code == 201
    return order_body


@pytest.fixture
def sold_out_late(client, order):
    """Call it, and order 1 has expired and its earbuds are gone."""

    def run():
        expire(client)
        order["items"] = [{"product_id": 1, "quantity": 25}]
        order["customer_telegram_id"] = "42"
        assert client.post("/api/orders", json=order).status_code == 201

    return run


def assert_untouched(client, telegram):
    assert read(client)["status"] == "pending"
    assert stock(client, 1) == 23 and telegram.requests == []


def assert_paid_once(client, telegram, provider):
    body = read(client)
    assert body["status"] == "paid" and {i["state"] for i in body["items"]} == {"sold"}
    assert (stock(client, 1), stock(client, 3)) == (23, 11)
    assert telegram.to(CUSTOMER) == [PAID]
    assert telegram.to(ADMIN) == [confirmed(provider)]


# --- Click ----------------------------------------------------------------------


def sign(form: dict) -> str:
    prepare_id = form["merchant_prepare_id"] if form["action"] == "1" else ""
    text = (
        f"{form['click_trans_id']}{form['service_id']}{SECRET}{form['merchant_trans_id']}"
        f"{prepare_id}{form['amount']}{form['action']}{form['sign_time']}"
    )
    return hashlib.md5(text.encode()).hexdigest()


def click(client, signature=None, **fields):
    form = {
        "click_trans_id": "90001",
        "service_id": SERVICE,
        "click_paydoc_id": "5",
        "merchant_trans_id": "1",
        "amount": SOUM,
        "action": "0",
        "error": "0",
        "error_note": "Success",
        "sign_time": "2026-11-24 12:00:00",
        **fields,
    }
    form["sign_string"] = sign(form) if signature is None else signature
    response = client.post(
        "/api/webhooks/click",
        content=urlencode(form),
        headers={"Content-Type": "application/x-www-form-urlencoded", "X-API-Key": ""},
    )
    assert response.status_code == 200, response.text
    return response.json()


def prepare(client, **fields) -> dict:
    return click(client, action="0", **fields)


def complete(client, prepared: dict, **fields) -> dict:
    fields = {"merchant_prepare_id": str(prepared["merchant_prepare_id"]), **fields}
    return click(client, action="1", **fields)


def test_click_payment(client, telegram):
    prepared = prepare(client)
    assert prepared["error"] == 0 and prepared["error_note"] == "Success"
    assert prepared["click_trans_id"] == "90001" and prepared["merchant_trans_id"] == "1"
    assert_untouched(client, telegram)

    done = complete(client, prepared)
    assert done["error"] == 0
    assert done["merchant_confirm_id"] == prepared["merchant_prepare_id"]
    assert_paid_once(client, telegram, "Click")


@pytest.mark.parametrize("amount", [SOUM, SOUM + ".00", SOUM + ".0"])
def test_click_amount_may_be_written_differently(client, amount):
    assert prepare(client, amount=amount)["error"] == 0


def test_click_signature_against_a_known_value():
    """Worked out by hand from Click's formula, so the test's helper can't hide a mistake."""
    from app.routers.click import click_signature

    form = {
        "click_trans_id": "1", "service_id": "2", "merchant_trans_id": "3",
        "merchant_prepare_id": "4", "amount": "5", "sign_time": "t",
    }
    assert click_signature({**form, "action": "0"}, "S") == hashlib.md5(b"12S350t").hexdigest()
    assert click_signature({**form, "action": "1"}, "S") == hashlib.md5(b"12S3451t").hexdigest()


def test_click_signature_is_required(client, telegram):
    for bad in ("", "0" * 32, "é"):
        assert prepare(client, signature=bad)["error"] == -1
    assert_untouched(client, telegram)


def test_click_signature_covers_what_matters(client, telegram):
    """A captured signature can't be reused for another order, amount or payment."""
    prepared = prepare(client)
    honest = {
        "click_trans_id": "90001", "service_id": SERVICE, "merchant_trans_id": "1",
        "merchant_prepare_id": str(prepared["merchant_prepare_id"]), "amount": SOUM,
        "action": "1", "sign_time": "2026-11-24 12:00:00",
    }
    for field in honest:
        changed = {**honest, field: honest[field] + "1"}
        if field == "action":
            changed[field] = "0"
        assert click(client, signature=sign(honest), **changed)["error"] == -1, field
    assert_untouched(client, telegram)


@pytest.mark.parametrize(
    "fields, code",
    [
        ({"amount": "2367743"}, -2),
        ({"amount": "2367745"}, -2),
        ({"amount": "184.98"}, -2),
        ({"amount": "NaN"}, -2),
        ({"amount": "-2367744"}, -2),
        ({"amount": ""}, -2),
        ({"action": "2"}, -3),
        ({"merchant_trans_id": "99"}, -5),
        ({"merchant_trans_id": "1; DROP TABLE orders"}, -5),
        ({"merchant_trans_id": "9" * 30}, -5),
        ({"service_id": "1111"}, -8),
        ({"click_trans_id": ""}, -8),
    ],
)
def test_click_refuses_bad_requests(client, telegram, fields, code):
    assert click(client, **fields)["error"] == code
    assert_untouched(client, telegram)


def test_click_empty_request(client, telegram):
    response = client.post("/api/webhooks/click")
    assert response.json()["error"] == -1


def test_click_complete_needs_its_own_prepare(client, telegram):
    prepared = prepare(client)
    assert complete(client, {"merchant_prepare_id": 99})["error"] == -6
    assert complete(client, {"merchant_prepare_id": "x"})["error"] == -6
    assert complete(client, prepared, click_trans_id="90002")["error"] == -6
    assert_untouched(client, telegram)


def test_click_cannot_complete_with_another_orders_prepare(client, telegram, order):
    client.post("/api/orders", json=order)
    prepared = prepare(client)
    assert complete(client, prepared, merchant_trans_id="2")["error"] == -6
    assert prepare(client, merchant_trans_id="2")["error"] == -8  # same Click payment


def test_click_duplicates_are_ignored(client, telegram):
    prepared = prepare(client)
    assert prepare(client) == prepared
    assert complete(client, prepared)["error"] == 0
    for _ in range(3):
        assert complete(client, prepared)["error"] == -4
        assert prepare(client)["error"] == -4
        assert prepare(client, click_trans_id="90002")["error"] == -4
    assert_paid_once(client, telegram, "Click")


def test_click_second_payment_for_a_paid_order_is_returned(client, telegram):
    """-4 would tell Click that this charge stands too."""
    first = prepare(client)
    second = prepare(client, click_trans_id="90002")
    assert complete(client, first)["error"] == 0
    for _ in range(2):
        assert complete(client, second, click_trans_id="90002")["error"] == -9
    assert complete(client, first)["error"] == -4
    assert complete(client, first, error="-5017")["error"] == -4  # too late to cancel it
    assert_paid_once(client, telegram, "Click")


def test_click_simultaneous_payments_for_one_order(client, telegram):
    first = prepare(client)
    second = prepare(client, click_trans_id="90002")
    with ThreadPoolExecutor(2) as pool:
        answers = [
            pool.submit(complete, client, first),
            pool.submit(complete, client, second, click_trans_id="90002"),
        ]
    assert sorted(a.result()["error"] for a in answers) == [-9, 0]
    assert_paid_once(client, telegram, "Click")


def test_click_simultaneous_duplicates_pay_once(client, telegram):
    prepared = prepare(client)
    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(lambda _: complete(client, prepared)["error"], range(8)))
    assert sorted(codes) == [-4] * 7 + [0]
    assert_paid_once(client, telegram, "Click")


def test_click_failed_charge_is_cancelled(client, telegram):
    prepared = prepare(client)
    assert complete(client, prepared, error="-5017")["error"] == -9
    assert complete(client, prepared)["error"] == -9  # and can't be revived
    assert prepare(client)["error"] == -9
    assert_untouched(client, telegram)
    retry = prepare(client, click_trans_id="90002")
    assert complete(client, retry, click_trans_id="90002")["error"] == 0


def test_click_refuses_an_expired_order_before_charging(client, telegram):
    expire(client)
    assert prepare(client)["error"] == -9


def test_click_payment_started_in_time_is_honoured(client, telegram):
    prepared = prepare(client)
    expire(client)
    telegram.requests.clear()
    assert complete(client, prepared)["error"] == 0
    assert_paid_once(client, telegram, "Click")


def test_click_late_payment_for_sold_items_is_returned(client, telegram, sold_out_late):
    prepared = prepare(client)
    sold_out_late()
    telegram.requests.clear()
    assert complete(client, prepared)["error"] == -9
    assert complete(client, prepared)["error"] == -9
    assert read(client)["status"] == "cancelled_expired"
    [warning] = telegram.payloads
    assert warning["chat_id"] == ADMIN
    assert warning["text"] == (
        "⚠️ PAYMENT REFUSED #1 | The order had expired and its items have sold since, "
        "so Click was told to return the $184.98."
    )


# --- Payme ----------------------------------------------------------------------


def auth(key=KEY, login="Paycom") -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"{login}:{key}".encode()).decode()}


def rpc(client, method, params, headers=None):
    response = client.post(
        "/api/webhooks/payme",
        content=json.dumps({"method": method, "params": params, "id": 7}),
        headers={"Content-Type": "text/json", "X-API-Key": "", **(headers or auth())},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == 7 and ("result" in body) != ("error" in body)
    return body


def result(client, method, **params):
    body = rpc(client, method, params)
    assert "result" in body, body
    return body["result"]


def error(client, method, **params) -> int:
    body = rpc(client, method, params)
    assert "error" in body, body
    assert set(body["error"]["message"]) == {"ru", "uz", "en"}
    return body["error"]["code"]


ID = "64f0c5e1a2b3c4d5e6f70001"
TIME = 1764000000000
PAY = {"amount": TIYIN, "account": {"order_id": "1"}}
NEW = {"id": ID, "time": TIME, **PAY}


def test_payme_payment(client, telegram):
    assert result(client, "CheckPerformTransaction", **PAY) == {"allow": True}
    created = result(client, "CreateTransaction", **NEW)
    assert created["state"] == 1 and isinstance(created["transaction"], str)
    assert len(str(created["create_time"])) == 13
    assert_untouched(client, telegram)

    done = result(client, "PerformTransaction", id=ID)
    assert done["state"] == 2 and done["transaction"] == created["transaction"]
    assert len(str(done["perform_time"])) == 13
    assert_paid_once(client, telegram, "Payme")
    assert result(client, "CheckTransaction", id=ID) == {
        "create_time": created["create_time"],
        "perform_time": done["perform_time"],
        "cancel_time": 0,
        "transaction": created["transaction"],
        "state": 2,
        "reason": None,
    }


def test_payme_answers_as_it_must(client):
    response = client.post("/api/webhooks/payme", content="{}", headers=auth())
    assert response.headers["content-type"] == "text/json; charset=UTF-8"


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": ""},
        {"Authorization": "Basic"},
        {"Authorization": "Basic !!!"},
        {"Authorization": "Bearer " + base64.b64encode(f"Paycom:{KEY}".encode()).decode()},
        {"Authorization": "Basic é".encode()},
        auth(key="wrong"),
        auth(key=""),
        auth(login="someone"),
    ],
)
def test_payme_key_is_required(client, telegram, headers):
    body = rpc(client, "CreateTransaction", NEW, headers)
    assert body["error"]["code"] == -32504
    assert error(client, "CheckTransaction", id=ID) == -31003  # nothing was stored
    assert_untouched(client, telegram)


@pytest.mark.parametrize(
    "body, code",
    [
        ("not json", -32700),
        ("", -32700),
        ("[]", -32600),
        ('"text"', -32600),
        ("{}", -32600),
        ('{"method": 5, "params": {}}', -32600),
        ('{"method": "CheckTransaction"}', -32600),
        ('{"method": "CheckTransaction", "params": {"id": 5}}', -32600),
        ('{"method": "CheckTransaction", "params": []}', -32600),
        ('{"method": "ChangePassword", "params": {}}', -32601),
    ],
)
def test_payme_malformed_requests(client, body, code):
    response = client.post("/api/webhooks/payme", content=body, headers=auth())
    assert response.status_code == 200
    assert response.json()["error"]["code"] == code


@pytest.mark.parametrize(
    "change, code",
    [
        ({"amount": TIYIN - 1}, -31001),
        ({"amount": 18498}, -31001),
        ({"amount": 0}, -31001),
        ({"account": {"order_id": "99"}}, -31050),
        ({"account": {"order_id": "1 OR 1=1"}}, -31050),
        ({"account": {"order_id": None}}, -31050),
        ({"account": {"order_id": "9" * 40}}, -31050),
        ({"account": {}}, -31050),
    ],
)
@pytest.mark.parametrize("method", ["CheckPerformTransaction", "CreateTransaction"])
def test_payme_refuses_bad_payments(client, telegram, method, change, code):
    body = rpc(client, method, {**NEW, **change})
    assert body["error"]["code"] == code
    if code == -31050:
        assert body["error"]["data"] == "order_id"
    assert_untouched(client, telegram)


def test_payme_duplicates_get_the_same_answer(client, telegram):
    created = result(client, "CreateTransaction", **NEW)
    assert result(client, "CreateTransaction", **NEW) == created
    done = result(client, "PerformTransaction", id=ID)
    for _ in range(3):
        assert result(client, "PerformTransaction", id=ID) == done
    assert_paid_once(client, telegram, "Payme")
    assert error(client, "CheckPerformTransaction", **PAY) == -31051
    assert error(client, "CreateTransaction", **{**NEW, "id": "another"}) == -31051


def test_payme_simultaneous_duplicates_pay_once(client, telegram):
    with ThreadPoolExecutor(8) as pool:
        created = list(pool.map(lambda _: result(client, "CreateTransaction", **NEW), range(8)))
        assert all(c == created[0] for c in created)
        done = list(pool.map(lambda _: result(client, "PerformTransaction", id=ID), range(8)))
    assert all(d == done[0] for d in done)
    assert_paid_once(client, telegram, "Payme")


def test_identical_requests_at_once_start_one_payment(client):
    """Both looked for an earlier one before either had saved it."""
    engine = client.app.state.engine
    with Session(engine) as one, Session(engine) as other:
        first = start(one, "payme", ID, 1, TIYIN)
        second = start(other, "payme", ID, 1, TIYIN)
        assert first.id == second.id


def test_payme_one_payment_at_a_time(client):
    result(client, "CreateTransaction", **NEW)
    assert error(client, "CreateTransaction", **{**NEW, "id": "second"}) == -31053
    result(client, "CancelTransaction", id=ID, reason=3)
    assert result(client, "CreateTransaction", **{**NEW, "id": "second"})["state"] == 1


def test_payme_cancel(client, telegram):
    result(client, "CreateTransaction", **NEW)
    cancelled = result(client, "CancelTransaction", id=ID, reason=3)
    assert cancelled["state"] == -1 and cancelled["cancel_time"] > 0
    assert result(client, "CancelTransaction", id=ID, reason=5) == cancelled
    assert result(client, "CheckTransaction", id=ID)["reason"] == 3
    assert error(client, "PerformTransaction", id=ID) == -31008
    assert error(client, "CreateTransaction", **NEW) == -31008
    assert_untouched(client, telegram)


def test_payme_cannot_cancel_a_paid_order(client, telegram):
    result(client, "CreateTransaction", **NEW)
    result(client, "PerformTransaction", id=ID)
    assert error(client, "CancelTransaction", id=ID, reason=5) == -31007
    assert_paid_once(client, telegram, "Payme")


def test_payme_unknown_transaction(client):
    for method in ("PerformTransaction", "CheckTransaction"):
        assert error(client, method, id="nope") == -31003
    assert error(client, "CancelTransaction", id="nope", reason=1) == -31003


def twelve_hours_later(client):
    with Session(client.app.state.engine) as db:
        db.execute(
            update(PaymentTransaction).values(
                created_ms=PaymentTransaction.created_ms - 12 * 3600 * 1000 - 1000
            )
        )
        db.commit()


@pytest.mark.parametrize(
    "method, params", [("PerformTransaction", {"id": ID}), ("CreateTransaction", NEW)]
)
def test_payme_payment_times_out_after_twelve_hours(client, telegram, method, params):
    result(client, "CreateTransaction", **NEW)
    twelve_hours_later(client)
    assert error(client, method, **params) == -31008
    details = result(client, "CheckTransaction", id=ID)
    assert (details["state"], details["reason"]) == (-1, 4) and details["cancel_time"] > 0
    assert_untouched(client, telegram)


def test_payme_timed_out_payment_does_not_block_the_next(client):
    result(client, "CreateTransaction", **NEW)
    twelve_hours_later(client)
    assert result(client, "CreateTransaction", **{**NEW, "id": "second"})["state"] == 1


def test_payme_statement(client, order):
    client.post("/api/orders", json=order)
    result(client, "CreateTransaction", **NEW)
    second = {**NEW, "id": "second", "time": TIME - 5, "account": {"order_id": "2"}}
    result(client, "CreateTransaction", **second)
    prepare(client, merchant_trans_id="2")  # a Click payment, which isn't Payme's business

    def ids(start, end):
        found = result(client, "GetStatement", **{"from": start, "to": end})
        return [t["id"] for t in found["transactions"]]

    assert ids(TIME - 5, TIME) == ["second", ID]
    assert ids(TIME - 4, TIME) == [ID]
    assert ids(TIME + 1, TIME + 9) == []
    assert ids(0, 10**14) == ["second", ID]  # up to now, so Click's would show
    [first, _] = result(client, "GetStatement", **{"from": 0, "to": TIME})["transactions"]
    assert first["account"] == {"order_id": "2"} and first["amount"] == TIYIN
    assert first["time"] == TIME - 5 and first["state"] == 1 and first["reason"] is None


def test_payme_refuses_an_expired_order_before_charging(client):
    expire(client)
    assert error(client, "CheckPerformTransaction", **PAY) == -31052
    assert error(client, "CreateTransaction", **NEW) == -31052


def test_payme_payment_started_in_time_is_honoured(client, telegram):
    result(client, "CreateTransaction", **NEW)
    expire(client, HOLD + timedelta(minutes=5))
    telegram.requests.clear()
    assert result(client, "PerformTransaction", id=ID)["state"] == 2
    assert_paid_once(client, telegram, "Payme")


def test_payme_late_payment_for_sold_items_is_returned(client, telegram, sold_out_late):
    result(client, "CreateTransaction", **NEW)
    sold_out_late()
    telegram.requests.clear()
    for _ in range(2):
        assert error(client, "PerformTransaction", id=ID) == -31008
    assert read(client)["status"] == "cancelled_expired"
    [warning] = telegram.to(ADMIN)
    assert warning.startswith("⚠️ PAYMENT REFUSED #1")


# --- Both -----------------------------------------------------------------------


def test_order_paid_through_both_is_only_paid_once(client, telegram):
    prepared = prepare(client)
    result(client, "CreateTransaction", **NEW)
    assert complete(client, prepared)["error"] == 0
    assert error(client, "PerformTransaction", id=ID) == -31008
    assert result(client, "CheckTransaction", id=ID)["state"] == -1
    assert_paid_once(client, telegram, "Click")


def test_click_payment_for_an_order_paid_through_payme_is_returned(client, telegram):
    prepared = prepare(client)
    result(client, "CreateTransaction", **NEW)
    result(client, "PerformTransaction", id=ID)
    for _ in range(2):
        assert complete(client, prepared)["error"] == -9
    assert_paid_once(client, telegram, "Payme")


def test_both_at_the_same_moment(client, telegram):
    prepared = prepare(client)
    result(client, "CreateTransaction", **NEW)
    with ThreadPoolExecutor(2) as pool:
        by_click = pool.submit(complete, client, prepared)
        by_payme = pool.submit(rpc, client, "PerformTransaction", {"id": ID})
    won = [by_click.result()["error"] == 0, "result" in by_payme.result()]
    assert won.count(True) == 1
    assert_paid_once(client, telegram, "Click" if won[0] else "Payme")


def test_escalated_order_can_be_paid(client, telegram):
    client.post(
        "/api/admin/escalate", json={"reason": "r", "context_summary": "s", "order_id": 1}
    )
    telegram.requests.clear()
    assert complete(client, prepare(client))["error"] == 0
    assert_paid_once(client, telegram, "Click")


@pytest.mark.parametrize(
    "missing", ["uzs_per_usd", "click_secret_key", "click_service_id", "payme_key"]
)
def test_webhooks_are_closed_until_set_up(make_client, missing):
    settings = {
        "uzs_per_usd": "12800", "click_service_id": SERVICE, "click_secret_key": SECRET,
        "payme_key": KEY,
    }
    client = make_client(**{**settings, missing: None})
    closed = {"click" if missing.startswith("click") else "payme"}
    if missing == "uzs_per_usd":
        closed = {"click", "payme"}
    for provider in ("click", "payme"):
        code = client.post(f"/api/webhooks/{provider}").status_code
        assert (code == 503) == (provider in closed)
