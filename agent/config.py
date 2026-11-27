from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config import BASE_DIR


class AidaSettings(BaseSettings):
    """What the phone line and the chat share. Read from the store backend's .env."""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", env_ignore_empty=True, extra="ignore"
    )

    store_name: str = "Aida Demo Store"
    admin_name: str = "our store manager"
    backend_url: str = "http://localhost:8000"
    agent_api_key: SecretStr | None = None

    # Which AI thinks up Aida's answers: "anthropic" (Claude), "openai" or "gemini".
    llm_provider: Literal["anthropic", "openai", "gemini"] = "anthropic"

    # Claude
    anthropic_api_key: SecretStr | None = None
    anthropic_base_url: str | None = None
    # Not CLAUDE_EFFORT: Claude Code sets that one for itself in its terminals.
    aida_model: str = "claude-opus-5-5"
    aida_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    aida_fast_mode: bool = False

    # OpenAI, if LLM_PROVIDER is openai
    openai_api_key: SecretStr | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-5-mini"
    # For reasoning models (gpt-5 and o-series): minimal, low, medium or high. Leave it
    # empty for models that don't reason, such as gpt-4.1, which reject it.
    openai_reasoning_effort: str | None = "low"

    # Gemini, if LLM_PROVIDER is gemini (through Google's OpenAI-compatible API), and
    # for phone calls if VOICE_ENGINE is gemini_live. The key is made at aistudio.google.com.
    gemini_api_key: SecretStr | None = None
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    gemini_model: str = "gemini-3.8-flash"
    gemini_reasoning_effort: str | None = "low"
