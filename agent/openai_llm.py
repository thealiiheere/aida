"""Streams replies from OpenAI instead of Claude, for LLM_PROVIDER=openai.

The rest of Aida keeps the conversation in Claude's format, so this translates it to
OpenAI's Chat Completions API on the way out and the answer back on the way in:
text blocks, tool calls and tool results map one to one. Thinking blocks, which only
Claude makes, are left out.
"""

import json
import logging
from collections.abc import Callable

import httpx2

from .config import AidaSettings
from .history import Conversation
from .llm import MAX_TOKENS, LlmError, Reply

log = logging.getLogger("aida.openai")

STOP_REASONS = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "max_tokens",
    "content_filter": "refusal",
}


# What each OpenAI-compatible provider is called, and the key it needs.
PROVIDERS = {"openai": ("OpenAI", "OPENAI_API_KEY"), "gemini": ("Gemini", "GEMINI_API_KEY")}


def as_openai(settings):
    """The settings with Gemini's in the OPENAI_ places when LLM_PROVIDER is gemini.

    Google serves Gemini through an OpenAI-compatible API, so one adapter does both.
    """
    if settings.llm_provider != "gemini":
        return settings
    return settings.model_copy(
        update={
            "openai_api_key": settings.gemini_api_key,
            "openai_base_url": settings.gemini_base_url,
            "openai_model": settings.gemini_model,
            "openai_reasoning_effort": settings.gemini_reasoning_effort,
        }
    )


def to_openai(system: list[dict], tools: list[dict], conversation: Conversation) -> dict:
    """A request body for Chat Completions, from what Claude would have been sent."""
    messages = [{"role": "system", "content": "\n\n".join(b["text"] for b in system)}]
    for message in conversation.messages:
        blocks = message["content"]
        if message["role"] == "assistant":
            text = "".join(b["text"] for b in blocks if b["type"] == "text")
            calls = [
                {
                    "id": b["id"],
                    "type": "function",
                    "function": {"name": b["name"], "arguments": json.dumps(b["input"])},
                }
                for b in blocks
                if b["type"] == "tool_use"
            ]
            out = {"role": "assistant", "content": text or None}
            if calls:
                out["tool_calls"] = calls
            messages.append(out)
            continue
        # Tool results answer the calls just before, so they go first.
        for b in blocks:
            if b["type"] == "tool_result":
                messages.append(
                    {"role": "tool", "tool_call_id": b["tool_use_id"], "content": _text(b)}
                )
        text = "\n".join(b["text"] for b in blocks if b["type"] == "text")
        if text:
            messages.append({"role": "user", "content": text})
    return {
        "messages": messages,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t["input_schema"],
                },
            }
            for t in tools
        ],
    }


def _text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content or ""


class OpenAiLlm:
    """The same interface as agent.llm.Claude."""

    key_name = "OPENAI_API_KEY"

    def __init__(
        self,
        settings: AidaSettings,
        http_client: httpx2.AsyncClient | None = None,
        *,
        patient: bool = False,
    ):
        self.key_name = PROVIDERS.get(settings.llm_provider, PROVIDERS["openai"])[1]
        settings = as_openai(settings)
        key = settings.openai_api_key
        self._key = key.get_secret_value() if key else None
        self._url = settings.openai_base_url.rstrip("/") + "/chat/completions"
        self._options = {"model": settings.openai_model, "max_completion_tokens": MAX_TOKENS}
        if settings.openai_reasoning_effort:
            self._options["reasoning_effort"] = settings.openai_reasoning_effort
        # Someone typing can wait. Someone on the phone hears the silence.
        timeout = httpx2.Timeout(120, connect=5) if patient else httpx2.Timeout(30, connect=3)
        self._client = http_client or httpx2.AsyncClient(timeout=timeout)
        self._attempts = 3 if patient else 1

    @property
    def has_credentials(self) -> bool:
        return bool(self._key)

    async def close(self) -> None:
        await self._client.aclose()

    async def warm(self, system: list[dict], tools: list[dict]) -> None:
        """OpenAI caches long prompts by itself, so there is nothing to do."""

    async def reply(
        self,
        system: list[dict],
        tools: list[dict],
        conversation: Conversation,
        on_text: Callable[[str], None] = lambda text: None,
    ) -> Reply:
        body = {**self._options, **to_openai(system, tools, conversation), "stream": True}
        problem = ""
        for attempt in range(1, self._attempts + 1):
            spoke = False

            def heard(text: str) -> None:
                nonlocal spoke
                spoke = True
                on_text(text)

            try:
                return await self._stream(body, heard)
            except _Retryable as exc:
                problem = str(exc)
            except httpx2.HTTPError as exc:
                problem = type(exc).__name__
            except ValueError as exc:
                raise LlmError(f"unreadable reply: {exc}") from exc
            # Text already passed on can't be taken back, so only a silent failure retries.
            if spoke or attempt == self._attempts:
                break
            log.warning("OpenAI failed (%s), retrying", problem)
        raise LlmError(problem)

    async def _stream(self, body: dict, on_text: Callable[[str], None]) -> Reply:
        headers = {"Authorization": f"Bearer {self._key}"}
        async with self._client.stream("POST", self._url, json=body, headers=headers) as response:
            if response.is_error:
                raise _problem(response.status_code, await response.aread())
            text, refusal, finish = [], [], None
            calls: dict[int, dict] = {}
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if error := chunk.get("error"):
                    raise _Retryable(f"stream error: {error.get('message', error)}")
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if piece := delta.get("content"):
                        text.append(piece)
                        on_text(piece)
                    if piece := delta.get("refusal"):
                        refusal.append(piece)
                    for call in delta.get("tool_calls") or []:
                        empty = {"id": "", "name": "", "args": ""}
                        slot = calls.setdefault(call.get("index", 0), empty)
                        function = call.get("function") or {}
                        slot["id"] = call.get("id") or slot["id"]
                        slot["name"] += function.get("name") or ""
                        slot["args"] += function.get("arguments") or ""
                    finish = choice.get("finish_reason") or finish
        if refusal:
            return Reply([], "refusal")
        content = [{"type": "text", "text": "".join(text)}] if text else []
        for index in sorted(calls):
            slot = calls[index]
            content.append(
                {
                    "type": "tool_use",
                    "id": slot["id"] or f"call_{index}",
                    "name": slot["name"],
                    "input": json.loads(slot["args"] or "{}"),
                }
            )
        stop = STOP_REASONS.get(finish or "", "end_turn")
        if calls and stop == "end_turn":
            stop = "tool_use"
        return Reply(content, stop)


class _Retryable(Exception):
    pass


def _problem(status: int, body: bytes) -> Exception:
    """An error to report, without the key. 4xx other than rate limits aren't retried."""
    try:
        error = json.loads(body).get("error") or {}
    except (ValueError, AttributeError):
        error = {}
    kind = error.get("code") or error.get("type") or ""
    message = f"HTTP {status}: {kind} {error.get('message', '')}".strip()[:300]
    if status == 429 or status >= 500:
        return _Retryable(message)
    return LlmError(message)
