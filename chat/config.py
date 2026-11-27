from pathlib import Path
from typing import Literal

from pydantic import SecretStr

from agent.config import AidaSettings
from app.config import BASE_DIR


class ChatSettings(AidaSettings):
    telegram_bot_token: SecretStr | None = None
    telegram_api_url: str = "https://api.telegram.org"
    admin_chat_id: str | None = None
    chat_database: Path = BASE_DIR / "chat.db"
    # The fixed lines customers get, such as the greeting: "en", or "uz" for Uzbek.
    # Aida's own answers are always in the language the customer writes in.
    chat_language: Literal["en", "uz", "ru"] = "en"
