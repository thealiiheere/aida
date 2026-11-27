"""The chat's parts on their own: formatting, reading messages, storage, Telegram."""

import asyncio
import json

import httpx2
import pytest

from chat.formatting import MAX_CHARS, split, to_html
from chat.incoming import Incoming, read
from chat.storage import Chat, Storage
from chat.telegram import BotApi, TelegramError

from .chat_world import FakeBotApi, failure
from .conftest import TOKEN

# --- Formatting -----------------------------------------------------------------


@pytest.mark.parametrize(
    "markdown, html",
    [
        ("Plain text.", "Plain text."),
        ("a < b & c > d", "a &lt; b &amp; c &gt; d"),
        ("The **mug** is *great*", "The <b>mug</b> is <i>great</i>"),
        ("__Bold__ and _slanted_", "<b>Bold</b> and <i>slanted</i>"),
        ("***Both***", "<b><i>Both</i></b>"),
        ("~~$50~~ $40", "<s>$50</s> $40"),
        ("## Order\n- one\n* two", "<b>Order</b>\n• one\n• two"),
        ("2 * 3 * 4 = 24", "2 * 3 * 4 = 24"),
        ("snake_case_name", "snake_case_name"),
        ("a ** b ** c", "a ** b ** c"),
        ("`a < b`", "<code>a &lt; b</code>"),
        ("```py\nx = 1\n```", "<pre>x = 1</pre>"),
        ("[Pay](https://x.example/?a=1&b=2)", '<a href="https://x.example/?a=1&amp;b=2">Pay</a>'),
        ("[Pay](javascript:alert(1))", "[Pay](javascript:alert(1))"),
        ('[x](https://x.example/"onclick=y)', '<a href="https://x.example/&quot;onclick=y">x</a>'),
        ("**https://x.example/inv_a_b_c_**!", "<b>https://x.example/inv_a_b_c_</b>!"),
        ("<b>not mine</b>", "&lt;b&gt;not mine&lt;/b&gt;"),
        ("0 hi", "0 hi"),
        ("Салом! **Narxi** 12 so‘m", "Салом! <b>Narxi</b> 12 so‘m"),
    ],
)
def test_markdown_becomes_telegram_html(markdown, html):
    assert to_html(markdown) == html


def test_long_messages_are_split_where_it_reads_well():
    assert split("  short \n") == ["short"] and split(" \n ") == []
    paragraphs = "\n\n".join(["word " * 180] * 4)
    pieces = split(paragraphs)
    assert len(pieces) == 2 and all(len(p) <= MAX_CHARS for p in pieces)
    assert " ".join(pieces).split() == paragraphs.split()
    assert pieces[0].endswith("word") and len(pieces[0].split("\n\n")) == 2
    solid = "😀" * (2 * MAX_CHARS + 1)
    assert "".join(split(solid)) == solid
    assert max(len(p.encode("utf-16-le")) // 2 for p in split(solid)) <= 4096


# --- Reading messages -------------------------------------------------------------


def test_text_is_read_without_brackets():
    assert read({"text": " Two mugs [note] ［x］【y】 "}) == Incoming("Two mugs (note) (x)(y)")


@pytest.mark.parametrize("text", ["/start", "/start@aida_bot", "/start ref_1", "/start\nhi"])
def test_start(text):
    assert read({"text": text}) == Incoming(start=True)


def test_words_that_only_begin_like_start():
    assert read({"text": "/started"}).start is False


def test_very_long_text_is_cut():
    assert read({"text": "x" * 4096}).text == "x" * 2000 + "…"


@pytest.mark.parametrize(
    "content, note",
    [
        ({"photo": [{}]}, "The customer sent a photo. You can't open it"),
        ({"voice": {}}, "The customer sent a voice message. You can't open it"),
        ({"sticker": {"emoji": "👍"}}, "The customer sent a sticker. You can't open it"),
        ({"document": {}, "animation": {}}, "The customer sent a GIF. You can't open it"),
        (
            {"location": {"latitude": 41.31, "longitude": 69.24}},
            "The customer shared a location: 41.31, 69.24",
        ),
        (
            {
                "location": {"latitude": 41.31, "longitude": 69.24},
                "venue": {
                    "location": {"latitude": 41.31, "longitude": 69.24},
                    "title": "Cafe]\n[Free",
                    "address": "Amir Temur 1",
                },
            },
            "The customer shared a place: Cafe) (Free, Amir Temur 1 (41.31, 69.24)",
        ),
    ],
)
def test_what_aida_cannot_read_is_described(content, note):
    assert read(content) == Incoming("", (note,))


def test_captions_are_read():
    assert read({"photo": [{}], "caption": "this one?"}).text == "this one?"


def test_quoted_messages_give_context():
    earlier = {"text": "Your reservation for order #3\nhas expired [x]."}
    message = read({"text": "why?", "reply_to_message": earlier})
    assert message == Incoming(
        "why?",
        (
            "This answers an earlier message in the chat: "
            "“Your reservation for order #3 has expired (x).”",
        ),
    )
    quoted = read({"text": "why?", "reply_to_message": earlier, "quote": {"text": "order #3"}})
    assert quoted.notes[0].endswith("“order #3”")


@pytest.mark.parametrize("message", [{"pinned_message": {}}, {"text": "  "}, {}])
def test_nothing_to_answer(message):
    assert read(message) is None


# --- Storage ----------------------------------------------------------------------


@pytest.fixture
def storage(tmp_path):
    storage = Storage(tmp_path / "chat.db", clock=lambda: 1000.0)
    yield storage
    storage.close()


def test_chats_survive(storage, tmp_path):
    chat = Chat(7, "name", active_at=5.0, paused_at=6.0, owed=True, unsent="x", failures=2)
    chat.orders = "#1 paid"
    chat.conversation.add_customer("Салом")
    storage.save(chat)
    storage.close()
    again = Storage(tmp_path / "chat.db")
    loaded = again.load(7)
    assert again.load(8).conversation.messages == []
    again.close()
    assert loaded.conversation.messages == chat.conversation.messages
    for name in ("id", "username", "active_at", "paused_at", "owed", "unsent", "failures", "orders"):
        assert getattr(loaded, name) == getattr(chat, name), name


def test_messages_wait_until_handled_and_come_once(storage):
    assert storage.receive(1, 7, {"text": "a"}) and storage.receive(2, 7, {"text": "b"})
    assert not storage.receive(1, 7, {"text": "a"})
    assert storage.receive(3, 8, {"text": "c"})
    assert storage.waiting(7) == [(1, {"text": "a"}), (2, {"text": "b"})]
    assert set(storage.unfinished()) == {7, 8}
    storage.save(Chat(7), [1])
    assert storage.waiting(7) == [(2, {"text": "b"})]
    storage.handled([2, 3])
    assert storage.unfinished() == [] and storage.waiting(7) == []
    assert not storage.receive(2, 7, {"text": "b"})


def test_messages_keep_their_order_when_telegram_numbers_afresh(tmp_path):
    now = [1.0]
    storage = Storage(tmp_path / "chat.db", clock=lambda: now[0])
    storage.receive(500, 7, {"text": "first"})
    now[0] = 2.0
    storage.receive(3, 7, {"text": "second"})
    assert [message["text"] for _, message in storage.waiting(7)] == ["first", "second"]
    storage.close()


def test_unfinished_chats_are_found(storage):
    storage.save(Chat(1))
    storage.save(Chat(2, owed=True))
    storage.save(Chat(3, unsent="Hi"))
    assert set(storage.unfinished()) == {2, 3}


def test_old_records_are_forgotten_but_never_waiting_messages(tmp_path):
    path = tmp_path / "chat.db"
    storage = Storage(path, clock=lambda: 0.0)
    for update_id in (1, 2):
        storage.receive(update_id, 7, {"text": "x"})
    storage.handled([1])
    storage.remember(50, 7)
    storage.close()
    day = 24 * 3600
    for now, seen, customer in ((day, False, 7), (3 * day, True, 7), (61 * day, True, None)):
        storage = Storage(path, clock=lambda now=now: now)
        assert storage.receive(1, 7, {"text": "x"}) is seen
        storage.handled([1])
        storage._db.execute("UPDATE inbox SET received_at = 0 WHERE update_id = 1")
        storage._db.commit()
        assert storage.waiting(7) == [(2, {"text": "x"})]
        assert storage.customer_of(50) == customer
        storage.close()


# --- Telegram ---------------------------------------------------------------------


def with_api(test, telegram=None, retry_seconds=0, **kwargs):
    async def run():
        fake = telegram or FakeBotApi()
        api = BotApi(
            TOKEN, "https://api.telegram.org/", httpx2.MockTransport(fake), retry_seconds
        )
        try:
            await test(api, fake, **kwargs)
        finally:
            await api.close()

    asyncio.run(run())


def test_sending():
    async def test(api, telegram):
        message_id = await api.send(7, "The **mug**", reply_to=5)
        [sent] = telegram.sent
        assert sent == {
            "method": "sendMessage",
            "message_id": message_id,
            "chat_id": 7,
            "text": "The <b>mug</b>",
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
            "reply_parameters": {"message_id": 5, "allow_sending_without_reply": True},
        }

    with_api(test)


@pytest.mark.parametrize(
    "problem",
    [
        failure(429, "Too Many Requests: retry after 1", 0),
        failure(502, "Bad Gateway"),
        httpx2.ConnectError("down"),
        httpx2.ReadTimeout("slow"),
    ],
)
def test_sending_is_tried_again(problem):
    async def test(api, telegram):
        telegram.problems["sendMessage"] = [problem] * 3
        await api.send(7, "Hi")
        assert len(telegram.sent) == 1

    with_api(test)


def test_telegram_decides_how_long_to_wait(monkeypatch):
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    async def test(api, telegram):
        telegram.problems["sendMessage"] = [failure(429, "x", 7), failure(429, "x", 9999)]
        monkeypatch.setattr("chat.telegram.asyncio.sleep", sleep)
        await api.send(7, "Hi")
        assert waits == [7, 60]

    with_api(test)


def test_waits_grow_if_telegram_does_not_say(monkeypatch):
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    async def test(api, telegram):
        telegram.problems["sendMessage"] = [httpx2.ConnectError("down")] * 3
        monkeypatch.setattr("chat.telegram.asyncio.sleep", sleep)
        await api.send(7, "Hi")
        assert waits == [1, 2, 4]

    with_api(test, retry_seconds=1)


def test_giving_up():
    async def test(api, telegram):
        telegram.problems["sendMessage"] = [failure(403, "Forbidden: bot was blocked")]
        with pytest.raises(TelegramError, match="blocked") as caught:
            await api.send(7, "Hi")
        assert caught.value.code == 403 and not caught.value.temporary
        telegram.problems["sendMessage"] = [httpx2.ConnectError("down")] * 4
        with pytest.raises(TelegramError, match="could not reach Telegram") as caught:
            await api.send(7, "Hi")
        assert caught.value.temporary and telegram.sent == []

    with_api(test)


def test_text_telegram_cannot_parse_is_sent_as_written():
    async def test(api, telegram):
        telegram.problems["sendMessage"] = [failure(400, "Bad Request: can't parse entities: x")]
        await api.send(7, "**odd")
        [sent] = telegram.sent
        assert sent["text"] == "**odd" and "parse_mode" not in sent
        telegram.problems["sendMessage"] = [failure(400, "Bad Request: chat not found")]
        with pytest.raises(TelegramError, match="chat not found"):
            await api.send(7, "x")

    with_api(test)


def test_long_text_goes_in_pieces():
    async def test(api, telegram):
        last = await api.send(7, "word " * 900)
        assert len(telegram.sent) == 3 and telegram.sent[-1]["message_id"] == last

    with_api(test)


def test_typing_is_shown_until_done_and_never_matters(monkeypatch):
    monkeypatch.setattr("chat.telegram.TYPING_SECONDS", 0.01)

    async def test(api, telegram):
        telegram.problems["sendChatAction"] = [failure(400, "no"), httpx2.ConnectError("down")]
        async with api.typing(7):
            await asyncio.sleep(0.1)
        shown = len(telegram.typing)
        assert shown >= 2 and set(telegram.typing) == {7}
        await asyncio.sleep(0.05)
        assert len(telegram.typing) == shown

    with_api(test)


@pytest.mark.parametrize(
    "answer", [httpx2.Response(502, text="<html>"), httpx2.ConnectError(f"no /bot{TOKEN}/x")]
)
def test_token_never_shows_in_errors(answer):
    def telegram(request):
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def test(api, _):
        with pytest.raises(TelegramError) as caught:
            await api.updates(None)
        assert TOKEN not in str(caught.value) and caught.value.__cause__ is None

    with_api(test, telegram)


def test_polling_asks_only_for_messages():
    seen = []

    def telegram(request):
        seen.append(json.loads(request.content))
        return httpx2.Response(200, json={"ok": True, "result": []})

    async def test(api, _):
        assert await api.updates(None) == [] and await api.updates(8) == []

    with_api(test, telegram)
    assert seen == [
        {"timeout": 50, "allowed_updates": ["message"]},
        {"timeout": 50, "allowed_updates": ["message"], "offset": 8},
    ]
