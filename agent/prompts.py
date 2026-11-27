"""Aida's full system prompts: the channel's own part, then the shared guardrails."""

from pathlib import Path

FOLDER = Path(__file__).parent


def chat_prompt(store_name: str, admin_name: str) -> str:
    return _build("system_prompt.md", store_name, admin_name)


def voice_prompt(store_name: str, admin_name: str, language: str = "en") -> str:
    extra = {"uz": ("voice_uzbek.md",), "ru": ("voice_russian.md",)}.get(language, ())
    return _build("voice_system_prompt.md", store_name, admin_name, extra)


def _build(file: str, store_name: str, admin_name: str, extra: tuple[str, ...] = ()) -> str:
    names = (file, "guardrails.md", *extra)
    parts = [(FOLDER / name).read_text(encoding="utf-8") for name in names]
    text = "\n".join(parts)
    return text.replace("[Store Name]", store_name).replace("[Admin Name]", admin_name)
