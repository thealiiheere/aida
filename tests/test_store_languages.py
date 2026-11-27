"""The store in Uzbek and Russian: Telegram messages, the checkout page and the console."""

from agent.history import Conversation
from app.i18n import RUSSIAN, UZBEK, tr
from chat.storage import Chat

from .conftest import ADMIN, CUSTOMER, invoice
from .test_console import save_chat


def send_link(client) -> dict:
    body = {"order_id": 1, "provider": "click", "send_to_customer": True}
    response = client.post("/api/payments/create-invoice", json=body)
    assert response.json()["sent_to_customer"] is True, response.text
    return response.json()


def customer_wrote(client, text: str) -> None:
    chat = Chat(int(CUSTOMER), "dilnoza_k", active_at=1_764_000_000)
    conversation = Conversation()
    conversation.add_customer(text)
    chat.conversation.messages = conversation.messages
    save_chat(client, chat)


def test_every_translation_has_the_same_placeholders():
    for table in (UZBEK, RUSSIAN):
        for english, translated in table.items():
            assert sorted(_names(english)) == sorted(_names(translated)), english
    assert tr("uz", "Order #{id}", id=7) == "Buyurtma #7"
    assert tr("de", "Order #{id}", id=7) == "Order #7"  # unknown: English
    assert tr("ru", "No such text") == "No such text"


def _names(text: str) -> list[str]:
    import string

    return [name for _, name, _, _ in string.Formatter().parse(text) if name]


def test_admin_messages_follow_the_store_language(make_client, telegram, order_body):
    client = make_client(store_language="uz")
    client.post("/api/orders", json=order_body)
    link = invoice(client, 1)
    assert client.post("/api/admin/notify-order", json={"order_id": 1}).status_code == 200
    message = telegram.to(ADMIN)[-1]
    assert message.startswith("🛍️ YANGI BUYURTMA TASDIQLANDI #1")
    assert "👤 Mijoz: Dilnoza Karimova" in message and link["payment_url"] in message


def test_customers_get_their_own_language(make_client, telegram, order_body):
    client = make_client(store_language="uz")
    client.post("/api/orders", json=order_body)
    customer_wrote(client, "Здравствуйте, сколько стоит доставка?")
    link = send_link(client)
    assert telegram.to(CUSTOMER)[-1].startswith("🛍️ Test Store: заказ #1")
    # Paying tells the customer in Russian and the admin in the store's Uzbek.
    client.post(f"/pay/{link['invoice_id']}")
    assert telegram.to(CUSTOMER)[-1].startswith("✅ Оплата получена!")
    assert telegram.to(ADMIN)[-1].startswith("💳 TO'LOV TASDIQLANDI #1")


def test_a_customer_who_never_wrote_gets_the_store_language(make_client, telegram, order_body):
    client = make_client(store_language="ru")
    client.post("/api/orders", json=order_body)
    send_link(client)
    assert telegram.to(CUSTOMER)[-1].startswith("🛍️ Test Store: заказ #1")


def test_english_stays_as_it_was(client, telegram, order):
    send_link(client)
    assert telegram.to(CUSTOMER)[-1].startswith("🛍️ Test Store: order #1")


def test_checkout_page_in_three_languages(make_client, order_body):
    client = make_client(store_language="uz")
    client.post("/api/orders", json=order_body)
    link = f"/pay/{invoice(client, 1)['invoice_id']}"
    page = client.get(link).text
    assert '<html lang="uz">' in page and "Click bilan to'lash (demo)" in page
    assert 'href="?lang=ru"' in page and "Demo to'lov sahifasi" in page
    russian = client.get(f"{link}?lang=ru").text
    assert "Оплатить через Click (демо)" in russian and "Заказ #1" in russian
    assert "Pay with Click (demo)" in client.get(f"{link}?lang=en").text
    assert "Click bilan" in client.get(f"{link}?lang=xx").text  # unknown: the store's
    # Paying keeps the chosen language on the page it comes back to.
    paid = client.post(f"{link}?lang=ru", follow_redirects=False)
    assert paid.headers["location"].endswith("?lang=ru")
    assert "Этот заказ оплачен" in client.get(f"{link}?lang=ru").text


def test_console_opens_in_the_store_language(make_client):
    client = make_client(store_language="ru", llm_provider="openai", openai_api_key="sk-x")
    assert '<html lang="ru">' in client.get("/console").text
    store = client.get("/api/console").json()["store"]
    assert (store["language"], store["ai"], store["summaries"]) == ("ru", "openai", True)
