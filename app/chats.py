"""The Telegram chat's conversations, read for the console.

The chat is a separate program and owns its database (see chat/storage.py). This only
reads it, and treats a missing or busy file as "nothing to show" rather than an error.
"""

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from chat.formatting import to_html

log = logging.getLogger("aida.console")

THINKING = ("thinking", "redacted_thinking")


class ChatsUnavailable(Exception):
    """Safe to show."""


@dataclass
class Entry:
    """One line of a conversation as the console shows it."""

    kind: str  # customer, aida, note or tool
    text: str
    tool: str | None = None
    ok: bool | None = None
    html: str | None = None  # Aida's words as Telegram showed them


@dataclass
class ChatRow:
    chat_id: int
    username: str | None
    active_at: datetime | None
    state: str  # aida, answering or with_person
    paused_at: datetime | None
    failures: int
    waiting: int  # messages received but not answered yet
    entries: list[Entry] = field(default_factory=list)

    @property
    def last(self) -> Entry | None:
        """The last thing either side said."""
        return next((e for e in reversed(self.entries) if e.kind in ("customer", "aida")), None)


def _when(seconds: float | None) -> datetime | None:
    return datetime.fromtimestamp(seconds, UTC) if seconds else None


def _connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise ChatsUnavailable("The chat hasn't run yet, so there are no conversations.")
    try:
        db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
        db.execute("SELECT 1 FROM chats LIMIT 1")
    except sqlite3.Error as exc:
        raise ChatsUnavailable(f"The chat's conversations can't be read ({exc}).") from None
    return db


def read_chats(path: Path, limit: int = 50) -> list[ChatRow]:
    """The most recently active chats, newest first."""
    db = _connect(path)
    try:
        waiting = dict(
            db.execute(
                "SELECT chat_id, count(*) FROM inbox WHERE message IS NOT NULL GROUP BY chat_id"
            )
        )
        rows = db.execute(
            "SELECT id, username, messages, active_at, paused_at, owed, failures FROM chats"
            " ORDER BY active_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise ChatsUnavailable(f"The chat's conversations can't be read ({exc}).") from None
    finally:
        db.close()
    return [_row(row, waiting.get(row[0], 0)) for row in rows]


def read_chat(path: Path, chat_id: int) -> ChatRow | None:
    db = _connect(path)
    try:
        row = db.execute(
            "SELECT id, username, messages, active_at, paused_at, owed, failures FROM chats"
            " WHERE id = ?",
            (chat_id,),
        ).fetchone()
        (waiting,) = db.execute(
            "SELECT count(*) FROM inbox WHERE chat_id = ? AND message IS NOT NULL", (chat_id,)
        ).fetchone()
    except sqlite3.Error as exc:
        raise ChatsUnavailable(f"The chat's conversations can't be read ({exc}).") from None
    finally:
        db.close()
    return _row(row, waiting) if row else None


def _row(row: tuple, waiting: int) -> ChatRow:
    chat_id, username, messages, active_at, paused_at, owed, failures = row
    try:
        entries = entries_of(json.loads(messages))
    except (ValueError, TypeError, KeyError):
        log.warning("chat %s has a conversation the console can't read", chat_id)
        entries = [Entry("note", "This conversation couldn't be read.")]
    if paused_at:
        state = "with_person"
    elif owed or waiting:
        state = "answering"
    else:
        state = "aida"
    return ChatRow(
        chat_id=chat_id,
        username=username,
        active_at=_when(active_at),
        state=state,
        paused_at=_when(paused_at),
        failures=failures,
        waiting=waiting,
        entries=entries,
    )


def entries_of(messages: list[dict]) -> list[Entry]:
    """A conversation in Claude's format as lines: what was said, notes, and tool calls."""
    entries: list[Entry] = []
    calls: dict[str, Entry] = {}
    inputs: dict[str, tuple[str, dict]] = {}
    for message in messages:
        for block in message["content"]:
            kind = block.get("type")
            if kind == "text":
                text = block["text"].strip()
                if not text:
                    continue
                if message["role"] == "assistant":
                    entries.append(Entry("aida", text, html=to_html(text)))
                elif text.startswith("[") and text.endswith("]"):
                    entries.append(Entry("note", text[1:-1].strip()))
                else:
                    entries.append(Entry("customer", text))
            elif kind == "tool_use":
                entry = Entry("tool", describe(block["name"], block.get("input") or {}, None))
                entry.tool = block["name"]
                calls[block["id"]] = entry
                inputs[block["id"]] = (block["name"], block.get("input") or {})
                entries.append(entry)
            elif kind == "tool_result" and (entry := calls.get(block.get("tool_use_id"))):
                result = _result(block)
                name, arguments = inputs[block["tool_use_id"]]
                entry.ok = bool(result.get("ok"))
                entry.text = describe(name, arguments, result)
            # Thinking, fallbacks and anything newer aren't shown.
    return entries


def _result(block: dict) -> dict:
    content = block.get("content")
    if isinstance(content, list):  # the API also allows a list of text blocks
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    try:
        result = json.loads(content or "")
    except ValueError:
        return {"ok": not block.get("is_error")}
    return result if isinstance(result, dict) else {"ok": not block.get("is_error")}


def _money(value) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


def describe(name: str, arguments: dict, result: dict | None) -> str:
    """One line saying what a tool call did, in the store's words."""
    if result is not None and not result.get("ok"):
        error = result.get("error") or {}
        return f"{_verb(name, arguments)} failed: {error.get('message') or 'no reason given'}"
    data = (result or {}).get("data") or {}
    if name == "search_catalog":
        query = str(arguments.get("query", "")).strip()
        found = f": {data['count']} found" if "count" in data else ""
        if not query:
            return f"Listed the catalog{found}"
        return f"Searched the catalog for “{query}”{found}"
    if name == "check_stock":
        if "stock_quantity" in data:
            title = data.get("title", "a product")
            return f"Checked stock of {title}: {data['stock_quantity']} left"
        return f"Checked stock of product {arguments.get('product_id', '?')}"
    if name == "create_order":
        if "order_id" in data:
            return f"Placed order #{data['order_id']} for {_money(data.get('total_amount'))}"
        return "Placing an order"
    if name == "generate_invoice":
        provider = {"click": "Click", "payme": "Payme"}.get(
            str(arguments.get("payment_provider")), "a"
        )
        line = f"Made a {provider} payment link for order #{arguments.get('order_id', '?')}"
        if result is not None:
            sent = data.get("sent_to_customer")
            line += ", sent to the chat" if sent else ", not sent to the chat"
        return line
    if name == "get_order_status":
        if "status" in data:
            status = STATUS_WORDS.get(data["status"], data["status"])
            return f"Checked order #{data.get('order_id')}: {status}"
        return f"Checking order #{arguments.get('order_id', '?')}"
    if name == "notify_admin_order":
        line = f"Sent order #{arguments.get('order_id', '?')} to the admin"
        if result is not None and not data.get("delivered", True):
            line += " (dry run, not delivered)"
        return line
    if name == "escalate_to_human":
        line = f"Handed over to a person: {arguments.get('reason', 'no reason given')}"
        if result is not None and not data.get("delivered", True):
            line += " (dry run, not delivered)"
        return line
    return f"Used {name}"


def _verb(name: str, arguments: dict) -> str:
    return {
        "search_catalog": "Catalog search",
        "check_stock": "Stock check",
        "create_order": "Order",
        "generate_invoice": "Payment link",
        "get_order_status": f"Status check of order #{arguments.get('order_id', '?')}",
        "notify_admin_order": "Message to the admin",
        "escalate_to_human": "Hand-over",
    }.get(name, name)


STATUS_WORDS = {
    "pending": "on hold, not paid",
    "paid": "paid",
    "escalated": "with a person",
    "cancelled_expired": "expired unpaid",
}
