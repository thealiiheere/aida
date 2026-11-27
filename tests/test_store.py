from concurrent.futures import ThreadPoolExecutor

import pytest

from .conftest import invoice, stock


def titles(client, **params):
    response = client.get("/api/products/search", params=params)
    assert response.status_code == 200, response.text
    return [p["title"] for p in response.json()["products"]]


# --- Catalog ----------------------------------------------------------------


def test_seed_matches_the_spec_examples(client):
    products = client.get("/api/products/search").json()["products"]
    prices = {p["title"]: p["price"] for p in products}
    assert 5 <= len(products) <= 10
    assert prices["Wireless Earbuds"] == 49.99
    assert prices["Coffee Beans"] == 18.00
    assert prices["Leather Backpack"] == 85.00


@pytest.mark.parametrize(
    "query, first",
    [
        ("earbuds", "Wireless Earbuds"),
        ("EARBUD", "Wireless Earbuds"),
        ("do you have any wireless earbuds?", "Wireless Earbuds"),
        ("headphones", "Wireless Earbuds"),
        ("backpacks", "Leather Backpack"),
        ("t-shirt", "Cotton T-Shirt"),
        ("leather wallet", "Leather Backpack"),
    ],
)
def test_search_finds(client, query, first):
    assert titles(client, q=query)[0] == first


@pytest.mark.parametrize("query", ["spaceship", "'; DROP TABLE products; --"])
def test_search_without_matches(client, query):
    assert titles(client, q=query) == []


@pytest.mark.parametrize("query", ["", "  ", "%", "_", "what do you have?"])
def test_search_without_search_terms_lists_the_catalog(client, query):
    assert len(titles(client, q=query)) == 8


def test_search_filters_by_category(client):
    assert titles(client, category="electronics") == ["Wireless Earbuds", "Smart Watch"]
    assert titles(client, q="light", category="Electronics") == []


def test_blank_search_puts_out_of_stock_last(client):
    assert titles(client, q="")[-1] == "Desk Lamp"


def test_stock(client):
    body = client.get("/api/products/1/stock").json()
    assert (body["stock_quantity"], body["in_stock"], body["note"]) == (25, True, None)
    assert client.get("/api/products/7/stock").json()["in_stock"] is False


def test_stock_does_not_claim_to_know_variants(client):
    body = client.get("/api/products/6/stock", params={"variant": "navy"}).json()
    assert body["variant"] == "navy" and "per product" in body["note"]


def test_unknown_product(client):
    response = client.get("/api/products/999/stock")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "product_not_found"


# --- Orders -----------------------------------------------------------------


def test_order_uses_catalog_prices_and_reserves_stock(client, order):
    assert order["status"] == "pending"
    assert order["total_amount"] == 184.98  # 2 x 49.99 + 85.00
    assert [i["line_total"] for i in order["items"]] == [99.98, 85.00]
    assert order["items"][1]["variant"] == "brown"
    assert order["created_at"].endswith("Z")
    assert (stock(client, 1), stock(client, 3)) == (23, 11)


def test_order_can_be_read_back(client, order):
    assert client.get(f"/api/orders/{order['order_id']}").json() == order
    assert client.get("/api/orders/999").status_code == 404


@pytest.mark.parametrize("field", ["unit_price", "price", "discount"])
def test_agent_cannot_set_prices(client, order_body, field):
    order_body["items"][0][field] = 0.01
    assert client.post("/api/orders", json=order_body).status_code == 422


@pytest.mark.parametrize(
    "change",
    [
        {"items": []},
        {"items": [{"product_id": 1, "quantity": 0}]},
        {"items": [{"product_id": 1, "quantity": -5}]},
        {"customer_name": "   "},
        {"delivery_address": ""},
        {"customer_telegram_id": "@someone"},
        {"customer_telegram_username": "no spaces allowed"},
        {"total_amount": 1},
    ],
)
def test_bad_orders_are_rejected(client, order_body, change):
    assert client.post("/api/orders", json=order_body | change).status_code == 422
    assert stock(client, 1) == 25


def test_ids_may_be_numbers_or_strings(client, order_body):
    order_body["customer_telegram_id"] = 123456789
    order_body["customer_telegram_username"] = "@dilnoza_k"
    order_body["items"] = [{"product_id": "2", "quantity": 1}]
    body = client.post("/api/orders", json=order_body).json()
    assert body["customer_telegram_id"] == "123456789"
    assert body["customer_telegram_username"] == "dilnoza_k"


def test_unknown_product_in_order(client, order_body):
    order_body["items"].append({"product_id": 999, "quantity": 1})
    response = client.post("/api/orders", json=order_body)
    assert response.status_code == 404
    assert response.json()["detail"]["product_ids"] == [999]
    assert stock(client, 1) == 25


def test_shortage_reserves_nothing(client, order_body):
    order_body["items"] = [
        {"product_id": 1, "quantity": 2},
        {"product_id": 7, "quantity": 1},
        {"product_id": 8, "quantity": 4},
    ]
    response = client.post("/api/orders", json=order_body)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "insufficient_stock"
    assert [(i["title"], i["available"]) for i in detail["items"]] == [
        ("Desk Lamp", 0),
        ("Yoga Mat", 3),
    ]
    assert (stock(client, 1), stock(client, 8)) == (25, 3)
    assert client.get("/api/orders/1").status_code == 404


def test_same_product_twice_counts_together(client, order_body):
    order_body["items"] = [
        {"product_id": 8, "quantity": 2, "variant": "blue"},
        {"product_id": 8, "quantity": 2, "variant": "green"},
    ]
    assert client.post("/api/orders", json=order_body).status_code == 409
    order_body["items"][1]["quantity"] = 1
    assert client.post("/api/orders", json=order_body).status_code == 201
    assert stock(client, 8) == 0


def test_simultaneous_orders_never_oversell(client, order_body):
    order_body["items"] = [{"product_id": 8, "quantity": 1}]  # 3 in stock
    bodies = [{**order_body, "customer_telegram_id": str(i + 1)} for i in range(20)]
    with ThreadPoolExecutor(20) as pool:
        codes = list(
            pool.map(lambda body: client.post("/api/orders", json=body).status_code, bodies)
        )
    assert sorted(set(codes)) == [201, 409]
    assert codes.count(201) == 3
    assert stock(client, 8) == 0


# --- Payments ---------------------------------------------------------------


@pytest.mark.parametrize("provider", ["click", "payme"])
def test_invoice(client, order, provider):
    body = invoice(client, order["order_id"], provider)
    assert body["amount"] == 184.98 and body["status"] == "unpaid"
    assert body["payment_url"] == f"http://localhost:8000/pay/{body['invoice_id']}"


def test_retried_invoice_returns_the_same_link(client, order):
    first = invoice(client, order["order_id"], "click")
    assert invoice(client, order["order_id"], "click") == first
    assert invoice(client, order["order_id"], "payme") != first


def test_simultaneous_invoices_share_one_link(client, order):
    with ThreadPoolExecutor(10) as pool:
        links = set(
            pool.map(
                lambda _: invoice(client, order["order_id"], "click")["payment_url"], range(5)
            )
        )
    assert len(links) == 1


def test_invoice_errors(client, order):
    def post(**body):
        return client.post("/api/payments/create-invoice", json=body).status_code

    assert post(order_id=999, provider="click") == 404
    assert post(order_id=order["order_id"], provider="stripe") == 422
    assert post(order_id=order["order_id"], provider="click", amount=1) == 422


def test_checkout_marks_the_order_paid(client, order):
    link = f"/pay/{invoice(client, order['order_id'])['invoice_id']}"
    page = client.get(link)
    assert page.status_code == 200 and "$184.98" in page.text and "Pay with Click" in page.text
    assert client.get(f"/api/orders/{order['order_id']}").json()["status"] == "pending"

    paid = client.post(link)  # follows the redirect back to the page
    assert paid.status_code == 200 and "This order is paid" in paid.text
    body = client.get(f"/api/orders/{order['order_id']}").json()
    assert body["status"] == "paid" and body["invoices"][0]["status"] == "paid"


def test_an_order_is_only_paid_once(client, order):
    click = invoice(client, order["order_id"], "click")["invoice_id"]
    payme = invoice(client, order["order_id"], "payme")["invoice_id"]
    client.post(f"/pay/{click}")
    assert "This order is paid" in client.post(f"/pay/{payme}").text
    body = client.get(f"/api/orders/{order['order_id']}").json()
    assert {i["provider"]: i["status"] for i in body["invoices"]} == {
        "click": "paid",
        "payme": "unpaid",
    }
    response = client.post(
        "/api/payments/create-invoice", json={"order_id": order["order_id"], "provider": "click"}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "already_paid"


def test_unknown_payment_link(client):
    assert client.get("/pay/inv_nope").status_code == 404
    assert client.post("/pay/inv_nope").status_code == 404


def test_checkout_escapes_the_store_name(make_client, order_body):
    client = make_client(store_name="<script>alert(1)</script>")
    client.post("/api/orders", json=order_body)
    page = client.get(f"/pay/{invoice(client, 1)['invoice_id']}").text
    assert "<script>" not in page


# --- Access -----------------------------------------------------------------


def test_api_key_is_enforced_when_set(make_client, order_body):
    client = make_client(agent_api_key="s3cret")
    calls = [
        ("GET", "/api/products/search", None),
        ("GET", "/api/products/1/stock", None),
        ("POST", "/api/orders", order_body),
        ("GET", "/api/orders/1", None),
        ("POST", "/api/payments/create-invoice", {"order_id": 1, "provider": "click"}),
        ("POST", "/api/admin/notify-order", {"order_id": 1}),
        ("POST", "/api/admin/escalate", {"reason": "r", "context_summary": "s"}),
    ]
    for method, path, body in calls:
        for headers in ({}, {"X-API-Key": "wrong"}, {"X-API-Key": "é".encode()}):
            response = client.request(method, path, json=body, headers=headers)
            assert response.status_code == 401, (method, path, headers)

    key = {"X-API-Key": "s3cret"}
    assert client.post("/api/orders", json=order_body, headers=key).status_code == 201
    link = client.post(
        "/api/payments/create-invoice", json={"order_id": 1, "provider": "click"}, headers=key
    ).json()["invoice_id"]
    assert client.get(f"/pay/{link}").status_code == 200  # customers have no key


def test_docs(client):
    assert client.get("/docs").status_code == 200
    assert client.get("/", follow_redirects=False).headers["location"] == "/console"
