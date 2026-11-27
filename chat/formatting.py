"""Aida's Markdown as Telegram shows it, and messages short enough for Telegram."""

import re
from html import escape

# Telegram counts 4096 UTF-16 units, and one character is two at most.
MAX_CHARS = 2000

# Kept as they are: underscores in a link aren't italics.
_LITERAL = re.compile(
    r"```(?:\w+\n)?(?P<block>.+?)```"
    r"|`(?P<code>[^`\n]+)`"
    r"|\[(?P<label>[^\[\]\n]+)\]\((?P<url>https?://[^\s()<>]+)\)"
    r"|(?P<bare>https?://[^\s<>*~]*[^\s<>*~.,;:!?)\]'\"])",
    re.DOTALL,
)
_SLOT = re.compile("\ue000(\\d+)\ue001")
_STYLES = (
    (re.compile(r"^#{1,6} +(.+?) *#*$", re.MULTILINE), r"<b>\1</b>"),
    (re.compile(r"^( *)[-*+] +", re.MULTILINE), r"\1• "),
    (re.compile(r"\*\*\*(?=\S)([^\n]+?)(?<=\S)\*\*\*"), r"<b><i>\1</i></b>"),
    (re.compile(r"\*\*(?=\S)([^\n]+?)(?<=\S)\*\*"), r"<b>\1</b>"),
    (re.compile(r"(?<!\w)__(?=\S)([^\n]+?)(?<=\S)__(?!\w)"), r"<b>\1</b>"),
    (re.compile(r"~~(?=\S)([^\n]+?)(?<=\S)~~"), r"<s>\1</s>"),
    (re.compile(r"(?<![\w*])\*(?=[^\s*])([^*\n]+?)(?<=\S)\*(?![\w*])"), r"<i>\1</i>"),
    (re.compile(r"(?<!\w)_(?=[^\s_])([^_\n]+?)(?<=\S)_(?!\w)"), r"<i>\1</i>"),
)


def to_html(markdown: str) -> str:
    """Telegram's HTML. Marks that aren't a pair are left as the customer would see them."""
    kept: list[str] = []

    def keep(match: re.Match) -> str:
        if text := match["block"] or match["code"]:
            tag = "pre" if match["block"] else "code"
            kept.append(f"<{tag}>{escape(text.strip())}</{tag}>")
        elif match["url"]:
            kept.append(f'<a href="{escape(match["url"])}">{escape(match["label"])}</a>')
        else:
            kept.append(escape(match["bare"]))
        return f"\ue000{len(kept) - 1}\ue001"

    text = escape(_LITERAL.sub(keep, re.sub("[\ue000\ue001]", "", markdown)), quote=False)
    for pattern, replacement in _STYLES:
        text = pattern.sub(replacement, text)
    return _SLOT.sub(lambda match: kept[int(match[1])], text)


def split(text: str) -> list[str]:
    """Pieces Telegram will take, broken between paragraphs, lines or words if possible."""
    pieces = []
    text = text.strip()
    while len(text) > MAX_CHARS:
        cut = -1
        for gap in ("\n\n", "\n", " "):
            cut = text.rfind(gap, MAX_CHARS // 2, MAX_CHARS)
            if cut > 0:
                break
        cut = cut if cut > 0 else MAX_CHARS
        pieces.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return [*pieces, text] if text else pieces
