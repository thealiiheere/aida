"""Connects Aida's tool calls to the store backend.

    tools = StoreTools(api_key="...", customer_telegram_id="123456789")
    tools.definitions                              # give these to the model
    tools.call("search_catalog", {"query": "mug"}) # run what the model asks for
"""

import asyncio
import json
import logging
from pathlib import Path
from string import Formatter
from urllib.parse import quote

import httpx2

log = logging.getLogger("aida.tools")

_SPEC = json.loads((Path(__file__).parent / "tools.json").read_text(encoding="utf-8"))


def live_definitions() -> list[dict]:
    """The tools for a live call or chat, without the fields it fills in itself."""
    definitions = []
    for tool in _SPEC["tools"]:
        hidden = _SPEC["endpoints"][tool["name"]].get("session_fields", [])
        schema = tool["input_schema"]
        schema = {
            **schema,
            "properties": {k: v for k, v in schema["properties"].items() if k not in hidden},
            "required": [k for k in schema["required"] if k not in hidden],
        }
        definitions.append({**tool, "input_schema": schema})
    return definitions


class StoreTools:
    """One instance per call or chat, so it knows which customer it is serving.

    `live` is for Aida's own phone line and chat: the model isn't asked who the customer
    is, and payment links go straight to the customer's Telegram chat.
    """

    definitions: list[dict] = _SPEC["tools"]

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        api_key: str | None = None,
        customer_telegram_id: str | None = None,
        customer_telegram_username: str | None = None,
        customer_phone: str | None = None,
        live: bool = False,
        client: httpx2.Client | None = None,
    ):
        self._client = client or httpx2.Client(base_url=base_url, timeout=15)
        self._headers = {"X-API-Key": api_key} if api_key else {}
        self._session = {
            "customer_telegram_id": customer_telegram_id,
            "customer_telegram_username": customer_telegram_username,
            "customer_phone": customer_phone,
            "send_to_customer": live,
            "last_spoken": None,
        }
        self._live = live
        if live:
            self.definitions = live_definitions()

    def customer_said(self, text: str) -> None:
        """Remember the customer's latest words, for whoever takes over from Aida."""
        self._session["last_spoken"] = text[:1000]  # as much as the store accepts

    def call(self, name: str, arguments: dict) -> dict:
        """Run a tool. Failures come back as data the model can act on, not exceptions."""
        endpoint = _SPEC["endpoints"].get(name)
        if endpoint is None:
            return _failure(None, "unknown_tool", f"There is no tool named {name}.")

        args = dict(arguments)
        for field in endpoint.get("session_fields", []):
            # The session's identity wins, so a caller can't order as someone else.
            if self._session[field]:
                args[field] = self._session[field]
            elif self._live:
                args.pop(field, None)  # not offered, so it can only be made up
        for old, new in endpoint.get("rename", {}).items():
            if old in args:
                args[new] = args.pop(old)

        path = endpoint["path"]
        fields = [field for _, field, _, _ in Formatter().parse(path) if field]
        if missing := [field for field in fields if not str(args.get(field, "")).strip(". ")]:
            return _failure(None, "missing_argument", f"{', '.join(missing)} is required.")
        path = path.format(**{field: quote(str(args.pop(field)), safe="") for field in fields})

        payload = {"params": args} if endpoint["method"] == "GET" else {"json": args}
        try:
            response = self._client.request(
                endpoint["method"], path, headers=self._headers, **payload
            )
            body = response.json()
        except (httpx2.HTTPError, ValueError) as exc:
            return _failure(
                None, "backend_unreachable", f"The store backend failed ({type(exc).__name__})."
            )

        if response.is_success:
            return {"ok": True, "data": body}
        detail = body.get("detail")
        if isinstance(detail, dict):
            return {"ok": False, "status": response.status_code, "error": detail}
        return _failure(response.status_code, "invalid_request", _explain(detail))


async def run_tool(tools: StoreTools, name: str, arguments: dict) -> dict:
    """Run a tool without holding up other calls and chats, and without ever raising.

    A tool call that goes without a result makes every later request to Claude invalid.
    """
    try:
        result = await asyncio.to_thread(tools.call, name, arguments)
    except Exception:
        log.exception("tool %s failed", name)
        message = "Something went wrong running this tool. It may or may not have worked."
        result = _failure(None, "tool_error", message)
    log.info("tool %s: %s", name, "ok" if result["ok"] else result["error"]["code"])
    return result


def no_manager() -> dict:
    """What Claude is told when an escalation reached nobody."""
    message = "No manager could be reached. Tell the customer, and keep helping."
    return _failure(None, "no_manager", message)


def tool_result(call: dict, result: dict) -> dict:
    return {
        "type": "tool_result",
        "tool_use_id": call["id"],
        "content": json.dumps(result),
        "is_error": not result["ok"],
    }


def _failure(status: int | None, code: str, message: str) -> dict:
    return {"ok": False, "status": status, "error": {"code": code, "message": message}}


def _explain(detail) -> str:
    """Flatten FastAPI's validation errors into one sentence per problem."""
    if not isinstance(detail, list):
        return str(detail)
    return " ".join(
        f"{'.'.join(str(part) for part in error['loc'][1:])}: {error['msg']}." for error in detail
    )
