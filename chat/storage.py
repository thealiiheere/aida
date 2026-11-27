"""Chats and unanswered messages, kept in SQLite so a restart loses neither.

Used from one thread only, in short transactions.
"""

import asyncio
import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from agent.history import Conversation

# Telegram redelivers an update for a day at most.
REMEMBER_UPDATES_SECONDS = 2 * 24 * 3600
REMEMBER_ADMIN_MESSAGES_SECONDS = 60 * 24 * 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    id INTEGER PRIMARY KEY,
    username TEXT,
    messages TEXT NOT NULL,
    active_at REAL NOT NULL,
    paused_at REAL,
    owed INTEGER NOT NULL,
    unsent TEXT NOT NULL,
    failures INTEGER NOT NULL,
    orders TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inbox (
    update_id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    message TEXT,
    received_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS inbox_waiting ON inbox (chat_id) WHERE message IS NOT NULL;
CREATE TABLE IF NOT EXISTS admin_messages (
    message_id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    sent_at REAL NOT NULL
);
"""


@dataclass
class Chat:
    """One customer's private chat with the bot."""

    id: int
    username: str | None = None
    conversation: Conversation = field(default_factory=Conversation)
    active_at: float = 0
    paused_at: float | None = None  # when a person took over from Aida
    owed: bool = False  # Aida hasn't finished answering
    unsent: str = ""  # in the conversation, but not yet in Telegram
    failures: int = 0  # times in a row that Claude didn't answer
    orders: str = ""  # what Aida was last told about the customer's orders
    # Held while the conversation is being added to.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


class Storage:
    def __init__(self, path: Path | str, clock=time.time):
        self._clock = clock
        self._db = sqlite3.connect(path)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        with self._db:
            self._forget_old()

    def close(self) -> None:
        self._db.close()

    # --- Chats ------------------------------------------------------------------

    def load(self, chat_id: int) -> Chat:
        row = self._db.execute(
            "SELECT username, messages, active_at, paused_at, owed, unsent, failures, orders"
            " FROM chats WHERE id = ?",
            (chat_id,),
        ).fetchone()
        if row is None:
            return Chat(chat_id)
        username, messages, active_at, paused_at, owed, unsent, failures, orders = row
        conversation = Conversation()
        conversation.messages = json.loads(messages)
        return Chat(
            chat_id, username, conversation, active_at, paused_at, bool(owed), unsent, failures,
            orders,
        )  # fmt: skip

    def save(self, chat: Chat, handled: list[int] = ()) -> None:
        """Save a chat, and in the same step mark the updates it now includes as handled."""
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO chats VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    chat.id,
                    chat.username,
                    json.dumps(chat.conversation.messages, ensure_ascii=False),
                    chat.active_at,
                    chat.paused_at,
                    chat.owed,
                    chat.unsent,
                    chat.failures,
                    chat.orders,
                ),
            )
            self._handled(handled)

    def unfinished(self) -> list[int]:
        """Chats a restart interrupted, or that have messages waiting."""
        rows = self._db.execute(
            "SELECT id FROM chats WHERE owed OR unsent != ''"
            " UNION SELECT chat_id FROM inbox WHERE message IS NOT NULL"
        )
        return [chat_id for (chat_id,) in rows]

    # --- Messages from Telegram ---------------------------------------------------

    def receive(self, update_id: int, chat_id: int, message: dict) -> bool:
        """Keep a message until it is handled. False if it was received before."""
        with self._db:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO inbox VALUES (?, ?, ?, ?)",
                (update_id, chat_id, json.dumps(message, ensure_ascii=False), self._clock()),
            )
        return cursor.rowcount == 1

    def waiting(self, chat_id: int) -> list[tuple[int, dict]]:
        """Messages not handled yet, oldest first."""
        rows = self._db.execute(
            "SELECT update_id, message FROM inbox WHERE chat_id = ? AND message IS NOT NULL"
            " ORDER BY received_at, update_id",
            (chat_id,),
        )
        return [(update_id, json.loads(message)) for update_id, message in rows]

    def handled(self, update_ids: list[int]) -> None:
        with self._db:
            self._handled(update_ids)

    def _handled(self, update_ids: list[int]) -> None:
        # The id stays, so that Telegram sending it again changes nothing.
        self._db.executemany(
            "UPDATE inbox SET message = NULL WHERE update_id = ?", [(i,) for i in update_ids]
        )

    # --- Messages in the admin's chat -----------------------------------------------

    def remember(self, message_id: int, chat_id: int) -> None:
        """Replies to this message in the admin's chat are for this customer."""
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO admin_messages VALUES (?, ?, ?)",
                (message_id, chat_id, self._clock()),
            )

    def customer_of(self, message_id: int) -> int | None:
        row = self._db.execute(
            "SELECT chat_id FROM admin_messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row[0] if row else None

    def _forget_old(self) -> None:
        now = self._clock()
        self._db.execute(
            "DELETE FROM inbox WHERE message IS NULL AND received_at < ?",
            (now - REMEMBER_UPDATES_SECONDS,),
        )
        self._db.execute(
            "DELETE FROM admin_messages WHERE sent_at < ?",
            (now - REMEMBER_ADMIN_MESSAGES_SECONDS,),
        )
