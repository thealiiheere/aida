"""Which of Aida's languages a piece of text is in: English, Uzbek or Russian.

Only used to pick fixed lines, number words and the text-to-speech language. Claude
itself always answers in the customer's language; this just keeps the rest in step.
"""

import re

LANGUAGES = ("en", "uz", "ru")

_CYRILLIC = re.compile(r"[Ѐ-ӿ]")
_LATIN = re.compile(r"[A-Za-z]")
# Letters only Uzbek's Cyrillic alphabet has.
_UZBEK_CYRILLIC = set("ўқғҳЎҚҒҲ")
# o' and g' (with any of the apostrophes people type), and sh/ch words are too common
# in English to count, so words are used as well.
_UZBEK_LETTERS = re.compile(r"[oOgG][‘’ʻʼ'`]")
_UZBEK_WORDS = set(
    "va bor yo'q yoq siz sizga sizni men menga biz uchun bilan rahmat qanday qancha nima "
    "necha kerak bormi yaxshi assalomu alaykum salom xayr iltimos dona narxi buyurtma "
    "manzil kechirasiz mumkin emas edi bo'ladi boladi ha bir daqiqa soniya hozir "
    "tekshiraman qayerga qachon".split()
)
_ENGLISH_WORDS = set(
    "the and you your is are have has do does what how much please thanks thank hello hi "
    "want need can could would order price i we it this that".split()
)


def language_of(text: str, default: str = "en") -> str:
    """The language of the text, or the default when it can't be told."""
    cyrillic = len(_CYRILLIC.findall(text))
    latin = len(_LATIN.findall(text))
    if cyrillic > latin:
        return "uz" if _UZBEK_CYRILLIC & set(text) else "ru"
    if not latin:
        return default
    words = set(re.findall(r"[a-z'‘’ʻʼ`]+", text.lower()))
    words = {re.sub(r"[‘’ʻʼ`]", "'", w) for w in words}
    uzbek = len(words & _UZBEK_WORDS) + len(_UZBEK_LETTERS.findall(text))
    english = len(words & _ENGLISH_WORDS)
    if uzbek > english:
        return "uz"
    if english > uzbek:
        return "en"
    return default if default != "ru" else "en"  # Latin script, so not Russian


def from_telegram(code: str | None, default: str = "en") -> str:
    """A Telegram language_code such as "ru" or "uz-UZ", as one of Aida's languages."""
    language = (code or "").split("-")[0].lower()
    return language if language in LANGUAGES else default
