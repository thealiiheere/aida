"""Turning model text into speakable sentences, and judging what the caller said."""

import re

# --- Sentences ----------------------------------------------------------------

_BOUNDARY = re.compile(r"""[.?!…]+["'”’)\]]*(?=\s)|\n""")
_LAST_WORD = re.compile(r"(\S+)$")
_ABBREVIATIONS = set("mr mrs ms dr st ave apt no vs etc approx e.g i.e jr sr".split())


class SentenceChunker:
    """Collects streamed text and releases each sentence as soon as it is complete.

    A sentence ends at . ? or ! followed by whitespace, so "49.99" and "St. Petersburg
    Street" stay whole. Joining everything released gives back the exact input.
    """

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, text: str) -> list[str]:
        self._buffer += text
        sentences = []
        start = 0
        for match in _BOUNDARY.finditer(self._buffer):
            candidate = self._buffer[start : match.end()]
            if candidate.strip() and not _is_abbreviation(candidate):
                sentences.append(candidate)
                start = match.end()
        self._buffer = self._buffer[start:]
        return sentences

    def flush(self) -> list[str]:
        rest, self._buffer = self._buffer, ""
        return [rest] if rest.strip() else []


def _is_abbreviation(text: str) -> bool:
    text = text.rstrip()
    if not text.endswith("."):
        return False
    word = _LAST_WORD.search(text).group(1).rstrip(".").lower()
    initial = len(word) == 1 and word.isalpha() and word != "i"
    list_number = word.isdigit() and text.strip() == f"{word}."
    return word in _ABBREVIATIONS or initial or list_number


# --- Speakable text -----------------------------------------------------------

_ONES = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen"
).split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
_SCALES = ((1_000_000, "million"), (1_000, "thousand"), (100, "hundred"))


def number_words(n: int) -> str:
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] + (f"-{_ONES[ones]}" if ones else "")
    for size, name in _SCALES:
        if n >= size:
            count, rest = divmod(n, size)
            words = f"{number_words(count)} {name}"
            return f"{words} {number_words(rest)}" if rest else words
    raise AssertionError("unreachable")


def uzbek_number_words(n: int) -> str:
    if n < 10:
        return _UZ_ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _UZ_TENS[tens] + (f" {_UZ_ONES[ones]}" if ones else "")
    for size, name in _UZ_SCALES:
        if n >= size:
            count, rest = divmod(n, size)
            # "yuz", "ming": one of them goes without "bir", as Uzbek is spoken.
            if count == 1 and size < 1_000_000:
                words = name
            else:
                words = f"{uzbek_number_words(count)} {name}"
            return f"{words} {uzbek_number_words(rest)}" if rest else words
    raise AssertionError("unreachable")


_UZ_ONES = "nol bir ikki uch to'rt besh olti yetti sakkiz to'qqiz".split()
_UZ_TENS = "_ o'n yigirma o'ttiz qirq ellik oltmish yetmish sakson to'qson".split()
_UZ_SCALES = ((1_000_000, "million"), (1_000, "ming"), (100, "yuz"))


def russian_form(n: int, one: str, few: str, many: str) -> str:
    """The noun after a number: 1 доллар, 2 доллара, 5 долларов, 21 доллар."""
    if n % 100 in range(11, 15):
        return many
    return {1: one, 2: few, 3: few, 4: few}.get(n % 10, many)


def russian_number_words(n: int, feminine: bool = False) -> str:
    if n == 0:
        return "ноль"
    parts = []
    for size, forms, female in _RU_SCALES:
        count, n = divmod(n, size)
        if count:
            parts.append(russian_number_words(count, female))
            parts.append(russian_form(count, *forms))
    hundreds, rest = divmod(n, 100)
    if hundreds:
        parts.append(_RU_HUNDREDS[hundreds])
    if rest >= 20:
        tens, rest = divmod(rest, 10)
        parts.append(_RU_TENS[tens])
    if rest:
        ones = _RU_ONES[rest]
        if feminine and rest in (1, 2):
            ones = ("одна", "две")[rest - 1]
        parts.append(ones)
    return " ".join(parts)


_RU_ONES = (
    "ноль один два три четыре пять шесть семь восемь девять десять одиннадцать "
    "двенадцать тринадцать четырнадцать пятнадцать шестнадцать семнадцать восемнадцать "
    "девятнадцать"
).split()
_RU_TENS = (
    "_ _ двадцать тридцать сорок пятьдесят шестьдесят семьдесят восемьдесят девяносто".split()
)
_RU_HUNDREDS = (
    "_ сто двести триста четыреста пятьсот шестьсот семьсот восемьсот девятьсот".split()
)
_RU_SCALES = (
    (1_000_000, ("миллион", "миллиона", "миллионов"), False),
    (1_000, ("тысяча", "тысячи", "тысяч"), True),
)


class _Words:
    """How one language says what text writes with symbols."""

    def __init__(self, language: str):
        self.language = language if language in ("en", "uz", "ru") else "en"
        self.number = {"uz": uzbek_number_words, "ru": russian_number_words}.get(
            self.language, number_words
        )
        self.ones = {"uz": _UZ_ONES, "ru": _RU_ONES}.get(self.language, _ONES)

    def digits(self, text: str) -> str:
        return " ".join(self.ones[int(d)] for d in text if d.isdigit())

    def money(self, match: re.Match) -> str:
        dollars = int(match[1].replace(",", ""))
        cents = int((match[2] or "0").ljust(2, "0"))
        parts = []
        if self.language == "ru":
            if dollars or not cents:
                form = russian_form(dollars, "доллар", "доллара", "долларов")
                parts.append(f"{self.number(dollars)} {form}")
            if cents:
                form = russian_form(cents, "цент", "цента", "центов")
                parts.append(f"{self.number(cents)} {form}")
            return " ".join(parts)
        if self.language == "uz":
            if dollars or not cents:
                parts.append(f"{self.number(dollars)} dollar")
            if cents:
                parts.append(f"{self.number(cents)} sent")
            return " ".join(parts)
        if dollars or not cents:
            parts.append(f"{self.number(dollars)} dollar{'' if dollars == 1 else 's'}")
        if cents:
            parts.append(f"{self.number(cents)} cent{'' if cents == 1 else 's'}")
        return " and ".join(parts)

    def integer(self, match: re.Match) -> str:
        text = match[0].replace(",", "")
        # Phone numbers and codes are read digit by digit.
        if len(text) > 6 or text.startswith("0"):
            return self.digits(text)
        return self.number(int(text))

    def clock(self, match: re.Match) -> str:
        hours, minutes = self.number(int(match[1])), match[2]
        if minutes == "00":
            return hours
        zero = {"uz": "nol ", "ru": "ноль "}.get(self.language, "oh ") if minutes[0] == "0" else ""
        return f"{hours} {zero}{self.number(int(minutes))}"

    def decimal(self, match: re.Match) -> str:
        point = {"uz": "butun", "ru": "и"}.get(self.language, "point")
        return f"{self.number(int(match[1]))} {point} {self.digits(match[2])}"

    @property
    def symbols(self) -> dict[str, str]:
        return {
            "en": {"link": "the link", "number": "number ", "plus": "plus ", "%": " percent",
                   "&": " and "},
            "uz": {"link": "havola", "number": "raqam ", "plus": "plyus ", "%": " foiz",
                   "&": " va "},
            "ru": {"link": "ссылка", "number": "номер ", "plus": "плюс ", "%": " процентов",
                   "&": " и "},
        }[self.language]  # fmt: skip


def speakable(text: str, language: str = "en") -> str:
    """What to send to text-to-speech. A safety net: the prompt already asks for this.

    Neither Flash v2.5 nor, as far as its docs say, VoiceLab expands numbers or
    currency, so "$49.99" has to arrive as words.
    """
    words = _Words(language)
    symbols = words.symbols
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)  # [label](url)
    text = re.sub(r"(https?://|www\.)\S*[^\s.,!?;:)]", symbols["link"], text)
    text = re.sub(r"^\s*(?:#{1,6}\s+|[-*•]\s+|\d+[.)]\s+(?=\S))", "", text, flags=re.MULTILINE)
    text = re.sub(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?(?!\d)", words.money, text)
    text = re.sub(r"#\s?(?=\d)", symbols["number"], text)
    text = re.sub(r"\+(?=\d)", symbols["plus"], text)
    text = re.sub(r"\b(\d{1,2}):(\d{2})\b", words.clock, text)
    text = re.sub(r"\b(\d+)\.(\d+)\b", words.decimal, text)
    text = re.sub(
        r"(?<![\w.])\d{1,3}(?:,\d{3})+(?!\w)|(?<![\w.])\d+(?!\w)", words.integer, text
    )
    text = text.replace("%", symbols["%"]).replace("&", symbols["&"])
    text = re.sub(r"[*_`#~|<>\\]", " ", text)
    text = re.sub(r"[🀀-🫿☀-➿️‍]", "", text)
    text = " ".join(text.split())
    return text if any(c.isalnum() for c in text) else ""


# --- The caller ---------------------------------------------------------------

# Noises people make to show they're listening.
_BACKCHANNEL_WORDS = set(
    "uh huh uhhuh mm mhm hmm hm mmhmm ah oh aha um er yeah yep yup yes ok okay right sure "
    "alright cool great good nice "
    # Uzbek, with the apostrophes taken out: xo'p, zo'r.
    "ha xa haa mayli xop tushunarli tushundim albatta yaxshi zor aha okey".split()
)
_BACKCHANNEL_PHRASES = {"i see", "got it", "go on", "all right"}


def is_backchannel(text: str) -> bool:
    """True for "uh-huh", "yeah yeah", "okay, I see": listening, not taking a turn."""
    words = re.findall(r"[^\W\d_]+", re.sub(r"['`\u2018\u2019\u02bb\u02bc]", "", text.casefold()))
    if not words or len(words) > 4:
        return False
    joined = " ".join(words)
    for phrase in _BACKCHANNEL_PHRASES:
        joined = re.sub(rf"\b{phrase}\b", "", joined)
    return all(word in _BACKCHANNEL_WORDS for word in joined.split())


def clean_transcript(text: str) -> str:
    """One line, without the [brackets] reserved for notes from the phone system."""
    return " ".join(re.sub(r"[\[\]]", " ", text).split())[:1000]
