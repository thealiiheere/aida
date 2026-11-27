"""Whole calls, with the real Claude adapter, tools and store backend behind them."""

import asyncio
import json

import pytest

from agent.tools import StoreTools
from tests.conftest import stock
from tests.fakes import Call, Fallback, Reply, Tool
from voice.session import (
    CALLBACK,
    FILLERS,
    GREETING,
    HICCUP,
    PASSED_ON,
    REFUSED,
    SILENT,
    SILENT_NOTE,
    STEPPING_IN,
    TRANSFER,
    TRANSFER_ONLY,
    RUSSIAN,
    UNCLEAR_NOTE,
    Caller,
)
from voice.stt import Speech, SttUnavailable, Utterance
from voice.tts import TtsError

HELLO = GREETING.format(store="Test Store")
ORDER = {
    "customer_name": "Dilnoza Karimova",
    "delivery_address": "12 Amir Temur Street",
    "items": [{"product_id": 1, "quantity": 2}],
}


def call_test(test):
    """Run an async test."""

    def run(client, telegram):
        asyncio.run(asyncio.wait_for(test(client, telegram), 30))

    run.__name__ = test.__name__
    return run


def texts(message):
    return [b["text"] for b in message["content"] if b["type"] == "text"]


# --- Talking ------------------------------------------------------------------


@call_test
async def test_greets_then_answers(client, telegram):
    async with Call(client, Reply("We have mugs for twelve dollars. Want one?")) as call:
        assert call.session.agent_speaking
        assert await call.hear_everything() == "".join(call.tts.spoken)
        assert " ".join(call.tts.spoken) == HELLO
        assert not call.session.agent_speaking
        await call.says("Do you have mugs?")
        await call.hear_everything()
        assert call.tts.spoken[-2:] == ["We have mugs for twelve dollars.", "Want one?"]
    assert [m["role"] for m in call.messages] == ["user", "assistant", "user", "assistant"]
    assert call.said(0) == HELLO
    assert texts(call.messages[2]) == ["Do you have mugs?"]


@call_test
async def test_audio_starts_before_claude_has_finished(client, telegram):
    more = asyncio.Event()
    async with Call(client, Reply("First sentence. ", more, "Second sentence.")) as call:
        await call.hear_everything()
        await call.says("Hi")
        assert call.tts.spoken[-1] == "First sentence."
        assert call.session.agent_speaking
        more.set()
        await call.until(lambda: call.tts.spoken[-1] == "Second sentence.")


@call_test
async def test_formatting_and_numbers_are_spoken_as_words(client, telegram):
    async with Call(client, Reply("The **mug** is $12.50. See https://x.example/p.")) as call:
        await call.hear_everything()
        await call.says("How much?")
        await call.hear_everything()
    assert call.tts.spoken[-2:] == ["The mug is twelve dollars and fifty cents.", "See the link."]


@call_test
async def test_caller_audio_reaches_speech_to_text(client, telegram):
    async with Call(client) as call:
        for payload in ("AAEC", "AwQF"):
            call.incoming.put_nowait(
                {"event": "media", "media": {"track": "inbound", "payload": payload}}
            )
        call.incoming.put_nowait({"event": "media"})  # malformed: ignored
        call.incoming.put_nowait({"event": "dtmf", "dtmf": {"digit": "1"}})
        await call.idle()
        assert call.stt.audio == bytes(range(6))


@call_test
async def test_prompt_is_warmed_with_exactly_what_the_turns_send(client, telegram):
    async with Call(client) as call:
        await call.hear_everything()
        await call.says("Hi")
        await call.hear_everything()
    [warm], [request] = call.claude.warmups, call.claude.requests
    for field in ("model", "system", "tools", "output_config"):
        assert warm[field] == request[field]
    assert "Test Store" in request["system"][0]["text"]
    assert "[Store Name]" not in request["system"][0]["text"]


# --- Barge-in -----------------------------------------------------------------


@call_test
async def test_late_marks_from_an_interrupted_turn_do_not_count_for_the_next(client, telegram):
    reply = Reply("We have wireless earbuds for forty dollars. We also have a lamp.")
    async with Call(client, reply) as call:
        await call.play(100)
        await call.says("What do you have?")  # cuts the greeting off
        await call.play(100)
        call.incoming.put_nowait({"event": "mark", "mark": {"name": "1:0"}})
        await call.idle()
        await call.starts_saying("wait")
        assert call.said() == "We have"


@call_test
async def test_barge_in_stops_audio_and_cuts_history_to_what_was_heard(client, telegram):
    reply = Reply("We have wireless earbuds for forty dollars. We also have a lamp.")
    async with Call(client, reply, Reply("Sure, the mug is twelve.")) as call:
        await call.hear_everything()
        await call.says("What do you have?")
        heard = await call.play(170)
        assert heard == "We have wireless "
        before = len(call.sent)

        await call.starts_saying("actually")

        assert not call.session.agent_speaking
        assert [m["event"] for m in call.sent[before:]] == ["clear"]
        assert call.said() == "We have wireless"
        await call.says("actually show me mugs")
        await call.hear_everything()
        assert call.tts.spoken[-1] == "Sure, the mug is twelve."
    sent = call.claude.requests[-1]["messages"]
    assert sent[3] == {
        "role": "assistant", "content": [{"type": "text", "text": "We have wireless"}]
    }
    assert texts(sent[4]) == ["actually show me mugs"]


@call_test
async def test_no_audio_follows_a_clear(client, telegram):
    async with Call(client, Reply("A long first sentence here. And a second one.")) as call:
        await call.hear_everything()
        call.tts.gate = asyncio.Event()  # synthesis stalls after the first chunk
        await call.says("Hi")
        await call.play(50)
        await call.starts_saying("wait")
        call.tts.gate.set()
        await call.idle()
        assert call.sent[-1]["event"] == "clear"


@call_test
async def test_marks_returned_by_a_clear_do_not_count_as_played(client, telegram):
    async with Call(client, Reply("One two three four. Five six seven eight.")) as call:
        await call.hear_everything()
        await call.says("Count")
        await call.play(80)
        await call.starts_saying("stop")
        await call.idle()  # the fake Twilio has now returned both marks
        assert call.said() == "One two"


@call_test
async def test_interrupting_while_claude_is_still_writing(client, telegram):
    never = asyncio.Event()
    reply = Reply("We have mugs and lamps. ", "And also some", never)
    async with Call(client, reply, Reply("Okay.")) as call:
        await call.hear_everything()
        await call.says("What do you have?")
        await call.play(120)
        await call.starts_saying("hold on")
        assert call.said() == "We have mugs"
        await call.says("hold on a second")
        await call.hear_everything()
        assert call.tts.spoken[-1] == "Okay."


@call_test
async def test_caller_who_keeps_talking_gets_one_answer_to_everything(client, telegram):
    never = asyncio.Event()
    async with Call(client, Reply(never), Reply("Two mugs, then.")) as call:
        await call.hear_everything()
        await call.says("I want")
        await call.starts_saying("two mugs")
        assert call.clears == 0  # nothing was playing
        await call.says("two mugs")
        await call.hear_everything()
    assert texts(call.claude.requests[-1]["messages"][-1]) == ["I want", "two mugs"]
    assert call.tts.spoken[-1] == "Two mugs, then."


@call_test
async def test_greeting_can_be_interrupted(client, telegram):
    async with Call(client, Reply("Yes, we do.")) as call:
        await call.play(60)
        await call.starts_saying("do you")
        assert call.said() == "Hello!"
        await call.says("do you have mugs")
        await call.hear_everything()
    assert call.session.transcript[0] == {"speaker": "aida", "text": "Hello! —"}


@pytest.mark.parametrize("noise", ["uh-huh", "Yeah.", "okay, I see"])
def test_listening_noises_do_not_interrupt(client, telegram, noise):
    async def run():
        async with Call(client, Reply("The lamp is warm. It dims too.")) as call:
            await call.hear_everything()
            await call.says("Tell me about the lamp")
            await call.play(60)
            await call.starts_saying(noise)
            await call.says(noise)
            assert call.session.agent_speaking and call.clears == 0
            await call.hear_everything()
            assert call.said() == "The lamp is warm. It dims too."
        assert len(call.claude.requests) == 1

    asyncio.run(run())


@call_test
async def test_yes_over_a_question_is_the_answer(client, telegram):
    question = "Two mugs to Amir Temur Street. Is that correct?"
    async with Call(client, Reply(question), Reply("Great.")) as call:
        await call.hear_everything()
        await call.says("Two mugs please")
        await call.play(100)
        await call.says("yes")  # said over the read-back: not yet an answer
        assert call.session.agent_speaking
        await call.play(330)  # "Is that correct" has started
        await call.says("yes")
        await call.hear_everything()
    assert call.tts.spoken[-1] == "Great."
    assert texts(call.claude.requests[-1]["messages"][-1]) == ["yes"]


@call_test
async def test_yes_after_aida_has_finished_is_a_turn(client, telegram):
    async with Call(client, Reply("It's twelve dollars."), Reply("Great.")) as call:
        await call.hear_everything()
        await call.says("How much?")
        await call.hear_everything()
        await call.says("okay")
        await call.hear_everything()
        assert call.tts.spoken[-1] == "Great."


@call_test
async def test_answering_just_as_aida_finishes_keeps_the_whole_message(client, telegram):
    async with Call(client, Reply("It's twelve dollars."), Reply("Sure.")) as call:
        await call.hear_everything()
        await call.says("How much?")
        call._buffer = [item for item in call._buffer if item[0] == "audio"]  # the mark is late
        await call.play()
        assert call.session.agent_speaking
        await call.starts_saying("I'll take it")
        # Fully heard, so nothing is cut and Claude's own content stays untouched.
        assert [b["type"] for b in call.messages[-1]["content"]] == ["thinking", "text"]
        await call.says("I'll take it")
        await call.hear_everything()
    assert {"speaker": "aida", "text": "It's twelve dollars."} in call.session.transcript


@call_test
async def test_many_interruptions_never_produce_a_request_claude_would_reject(client, telegram):
    replies = []
    for i in range(6):
        search = Tool("search_catalog", {"query": "mug"})
        replies += [Reply("Looking now. ", search), Reply(f"Here is answer number {i}. And more.")]
    async with Call(client, *replies) as call:
        for i in range(6):
            await call.play(30 + 70 * i)
            await call.starts_saying(f"question {i}")
            await call.says(f"question {i}")
            await call.until(lambda: len(call.tts.spoken) and call.session.agent_speaking)
        await call.hear_everything()
    assert len(call.claude.requests) >= 6  # __aexit__ checks that none were rejected


# --- Tools --------------------------------------------------------------------


@call_test
async def test_full_order(client, telegram):
    replies = [
        Reply(Tool("search_catalog", {"query": "earbuds"})),
        Reply("They're forty-nine ninety-nine. Where should we deliver?"),
        Reply("Two to Amir Temur Street. Is that correct?"),
        Reply("Placing it now. ", Tool("create_order", ORDER)),
        Reply(Tool("generate_invoice", {"order_id": "1", "payment_provider": "click"})),
        Reply(
            Tool(
                "notify_admin_order",
                {"order_id": "1", "customer_summary": "Wants it soon", "payment_link": "x"},
            )
        ),
        Reply("Your order is in. The link is in your Telegram chat."),
    ]
    async with Call(client, *replies) as call:
        for line in ("Any earbuds?", "Amir Temur Street twelve", "Yes, that's right"):
            await call.hear_everything()
            await call.says(line)
        await call.hear_everything()
        assert call.session.order_ids == [1]
        assert call.tts.spoken[-1] == "The link is in your Telegram chat."
    chats = [p["chat_id"] for p in telegram.payloads]
    assert chats == ["123456789", "555"]  # the link to the customer, then the admin's notice
    assert "Pay here" in telegram.payloads[0]["text"]
    order = client.get("/api/orders/1").json()
    assert order["customer_telegram_id"] == "123456789"
    assert order["customer_telegram_username"] == "dilnoza_k"


@call_test
async def test_filler_covers_a_silent_tool_call_but_stays_out_of_history(client, telegram):
    async with Call(
        client, Reply(Tool("search_catalog", {"query": "mug"})), Reply("Found some.")
    ) as call:
        await call.hear_everything()
        await call.says("Mugs?")
        await call.hear_everything()
        assert call.tts.spoken[-2] in FILLERS
    assert call.tts.spoken[-2] not in json.dumps(call.messages)


@call_test
async def test_aida_follows_a_caller_who_switches_to_russian(client, telegram):
    async with Call(
        client,
        Reply("Да, есть кружки за $12."),
        Reply(Tool("search_catalog", {"query": "кружка"})),
        Reply("Нашла две."),
        voice_language="uz",
    ) as call:
        await call.hear_everything()
        assert set(call.tts.languages) == {"uz"}  # the greeting
        await call.says("Здравствуйте, у вас есть кружки?")
        await call.hear_everything()
        assert call.session.language == "ru"
        assert call.tts.spoken[-1] == "Да, есть кружки за двенадцать долларов."
        assert call.tts.languages[-1] == "ru"
        await call.says("А какие?")
        await call.hear_everything()
        # The filler while the catalog is searched is Russian now too.
        assert call.tts.spoken[-2] in RUSSIAN.fillers and call.tts.languages[-2] == "ru"


@call_test
async def test_no_filler_when_aida_already_said_something(client, telegram):
    async with Call(
        client,
        Reply("Let me look. ", Tool("search_catalog", {"query": "mug"})),
        Reply("Found some."),
    ) as call:
        await call.hear_everything()
        await call.says("Mugs?")
        await call.hear_everything()
        assert call.tts.spoken[-2:] == ["Let me look.", "Found some."]


class SlowTools:
    """Holds one tool until released, so a test can act while it runs."""

    def __init__(self, tools, name):
        self.tools, self.name = tools, name
        self.definitions = tools.definitions
        self.started, self.release = asyncio.Event(), asyncio.Event()
        self.loop = asyncio.get_running_loop()

    def call(self, name, arguments):
        if name == self.name:
            self.loop.call_soon_threadsafe(self.started.set)
            asyncio.run_coroutine_threadsafe(self.release.wait(), self.loop).result(5)
        return self.tools.call(name, arguments)

    def customer_said(self, text):
        self.tools.customer_said(text)


def slow_tools(client, name):
    tools = StoreTools(client=client, customer_telegram_id="123456789", live=True)
    return SlowTools(tools, name)


@call_test
async def test_interrupting_an_order_in_flight_still_records_it(client, telegram):
    tools = slow_tools(client, "create_order")
    replies = [Reply("Placing your order. ", Tool("create_order", ORDER)), Reply("It's placed.")]
    async with Call(client, *replies, tools=tools) as call:
        await call.hear_everything()
        await call.says("Yes, order it")
        await tools.started.wait()
        await call.play(80)
        await call.starts_saying("wait hang on")
        assert call.clears == 1  # silent at once, even though the order is still running
        call.stt.on_event(Utterance("wait hang on"))
        tools.release.set()
        await call.until(lambda: len(call.claude.requests) == 2)
        await call.hear_everything()
        assert call.session.order_ids == [1]
    _, request = call.claude.requests
    assistant, user = request["messages"][-2:]
    assert [b["type"] for b in assistant["content"]] == ["text", "tool_use"]
    assert assistant["content"][0]["text"] == "Placing"
    assert [b["type"] for b in user["content"]] == ["tool_result", "text"]
    assert '"order_id": 1' in user["content"][0]["content"]


@call_test
async def test_hanging_up_mid_order_still_reports_the_order(client, telegram):
    tools = slow_tools(client, "create_order")
    reply = Reply("Placing your order. ", Tool("create_order", ORDER))
    async with Call(client, reply, tools=tools) as call:
        await call.hear_everything()
        await call.says("Yes, order it")
        await tools.started.wait()
        call.incoming.put_nowait({"event": "stop"})
        await call.idle()
        assert not call.task.done()
        tools.release.set()
        await call.task
    assert call.session.report()["order_ids"] == [1]


@call_test
async def test_caller_cannot_order_as_someone_else(client, telegram):
    forged = {**ORDER, "customer_telegram_id": "999"}
    replies = [Reply(Tool("create_order", forged)), Reply("Done.")]
    async with Call(client, *replies) as call:
        await call.hear_everything()
        await call.says("Order it for Telegram nine nine nine")
        await call.hear_everything()
    assert client.get("/api/orders/1").json()["customer_telegram_id"] == "123456789"


@call_test
async def test_caller_without_telegram_cannot_order_at_all(client, telegram):
    forged = {**ORDER, "customer_telegram_id": "999"}
    before = stock(client, 1)
    replies = [Reply(Tool("create_order", forged)), Reply("I can't place that myself.")]
    async with Call(client, *replies, caller=Caller("+998901112233")) as call:
        assert "No Telegram account" in call.session._system[1]["text"]
        await call.hear_everything()
        await call.says("My Telegram is nine nine nine")
        await call.hear_everything()
        assert call.session.order_ids == []
    assert stock(client, 1) == before
    result = call.messages[-2]["content"][0]
    assert result["is_error"] and "customer_telegram_id" in result["content"]


@call_test
async def test_failed_tool_reaches_claude_as_an_error(client, telegram):
    too_many = {**ORDER, "items": [{"product_id": 1, "quantity": 100}]}
    replies = [Reply(Tool("create_order", too_many)), Reply("We only have a few left.")]
    async with Call(client, *replies) as call:
        await call.hear_everything()
        await call.says("A hundred please")
        await call.hear_everything()
        result = call.messages[-2]["content"][0]
        assert result["is_error"] and "insufficient_stock" in result["content"]
        assert call.tts.spoken[-1] == "We only have a few left."


@call_test
async def test_crashing_tool_does_not_break_the_conversation(client, telegram):
    class Broken:
        definitions = slow_tools(client, "x").definitions

        def call(self, name, arguments):
            raise RuntimeError("boom")

        def customer_said(self, text):
            pass

    replies = [Reply(Tool("search_catalog", {"query": "x"})), Reply("Sorry about that.")]
    async with Call(client, *replies, tools=Broken()) as call:
        await call.hear_everything()
        await call.says("Mugs?")
        await call.hear_everything()
        assert call.tts.spoken[-1] == "Sorry about that."


@call_test
async def test_endless_tool_calls_are_stopped(client, telegram):
    search = Reply(Tool("search_catalog", {"query": "mug"}))
    async with Call(client, *[search] * 20) as call:
        await call.hear_everything()
        await call.says("Mugs?")
        await call.hear_everything()
        assert len(call.claude.requests) == 8
        assert HICCUP.startswith(call.tts.spoken[-2])
        await call.says("Hello?")
        await call.until(lambda: len(call.claude.requests) > 8)


# --- Handing over -------------------------------------------------------------


ESCALATE = Tool("escalate_to_human", {"reason": "Upset", "context_summary": "Wants a refund"})


@call_test
async def test_escalation_transfers_and_pauses_the_ai(client, telegram):
    async with Call(
        client, Reply(ESCALATE), admin_phone="+998900000000", admin_name="Kim"
    ) as call:
        await call.hear_everything()
        await call.says("This is ridiculous, get me a person")
        await call.until(lambda: call.session._closing)
        await call.starts_saying("hello?")  # ignored from here on
        await call.says("hello?")
        assert call.clears == 0
        await call.hear_everything()
        await asyncio.wait_for(call.task, 5)
    assert spoken_last(call).endswith(TRANSFER.format(admin="Kim"))
    assert call.session.outcome == "escalated"
    assert len(call.claude.requests) == 1
    assert "HUMAN SUPPORT NEEDED" in telegram.last_text and "+998901234567" in telegram.last_text
    assert "💬 Last words: “This is ridiculous, get me a person”" in telegram.last_text


def spoken_last(call) -> str:
    """Everything said since Claude last heard from the caller."""
    return call.session.transcript[-1]["text"]


@pytest.mark.parametrize(
    "caller, line",
    [
        (Caller("+998901112233", "42"), STEPPING_IN),
        (Caller(None, "42"), STEPPING_IN),
        (Caller("+998901112233"), CALLBACK),
        (Caller(), PASSED_ON),
    ],
)
def test_escalation_without_a_phone_to_transfer_to(client, telegram, caller, line):
    async def run():
        async with Call(client, Reply(ESCALATE), caller=caller) as call:
            await call.hear_everything()
            await call.says("Get me a person")
            await call.hear_everything()
            await asyncio.wait_for(call.task, 5)
        assert spoken_last(call).endswith(line.format(admin="our store manager"))
        assert call.session.outcome == "escalated"

    asyncio.run(run())


def test_transfer_does_not_claim_notes_were_passed_on_when_they_were_not(make_client):
    async def run():
        client = make_client(telegram_bot_token=None)  # nothing is delivered
        async with Call(client, Reply(ESCALATE), admin_phone="+998900000000") as call:
            await call.hear_everything()
            await call.says("Get me a person")
            await call.hear_everything()
            await asyncio.wait_for(call.task, 5)
        assert spoken_last(call).endswith(TRANSFER_ONLY.format(admin="our store manager"))
        assert "passed your order notes" not in spoken_last(call)
        assert call.session.outcome == "escalated"

    asyncio.run(run())


def test_aida_never_claims_to_connect_a_manager_nobody_reached(make_client):
    async def run():
        client = make_client(telegram_bot_token=None)  # dry run: nothing is delivered
        async with Call(client, Reply(ESCALATE), Reply("I couldn't reach them.")) as call:
            await call.hear_everything()
            await call.says("Get me a person")
            await call.hear_everything()
            assert call.tts.spoken[-1] == "I couldn't reach them."
            assert not call.task.done()
        assert call.session.outcome == "completed"
        assert "no_manager" in json.dumps(call.messages)

    asyncio.run(run())


@call_test
async def test_interrupting_an_escalation_in_flight_still_hands_over(client, telegram):
    tools = slow_tools(client, "escalate_to_human")
    async with Call(
        client, Reply("One second. ", ESCALATE), tools=tools, admin_phone="+998900000000"
    ) as call:
        await call.hear_everything()
        await call.says("Get me a person")
        await tools.started.wait()
        call.stt.on_event(Speech("hurry up please"))
        await call.idle()
        tools.release.set()
        await call.until(lambda: call.tts.spoken[-1].startswith("Connecting you right now"))
        await call.hear_everything()
        await asyncio.wait_for(call.task, 5)
    assert call.session.outcome == "escalated"


# --- Unclear and silent callers -----------------------------------------------


@call_test
async def test_garbled_twice_asks_again_and_is_kept_from_claude(client, telegram):
    async with Call(client, Reply("I missed that."), Reply("It's noisy."), Reply("Ok.")) as call:
        await call.hear_everything()
        for attempt in (1, 2):
            await call.says("flurb the zonk", confidence=0.2)
            await call.hear_everything()
            note = UNCLEAR_NOTE.format(attempt=attempt)
            assert f"attempt {attempt}" in note
            assert texts(call.claude.requests[-1]["messages"][-1]) == [f"[{note}]"]
        assert "flurb" not in json.dumps(call.messages)
        await call.says("A mug please")  # clear again: the count starts over
        await call.hear_everything()
        for _ in (1, 2):
            await call.says("zonk", confidence=0.2)
            await call.hear_everything()
        assert f"[{UNCLEAR_NOTE.format(attempt=2)}]" in json.dumps(call.messages[-2])
        assert call.session.outcome == "completed" and not call.task.done()


@call_test
async def test_garbled_three_times_in_a_row_goes_to_a_person(client, telegram):
    replies = [Reply("I missed that."), Reply("It's noisy.")]
    async with Call(client, *replies, admin_phone="+998900000000") as call:
        await call.hear_everything()
        for _ in range(2):
            await call.says("flurb", confidence=0.2)
            await call.hear_everything()
            assert not call.session._closing
        await call.says("zonk", confidence=0.2)
        await call.until(lambda: call.session._closing)
        await call.hear_everything()
        await asyncio.wait_for(call.task, 5)
    assert call.session.outcome == "escalated"
    assert len(call.claude.requests) == 2
    assert "❓ Reason: Repeated low transcription confidence\n" in telegram.last_text
    assert "💬 Last words: “zonk”" in telegram.last_text


def test_garbled_three_times_with_nobody_to_take_over(make_client):
    async def run():
        client = make_client(telegram_bot_token=None)
        async with Call(client, *[Reply("Sorry?")] * 3) as call:
            await call.hear_everything()
            for attempt in (1, 2, 3):
                await call.says("flurb", confidence=0.2)
                await call.until(lambda n=attempt: len(call.claude.requests) == n)
                await call.hear_everything()
            assert "no manager could be reached" in json.dumps(call.messages[-2])
            assert not call.task.done()

    asyncio.run(run())


@call_test
async def test_empty_transcripts_are_ignored(client, telegram):
    async with Call(client) as call:
        await call.play(50)
        await call.starts_saying("  ")
        await call.says("")
        assert call.session.agent_speaking and call.clears == 0
        assert call.claude.requests == []


@call_test
async def test_silence_prompts_twice_then_hangs_up(client, telegram):
    async with Call(client, Reply("Still there?"), Reply("Hello?")) as call:
        for expected in ("Still there?", "Hello?"):
            await call.hear_everything()
            call.now += 1
            await call.until(lambda line=expected: call.tts.spoken[-1] == line)
        assert f"[{SILENT_NOTE}]" in texts(call.claude.requests[-1]["messages"][-1])
        await call.hear_everything()
        call.now += 1
        await call.until(lambda: call.session._closing)
        await call.hear_everything()
        await asyncio.wait_for(call.task, 5)
    assert call.session.outcome == "silent"
    assert call.session.transcript[-1]["text"] == SILENT


@call_test
async def test_talking_resets_the_silence_count(client, telegram):
    async with Call(client, Reply("Still there?"), Reply("Great."), Reply("Hello?")) as call:
        await call.hear_everything()
        call.now += 1
        await call.until(lambda: call.tts.spoken[-1] == "Still there?")
        await call.hear_everything()
        await call.says("Yes I'm here")
        await call.hear_everything()
        assert call.session._silent_prompts == 0


@call_test
async def test_a_caller_mid_sentence_is_not_silent(client, telegram):
    async with Call(client) as call:
        await call.hear_everything()
        for _ in range(5):
            call.now += 0.15  # under the limit each time, well over it in total
            await call.starts_saying("my address is twelve")
            await asyncio.sleep(0.05)
        assert call.claude.requests == []


# --- When things break --------------------------------------------------------


@call_test
async def test_claude_failing_once_is_a_hiccup(client, telegram):
    async with Call(client, 500, 500, Reply("Here you go.")) as call:  # the SDK retries once
        await call.hear_everything()
        await call.says("Mugs?")
        await call.hear_everything()
        assert " ".join(call.tts.spoken[-2:]) == HICCUP
        await call.says("Mugs please")
        await call.hear_everything()
        assert call.tts.spoken[-1] == "Here you go."


@call_test
async def test_claude_failing_twice_ends_the_call_and_alerts_the_admin(client, telegram):
    async with Call(client, *[500] * 4) as call:
        await call.hear_everything()
        await call.says("Mugs?")
        await call.hear_everything()
        await call.says("Mugs please")
        await asyncio.wait_for(call.task, 5)
        await call.session.alert_problem()
    assert call.session.outcome == "failed"
    assert "HUMAN SUPPORT NEEDED" in telegram.last_text and "Mugs please" in telegram.last_text


@call_test
async def test_rejected_thinking_is_dropped_and_the_turn_retried(client, telegram):
    async with Call(client, Reply("One."), Reply("Two.")) as call:
        await call.hear_everything()
        await call.says("First")
        await call.hear_everything()
        call.messages[-1]["content"][0]["signature"] = "stale"
        await call.says("Second")
        await call.hear_everything()
        assert call.tts.spoken[-1] == "Two."
        assert len(call.claude.rejected) == 1
        call.claude.rejected.clear()


@call_test
async def test_refusal_gets_a_polite_line(client, telegram):
    async with Call(client, Reply("", stop="refusal"), Reply("Sure.")) as call:
        await call.hear_everything()
        await call.says("Something bad")
        await call.hear_everything()
        assert call.said() == REFUSED
        await call.says("A mug then")
        await call.hear_everything()


@call_test
async def test_truncated_tool_call_is_not_run(client, telegram):
    cut_off = Reply("Placing it.", Tool("create_order", ORDER), stop="max_tokens")
    async with Call(client, cut_off, Reply("Let me try again.")) as call:
        await call.hear_everything()
        await call.says("Order it")
        await call.hear_everything()
        assert call.session.order_ids == []
        await call.says("Did it work?")
        await call.hear_everything()


@call_test
async def test_only_the_fallback_models_tool_calls_run(client, telegram):
    reply = Reply(
        Tool("search_catalog", {"query": "dead"}), Fallback(), Tool("create_order", ORDER)
    )
    async with Call(client, reply, Reply("Placed.")) as call:
        await call.hear_everything()
        await call.says("Order it")
        await call.hear_everything()
        assert call.session.order_ids == [1]
        assert "dead" not in json.dumps(call.messages)


@call_test
async def test_empty_reply_is_a_hiccup(client, telegram):
    async with Call(client, Reply(), Reply("Hi again.")) as call:
        await call.hear_everything()
        await call.says("Hello?")
        await call.hear_everything()
        assert call.said() == HICCUP
        await call.says("Hello?")
        await call.hear_everything()


@call_test
async def test_speech_synthesis_failing_ends_the_call(client, telegram):
    async with Call(client, Reply("Hi.")) as call:
        await call.hear_everything()
        call.tts.fail = TtsError("HTTP 500")
        await call.says("Hello?")
        await asyncio.wait_for(call.task, 5)
    assert call.session.outcome == "failed"
    assert "Text-to-speech" in call.session.problem


@call_test
async def test_speech_recognition_failing_ends_the_call(client, telegram):
    async with Call(client) as call:
        call.stt.fail.set_exception(SttUnavailable("closed by the provider"))
        await asyncio.wait_for(call.task, 5)
    assert call.session.outcome == "failed"


# --- The report ---------------------------------------------------------------


@call_test
async def test_report_has_what_was_heard_and_is_accepted_by_the_backend(client, telegram):
    reply = Reply("We have mugs and lamps and more.")
    async with Call(client, reply) as call:
        await call.hear_everything()
        await call.says("[What] do you have?")
        await call.play(130)
        await call.starts_saying("just mugs")
        await call.hang_up()
    report = call.session.report()
    assert report["transcript"] == [
        {"speaker": "aida", "text": HELLO},
        {"speaker": "caller", "text": "What do you have?"},
        {"speaker": "aida", "text": "We have mugs —"},
    ]
    response = client.post("/api/admin/notify-order", json={"call": report})
    assert response.status_code == 200, response.text
    assert "CALL ENDED" in telegram.last_text and "We have mugs —" in telegram.last_text


@call_test
async def test_finished_call_is_accepted_for_its_summary(client, telegram):
    replies = [Reply("Placing it. ", Tool("create_order", ORDER)), Reply("Done.")]
    async with Call(client, *replies) as call:
        await call.hear_everything()
        await call.says("Order it")
        await call.hear_everything()
        call.now += 20
        await call.hang_up()
    call.now += 500  # what happens after the call isn't part of it
    ended = call.session.ended()
    assert ended["raw_audio_transcript"].splitlines()[:2] == [f"Aida: {HELLO}", "Caller: Order it"]
    assert 20 <= ended["call_duration"] < 60
    assert call.session.report()["duration_seconds"] == ended["call_duration"]
    assert ended["order_ids"] == [1] and ended["escalated"] is False
    response = client.post("/api/telephony/call-ended", json=ended)
    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "Order Placed"
    assert "👤 Customer: @dilnoza_k\n" in json.loads(telegram.requests[-2].content)["text"]
    assert b"Caller: Order it" in telegram.requests[-1].content  # no summary, so it's attached


@pytest.mark.parametrize(
    "outcome, escalated",
    [("completed", False), ("silent", False), ("escalated", True), ("failed", True)],
)
def test_which_calls_count_as_escalated(client, outcome, escalated):
    async def run():
        async with Call(client) as call:
            await call.hang_up()
        call.session.outcome = outcome
        assert call.session.ended()["escalated"] is escalated

    asyncio.run(run())


@call_test
async def test_hanging_up_before_anything_is_heard(client, telegram):
    async with Call(client) as call:
        await call.hang_up()
    assert call.session.ended()["raw_audio_transcript"] == ""
    assert client.post("/api/telephony/call-ended", json=call.session.ended()).is_success
    assert call.session.report()["transcript"] == []
    assert client.post("/api/admin/notify-order", json={"call": call.session.report()}).is_success


@call_test
async def test_latency_is_measured_from_the_transcript_to_the_first_audio(client, telegram):
    wait = asyncio.Event()
    async with Call(client, Reply(wait, "Hi.")) as call:
        await call.hear_everything()
        await call.says("Hello?")
        call.now += 0.6
        wait.set()
        await call.hear_everything()
    assert call.session.latencies_ms == [600]
