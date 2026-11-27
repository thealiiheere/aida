"""OpenAI in Claude's place: the adapter, a chat, a call, summaries and the key check."""

import asyncio
import json

import httpx2
import pytest

from agent.config import AidaSettings
from agent.history import Conversation
from agent.llm import Claude, LlmError, make_llm
from agent.openai_llm import OpenAiLlm, to_openai
from app.config import Settings
from app.summary import Summariser
from check import check_llm
from tests.fakes import Call, FakeOpenAI, Reply, Tool, voice_settings
from tests.test_chat import run, store, world  # noqa: F401 - fixtures
from tests.test_voice_session import call_test

OPENAI = {"llm_provider": "openai", "openai_api_key": "sk-test"}


def settings(**overrides) -> AidaSettings:
    return AidaSettings(_env_file=None, **(OPENAI | overrides))


# --- Choosing -----------------------------------------------------------------


def test_the_provider_comes_from_settings():
    assert isinstance(make_llm(settings()), OpenAiLlm)
    assert isinstance(make_llm(settings(llm_provider="anthropic")), Claude)
    assert make_llm(settings()).key_name == "OPENAI_API_KEY"
    assert not make_llm(settings(openai_api_key=None)).has_credentials


# --- The translation ----------------------------------------------------------


def test_a_claude_conversation_becomes_an_openai_request():
    conversation = Conversation()
    conversation.add_note("The customer pressed Start.")
    conversation.add_customer("Mugs?")
    conversation.add_assistant([
        {"type": "thinking", "thinking": "Search.", "signature": "s"},
        {"type": "text", "text": "Let me look."},
        {"type": "tool_use", "id": "call_1", "name": "search_catalog",
         "input": {"query": "mug"}},
    ])  # fmt: skip
    conversation.add_tool_results(
        [{"type": "tool_result", "tool_use_id": "call_1", "content": '{"ok": true}'}]
    )
    conversation.add_note("A note that arrived with the result.")
    tools = [{"name": "search_catalog", "description": "Search.", "input_schema": {"type": "object"}}]
    body = to_openai([{"type": "text", "text": "You are Aida."}, {"type": "text", "text": "Hi."}],
                     tools, conversation)  # fmt: skip
    assert body["messages"] == [
        {"role": "system", "content": "You are Aida.\n\nHi."},
        {"role": "user", "content": "[The customer pressed Start.]\nMugs?"},
        {"role": "assistant", "content": "Let me look.", "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "search_catalog", "arguments": '{"query": "mug"}'},
        }]},
        {"role": "tool", "tool_call_id": "call_1", "content": '{"ok": true}'},
        {"role": "user", "content": "[A note that arrived with the result.]"},
    ]  # fmt: skip
    assert body["tools"] == [{"type": "function", "function": {
        "name": "search_catalog", "description": "Search.", "parameters": {"type": "object"},
    }}]  # fmt: skip


# --- Replies ------------------------------------------------------------------


def ask(fake: FakeOpenAI, patient: bool = False, **overrides):
    async def go():
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(fake))
        llm = OpenAiLlm(settings(**overrides), client, patient=patient)
        conversation = Conversation()
        conversation.add_customer("Mugs?")
        streamed = []
        try:
            reply = await llm.reply([{"type": "text", "text": "Be Aida."}], [], conversation,
                                    streamed.append)  # fmt: skip
        finally:
            await llm.close()
        return reply, streamed

    return asyncio.run(go())


def test_text_streams_and_tool_calls_come_back_as_claude_blocks():
    fake = FakeOpenAI(Reply("Checking our mugs now. ", Tool("search_catalog", {"query": "mug"})))
    reply, streamed = ask(fake)
    assert "".join(streamed) == "Checking our mugs now. " and len(streamed) > 1
    assert reply.stop_reason == "tool_use"
    assert reply.content == [
        {"type": "text", "text": "Checking our mugs now. "},
        {"type": "tool_use", "id": "call_1", "name": "search_catalog", "input": {"query": "mug"}},
    ]
    (body,) = fake.requests
    assert body["model"] == "gpt-5-mini" and body["stream"] is True
    assert body["reasoning_effort"] == "low" and body["max_completion_tokens"] == 4096
    assert fake.headers[0]["authorization"] == "Bearer sk-test"


def test_options():
    fake = FakeOpenAI()
    ask(fake, openai_model="gpt-4.1", openai_reasoning_effort=None)
    assert fake.requests[0]["model"] == "gpt-4.1" and "reasoning_effort" not in fake.requests[0]


@pytest.mark.parametrize("stop, expected", [("max_tokens", "max_tokens"), ("refusal", "refusal")])
def test_stop_reasons(stop, expected):
    reply, _ = ask(FakeOpenAI(Reply("Hm", stop=stop)))
    assert reply.stop_reason == expected


def test_a_rejected_request_is_not_retried_and_does_not_show_the_key():
    fake = FakeOpenAI(401, Reply("Never"))
    with pytest.raises(LlmError, match="HTTP 401") as failure:
        ask(fake, patient=True)
    assert "sk-test" not in str(failure.value) and len(fake.replies) == 1


def test_a_server_error_is_retried_when_there_is_time():
    reply, _ = ask(FakeOpenAI(500, Reply("Mugs!")), patient=True)
    assert reply.content == [{"type": "text", "text": "Mugs!"}]
    with pytest.raises(LlmError, match="HTTP 500"):
        ask(FakeOpenAI(500, Reply("Too late")))  # on the phone: no time to retry


# --- A chat and a call --------------------------------------------------------


def test_a_chat_answered_by_openai(world):  # noqa: F811
    async def test():
        replies = [Reply(Tool("search_catalog", {"query": "earbuds"})), Reply("We have earbuds.")]
        async with world(*replies, **OPENAI) as w:
            assert await w.ask("Do you have earbuds?") == "We have earbuds."
            assert w.openai.rejected == []
            last = w.openai.requests[-1]["messages"]
            assert [m["role"] for m in last] == ["system", "user", "assistant", "tool"]
            assert json.loads(last[-1]["content"])["ok"] is True
            assert w.claude.requests == []

    run(test)


@call_test
async def test_a_call_answered_by_openai(client, telegram):
    async with Call(client, Reply(Tool("search_catalog", {"query": "mug"})),
                    Reply("We have mugs for $12."), **OPENAI) as call:  # fmt: skip
        await call.hear_everything()
        await call.says("Mugs?")
        await call.hear_everything()
        assert call.tts.spoken[-1] == "We have mugs for twelve dollars."
        assert call.openai.rejected == [] and len(call.openai.requests) == 2


# --- Summaries ----------------------------------------------------------------


def test_call_summaries_by_openai():
    seen = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        seen.append(body)
        summary = {"customer_intent": "Wanted mugs.", "items_discussed": "Mugs.",
                   "outcome": "Inquiry Only"}  # fmt: skip
        return httpx2.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": json.dumps(summary), "refusal": None}}]})

    summariser = Summariser(
        Settings(_env_file=None, **OPENAI), httpx2.Client(transport=httpx2.MockTransport(answer))
    )
    assert summariser.configured
    summary = summariser.summarise("Caller: Mugs?\nAida: We have mugs.")
    assert summary.outcome == "Inquiry Only"
    schema = seen[0]["response_format"]["json_schema"]
    assert schema["strict"] is True
    assert schema["schema"]["required"] == ["customer_intent", "items_discussed", "outcome"]
    assert schema["schema"]["additionalProperties"] is False
    assert not Summariser(Settings(_env_file=None, llm_provider="openai")).configured


# --- The key check ------------------------------------------------------------


def openai_answering(status: int, code: str = "") -> httpx2.Client:
    def answer(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["authorization"] == "Bearer sk-test"
        if status == 200:
            return httpx2.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
        return httpx2.Response(status, json={"error": {"code": code, "message": "no"}})

    return httpx2.Client(transport=httpx2.MockTransport(answer))


@pytest.mark.parametrize(
    "status, code, says",
    [
        (200, "", "answers, with gpt-5-mini"),
        (401, "invalid_api_key", "OPENAI_API_KEY was rejected"),
        (429, "insufficient_quota", "no credit left"),
        (404, "model_not_found", "There is no model called gpt-5-mini"),
        (429, "rate_limit_exceeded", "Too many requests"),
    ],
)
def test_the_key_check(status, code, says):
    result = check_llm(voice_settings(**OPENAI), openai_answering(status, code))
    assert result.name == "OpenAI" and result.ok is (status == 200) and says in result.detail


# --- Gemini, through Google's OpenAI-compatible API ----------------------------

GEMINI = {"llm_provider": "gemini", "gemini_api_key": "g-test", "openai_api_key": "sk-other"}


def test_gemini_answers_through_the_same_adapter():
    seen = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"choices": [{"message": {"content": "Salom"}}]})

    llm = make_llm(settings(**GEMINI))
    assert isinstance(llm, OpenAiLlm) and llm.key_name == "GEMINI_API_KEY"
    llm = OpenAiLlm(settings(**GEMINI), httpx2.AsyncClient(transport=httpx2.MockTransport(answer)))
    assert llm.has_credentials
    assert not make_llm(settings(llm_provider="gemini", gemini_api_key=None)).has_credentials
    assert llm._url == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    assert llm._options["model"] == "gemini-3.8-flash" and llm._key == "g-test"


@pytest.mark.parametrize(
    "status, says",
    [(200, "answers, with gemini-3.8-flash"), (400, "GEMINI_API_KEY was rejected"),
     (404, "There is no model called gemini-3.8-flash"), (429, "no quota left")],
)  # fmt: skip
def test_the_gemini_key_check(status, says):
    def answer(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["authorization"] == "Bearer g-test"
        assert str(request.url).startswith("https://generativelanguage.googleapis.com/")
        if status == 200:
            return httpx2.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
        return httpx2.Response(status, json=[{"error": {"message": "API key not valid"}}])

    client = httpx2.Client(transport=httpx2.MockTransport(answer))
    result = check_llm(voice_settings(**GEMINI), client)
    assert result.name == "Gemini" and result.ok is (status == 200) and says in result.detail
