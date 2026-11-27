"""Streams Claude's replies.

The tool loops are hand-written (voice/session.py, chat/aida.py) rather than the SDK's
tool runner: a caller can interrupt between any two tokens, and a chat is saved between
any two steps.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass

import anthropic
import httpx2
from anthropic import AsyncAnthropic

from .claude import refusal_fallback
from .config import AidaSettings
from .history import Conversation

log = logging.getLogger("aida.llm")

# Room for thinking plus a few sentences and a tool call.
MAX_TOKENS = 4096


class LlmError(Exception):
    pass


@dataclass(frozen=True)
class Reply:
    content: list[dict]
    stop_reason: str | None


def make_llm(settings: AidaSettings, *, patient: bool = False):
    """Claude, OpenAI or Gemini, whichever LLM_PROVIDER names. All have the same methods."""
    if settings.llm_provider in ("openai", "gemini"):
        from .openai_llm import OpenAiLlm

        return OpenAiLlm(settings, patient=patient)
    return Claude(settings, patient=patient)


class Claude:
    key_name = "ANTHROPIC_API_KEY"

    def __init__(
        self,
        settings: AidaSettings,
        http_client: httpx2.AsyncClient | None = None,
        *,
        patient: bool = False,
    ):
        key = settings.anthropic_api_key
        self._client = AsyncAnthropic(
            # Without a key here, the SDK looks for credentials in the environment.
            api_key=key.get_secret_value() if key else None,
            base_url=settings.anthropic_base_url,
            # Someone typing can wait. Someone on the phone hears the silence.
            timeout=(
                anthropic.Timeout(120, connect=5, read=60)
                if patient
                else anthropic.Timeout(30, connect=3, read=10)
            ),
            max_retries=3 if patient else 1,
            http_client=http_client,
        )
        self._common = {
            "model": settings.aida_model,
            # Low effort keeps the wait before the first word short.
            "output_config": {"effort": settings.aida_effort},
        }
        self._extra = {}
        if settings.aida_fast_mode:  # which can't be combined with a fallback
            self._common["speed"] = "fast"
        else:
            self._extra = refusal_fallback(settings.aida_model)

    @property
    def has_credentials(self) -> bool:
        c = self._client
        return bool(c.api_key or c.auth_token or c.credentials)

    async def close(self) -> None:
        await self._client.close()

    async def warm(self, system: list[dict], tools: list[dict]) -> None:
        """Put the prompt in the cache while the greeting plays. Failure is harmless."""
        try:
            await self._client.beta.messages.create(
                **self._common,
                max_tokens=0,
                system=system,
                tools=tools,
                messages=[{"role": "user", "content": "warmup"}],
            )
        except (anthropic.APIError, httpx2.HTTPError) as exc:
            log.info("prompt cache not warmed: %s", exc)

    async def reply(
        self,
        system: list[dict],
        tools: list[dict],
        conversation: Conversation,
        on_text: Callable[[str], None] = lambda text: None,
    ) -> Reply:
        try:
            try:
                return await self._stream(system, tools, conversation, on_text)
            except anthropic.BadRequestError as exc:
                if "thinking" not in str(exc):
                    raise
                # Rejected before any output, so nothing has been spoken yet.
                log.warning("a thinking block was rejected, retrying without them: %s", exc)
                conversation.strip_thinking()
                return await self._stream(system, tools, conversation, on_text)
        # The SDK raises ValueError for a stream it can't assemble, and lets a
        # connection that dies mid-stream through as a plain HTTP error.
        except (anthropic.APIError, httpx2.HTTPError, ValueError) as exc:
            raise LlmError(f"{type(exc).__name__}: {exc}") from exc

    async def _stream(
        self,
        system: list[dict],
        tools: list[dict],
        conversation: Conversation,
        on_text: Callable[[str], None],
    ) -> Reply:
        # eager_input_streaming stays off: tool inputs here are tiny, and validated
        # JSON matters more than speed when the input is an order.
        async with self._client.beta.messages.stream(
            **self._common,
            **self._extra,
            max_tokens=MAX_TOKENS,
            system=system,
            tools=tools,
            messages=list(conversation.messages),
            cache_control={"type": "ephemeral"},
        ) as stream:
            async for event in stream:
                if event.type == "text":
                    on_text(event.text)
            message = await stream.get_final_message()
        return Reply([block.to_dict() for block in message.content], message.stop_reason)
