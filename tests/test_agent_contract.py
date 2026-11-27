"""Every tool in Aida's prompt must work against the real API."""

import re

import httpx2
import pytest

from agent.prompts import chat_prompt, voice_prompt
from agent.tools import _SPEC, StoreTools, live_definitions
from app.lifecycle import HOLD
from app.schemas import OrderStatusOut

PROMPT = chat_prompt("Test Store", "Kim")
VOICE_PROMPT = voice_prompt("Test Store", "Kim")
BOTH = pytest.mark.parametrize(
    "prompt", [PROMPT, VOICE_PROMPT], ids=["chat", "voice"]
)


@pytest.fixture
def tools(client):
    return StoreTools(
        client=client,
        customer_telegram_id="123456789",
        customer_telegram_username="dilnoza_k",
    )


@pytest.mark.parametrize(
    "prompt, definitions",
    [(PROMPT, StoreTools.definitions), (VOICE_PROMPT, live_definitions())],
    ids=["chat", "voice"],
)
def test_prompt_and_schemas_agree(prompt, definitions):
    in_prompt = {
        name: {
            part.split(":")[0].strip(" ?"): "?" in part.split(":")[0]
            for part in signature.split(", ")
        }
        for name, signature in re.findall(r"^- `(\w+)\((.*)\)`:", prompt, re.MULTILINE)
    }
    in_schema = {
        tool["name"]: {
            field: field not in tool["input_schema"]["required"]
            for field in tool["input_schema"]["properties"]
        }
        for tool in definitions
    }
    assert in_prompt == in_schema


@BOTH
def test_prompts_are_filled_in_and_include_the_guardrails(prompt):
    assert "[Store Name]" not in prompt and "[Admin Name]" not in prompt
    assert "Test Store" in prompt
    for line in (
        "I've reserved [Item Name] for you for 15 minutes while you complete payment via the "
        "link in our Telegram chat.",
        "We're currently out of stock on [Item Name], but we have [Alternative Item] available "
        "for [Price]. Would you like me to reserve that instead?",
        "I missed that—could you repeat the item name?",
        "The line sounds a bit noisy. Could you type your selection in our Telegram chat, or "
        "shall I connect you to our manager?",
        "Repeated low transcription confidence",
        "What color or size are you looking for?",
        "Thank you! We've received your payment for order #[Order_ID]. Our warehouse is packing "
        "your item now.",
        "Your invoice is still processing. Once confirmed, you'll receive a confirmation receipt "
        "right here in Telegram!",
        "I've passed your order notes to Kim. They are stepping into this chat right now.",
    ):
        assert prompt.count(line) == 1, line


@BOTH
def test_prompts_do_not_contradict_the_guardrails(prompt):
    assert "2 consecutive" not in prompt and "which item you're looking for" not in prompt
    assert "Let me connect you directly" not in prompt


def test_prompts_match_how_the_store_behaves():
    minutes = int(HOLD.total_seconds() // 60)
    for text in (PROMPT, str(StoreTools.definitions)):
        found = set(re.findall(r"(\d+)[- ]minute", text))
        assert found == {str(minutes)}
    statuses = set(OrderStatusOut.model_fields["status"].annotation.__args__)
    [tool] = [t for t in StoreTools.definitions if t["name"] == "get_order_status"]
    for status in statuses:
        assert f"`{status}`" in PROMPT and status in tool["description"]


def test_order_status_is_only_for_the_customers_own_orders(client, tools, order):
    assert tools.call("get_order_status", {"order_id": "1"})["data"]["status"] == "pending"
    stranger = StoreTools(client=client, customer_telegram_id="42")
    unknown = StoreTools(client=client, customer_phone="+998901234567", live=True)
    forged = {"order_id": "1", "customer_telegram_id": "123456789"}
    for other in (stranger, unknown):
        assert other.call("get_order_status", forged)["error"]["code"] == "order_not_found"


def test_hand_over_carries_the_customers_last_words(tools, telegram):
    tools.customer_said("Do you have mugs?")
    tools.customer_said("Never mind, get me a person")
    tools.call("escalate_to_human", {"reason": "r", "context_summary": "s"})
    assert "💬 Last words: “Never mind, get me a person”" in telegram.last_text
    assert "mugs" not in telegram.last_text


def test_hand_over_survives_very_long_words(tools, telegram):
    tools.customer_said("x" * 5000)
    assert tools.call("escalate_to_human", {"reason": "r", "context_summary": "s"})["ok"]
    assert "💬 Last words: “xxx" in telegram.last_text


def test_voice_tools_leave_identity_to_the_session():
    names = [tool["name"] for tool in live_definitions()]
    assert names == [tool["name"] for tool in StoreTools.definitions]
    assert "customer_" not in "".join(
        field
        for tool in live_definitions()
        for field in tool["input_schema"]["properties"]
        if field not in ("customer_name", "customer_summary")
    )
    # Building them must not change the chat agent's.
    assert "customer_telegram_id" in str(StoreTools.definitions)


def test_call_tools_send_the_link_and_the_callers_phone(client, telegram, order):
    tools = StoreTools(client=client, customer_phone="+998901234567", live=True)
    made = tools.call("generate_invoice", {"order_id": "1", "payment_provider": "click"})
    assert made["data"]["sent_to_customer"] is True
    tools.call("escalate_to_human", {"reason": "Upset", "context_summary": "Refund"})
    assert "+998901234567" in telegram.last_text


def test_chat_tools_leave_sending_the_link_to_the_agent(client, tools, telegram, order):
    made = tools.call("generate_invoice", {"order_id": "1", "payment_provider": "click"})
    assert made["data"]["sent_to_customer"] is False and telegram.requests == []


def test_every_tool_maps_to_a_real_endpoint(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert set(_SPEC["endpoints"]) == {tool["name"] for tool in StoreTools.definitions}
    for endpoint in _SPEC["endpoints"].values():
        assert endpoint["method"].lower() in paths[endpoint["path"]]


def test_full_call(client, tools, telegram):
    found = tools.call("search_catalog", {"query": "earbuds"})["data"]["products"][0]
    product_id = str(found["id"])
    assert tools.call("check_stock", {"product_id": product_id})["data"]["in_stock"]

    order = tools.call(
        "create_order",
        {
            "customer_telegram_id": "123456789",
            "customer_name": "Dilnoza Karimova",
            "delivery_address": "12 Amir Temur Street, Tashkent",
            "items": [{"product_id": product_id, "quantity": 1}],
        },
    )["data"]
    order_id = str(order["order_id"])
    link = tools.call("generate_invoice", {"order_id": order_id, "payment_provider": "payme"})[
        "data"
    ]["payment_url"]
    sent = tools.call(
        "notify_admin_order",
        {"order_id": order_id, "customer_summary": "Call first.", "payment_link": link},
    )
    assert sent["ok"] and sent["data"]["delivered"]
    assert link in telegram.last_text and "$49.99 via Payme" in telegram.last_text

    handoff = tools.call(
        "escalate_to_human",
        {"reason": "Asked for a person", "context_summary": "Bulk order.", "order_id": order_id},
    )
    assert handoff["data"]["pause_ai"] and "Bulk order." in telegram.last_text


def test_session_identity_beats_what_the_caller_says(tools, telegram):
    order = tools.call(
        "create_order",
        {
            "customer_telegram_id": "999",
            "customer_telegram_username": "someone_else",
            "customer_name": "X",
            "delivery_address": "Y",
            "items": [{"product_id": "2", "quantity": 1}],
        },
    )["data"]
    assert order["customer_telegram_id"] == "123456789"
    assert order["customer_telegram_username"] == "dilnoza_k"

    tools.call("escalate_to_human", {"reason": "r", "context_summary": "s"})
    assert "@dilnoza_k" in telegram.last_text


def test_failures_come_back_as_data(client, tools):
    assert tools.call("make_coffee", {})["error"]["code"] == "unknown_tool"
    assert tools.call("check_stock", {"product_id": "999"})["error"]["code"] == "product_not_found"
    assert tools.call("check_stock", {})["error"]["code"] == "missing_argument"

    short = tools.call(
        "create_order",
        {
            "customer_name": "X",
            "delivery_address": "Y",
            "items": [{"product_id": "8", "quantity": 9}],
        },
    )
    assert short["status"] == 409 and short["error"]["items"][0]["available"] == 3

    invalid = tools.call("generate_invoice", {"order_id": "1", "payment_provider": "cash"})
    assert invalid["error"]["code"] == "invalid_request"
    assert "provider" in invalid["error"]["message"]


@pytest.mark.parametrize("product_id", ["../../orders/1", "1/../../orders", "..", "."])
def test_ids_cannot_reach_other_endpoints(client, tools, order, product_id):
    result = tools.call("check_stock", {"product_id": product_id})
    assert result["ok"] is False
    assert "customer_name" not in str(result)


def test_backend_down_is_not_an_exception():
    def refuse(request):
        raise httpx2.ConnectError("refused")

    down = httpx2.Client(base_url="http://store", transport=httpx2.MockTransport(refuse))
    result = StoreTools(client=down).call("search_catalog", {"query": "mug"})
    assert result["error"]["code"] == "backend_unreachable"


def test_api_key_is_sent(make_client):
    client = make_client(agent_api_key="s3cret")
    assert StoreTools(client=client).call("search_catalog", {"query": ""})["status"] == 401
    assert StoreTools(client=client, api_key="s3cret").call("search_catalog", {"query": ""})["ok"]
