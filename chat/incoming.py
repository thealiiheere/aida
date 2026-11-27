"""Turns a Telegram message into what Aida should read."""

import re
from dataclasses import dataclass

MAX_TEXT_CHARS = 2000
MAX_QUOTE_CHARS = 300

# [Square brackets] are for notes from the system, so customers' own become (round).
# Their look-alikes do too.
_ROUND = str.maketrans("[]［］【】〔〕⟦⟧〚〛", "()()()()()()")

_UNREADABLE = {
    "photo": "a photo",
    "voice": "a voice message",
    "video_note": "a video message",
    "video": "a video",
    "animation": "a GIF",
    "audio": "an audio file",
    "document": "a file",
    "sticker": "a sticker",
    "contact": "a contact card",
    "poll": "a poll",
    "story": "a story",
    "dice": "an animated emoji",
    "paid_media": "a photo or video",
}


@dataclass(frozen=True)
class Incoming:
    text: str = ""
    notes: tuple[str, ...] = ()
    start: bool = False  # the customer pressed Start
    language: str | None = None  # their Telegram app's language, such as "ru"


def clean(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    text = text.translate(_ROUND).strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def read(message: dict) -> Incoming | None:
    """None if there is nothing to answer, such as a pinned message."""
    text = message.get("text") or message.get("caption") or ""
    language = (message.get("from") or {}).get("language_code")
    if re.match(r"/start(@\w+)?(\s|$)", text):
        return Incoming(start=True, language=language)
    notes = []
    quoted = (message.get("quote") or {}).get("text") or _text(message.get("reply_to_message"))
    if quoted:
        quoted = " ".join(clean(quoted, MAX_QUOTE_CHARS).split())
        notes.append(f"This answers an earlier message in the chat: “{quoted}”")
    if note := _attachment(message):
        notes.append(note)
    elif not text.strip():
        return None
    return Incoming(clean(text), tuple(notes), language=language)


def _text(message: dict | None) -> str:
    return (message or {}).get("text") or (message or {}).get("caption") or ""


def _attachment(message: dict) -> str | None:
    if place := message.get("venue"):
        spot = place.get("location") or {}
        name = clean(f"{place.get('title', '')}, {place.get('address', '')}", MAX_QUOTE_CHARS)
        return f"The customer shared a place: {' '.join(name.split())} ({_coordinates(spot)})"
    if spot := message.get("location"):
        return f"The customer shared a location: {_coordinates(spot)}"
    for kind, name in _UNREADABLE.items():
        if kind in message:
            return f"The customer sent {name}. You can't open it"
    return None


def _coordinates(location: dict) -> str:
    return f"{location.get('latitude')}, {location.get('longitude')}"
