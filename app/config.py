from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", env_ignore_empty=True, extra="ignore"
    )

    store_name: str = "Aida Demo Store"
    # What the admin's Telegram messages, the console and the checkout page are in, and
    # what customers get when they haven't written to the bot: en, uz or ru.
    store_language: Literal["en", "uz", "ru"] = "en"
    database_url: str = f"sqlite:///{(BASE_DIR / 'aida.db').as_posix()}"
    payment_base_url: str = "http://localhost:8000/pay"
    agent_api_key: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_api_url: str = "https://api.telegram.org"
    admin_chat_id: str | None = None
    admin_username: str | None = None

    # Real payments. Prices are in dollars; Click and Payme charge in soum.
    uzs_per_usd: Decimal | None = Field(default=None, gt=0)
    click_service_id: str | None = None
    click_secret_key: SecretStr | None = None
    payme_login: str = "Paycom"
    payme_key: SecretStr | None = None

    # The Telegram chat's conversations, which the console reads. Same as the chat's own.
    chat_database: Path = BASE_DIR / "chat.db"

    # Call summaries, by whichever AI LLM_PROVIDER names
    llm_provider: Literal["anthropic", "openai", "gemini"] = "anthropic"
    openai_api_key: SecretStr | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-5-mini"
    openai_reasoning_effort: str | None = "low"
    # Gemini, if LLM_PROVIDER is gemini (through Google's OpenAI-compatible API), and
    # for phone calls if VOICE_ENGINE is gemini_live. The key is made at aistudio.google.com.
    gemini_api_key: SecretStr | None = None
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    gemini_model: str = "gemini-3.8-flash"
    gemini_reasoning_effort: str | None = "low"
    anthropic_api_key: SecretStr | None = None
    anthropic_base_url: str | None = None
    summary_model: str = "claude-opus-5-5"

    @property
    def real_payments(self) -> bool:
        return bool(self.click_secret_key or self.payme_key)

    def tiyin(self, dollars: Decimal) -> int:
        """A price in hundredths of a soum, rounded to a whole soum."""
        return int((dollars * self.uzs_per_usd).quantize(Decimal(1), ROUND_HALF_UP)) * 100


@lru_cache
def get_settings() -> Settings:
    return Settings()
