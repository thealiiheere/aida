"""Telling English, Uzbek and Russian apart, and speaking Russian numbers."""

import pytest

from agent.language import from_telegram, language_of
from agent.prompts import voice_prompt
from voice.session import LINES
from voice.speech import russian_number_words, speakable


@pytest.mark.parametrize(
    "text, default, language",
    [
        ("Здравствуйте, у вас есть кружки?", "uz", "ru"),
        ("Салом, қанча туради?", "ru", "uz"),  # Uzbek in Cyrillic
        ("Assalomu alaykum, quloqchin bormi?", "en", "uz"),
        ("Narxi qirq to'qqiz dollar.", "en", "uz"),
        ("Zo‘r tanlov!", "en", "uz"),
        ("Wireless Earbuds bor.", "en", "uz"),
        ("Do you have mugs?", "uz", "en"),
        ("Wireless Earbuds.", "uz", "uz"),  # can't tell: stay
        ("Wireless Earbuds.", "ru", "en"),  # Latin script, so not Russian
        ("12345", "ru", "ru"),
        ("", "uz", "uz"),
    ],
)
def test_language_of(text, default, language):
    assert language_of(text, default) == language


def test_telegram_language_codes():
    assert from_telegram("ru") == "ru"
    assert from_telegram("uz-UZ") == "uz"
    assert from_telegram("de", "uz") == "uz"
    assert from_telegram(None, "ru") == "ru"


@pytest.mark.parametrize(
    "n, words",
    [
        (0, "ноль"), (1, "один"), (11, "одиннадцать"), (21, "двадцать один"),
        (121, "сто двадцать один"), (2000, "две тысячи"), (5001, "пять тысяч один"),
        (21_000, "двадцать одна тысяча"), (1_000_001, "один миллион один"),
    ],
)  # fmt: skip
def test_russian_number_words(n, words):
    assert russian_number_words(n) == words


@pytest.mark.parametrize(
    "text, spoken",
    [
        ("Цена $49.99.", "Цена сорок девять долларов девяносто девять центов."),
        ("$1, $2, $5, $21 и $0.01", "один доллар, два доллара, пять долларов, двадцать один доллар и один цент"),
        ("Заказ #12 готов", "Заказ номер двенадцать готов"),
        ("Скидка 10%", "Скидка десять процентов"),
        ("В 10:30 или 9:05", "В десять тридцать или девять ноль пять"),
        ("+998901234567", "плюс девять девять восемь девять ноль один два три четыре пять шесть семь"),
        ("Оплата: https://pay.example/x", "Оплата: ссылка"),
    ],
)  # fmt: skip
def test_russian_speakable(text, spoken):
    assert speakable(text, "ru") == spoken


def test_a_comma_after_a_price_is_kept():
    assert speakable("$1, $2 or $1,250.") == (
        "one dollar, two dollars or one thousand two hundred fifty dollars."
    )


def test_russian_prompt_and_lines():
    english = voice_prompt("Test Store", "Kim")
    russian = voice_prompt("Test Store", "Kim", "ru")
    assert russian.startswith(english) and "everything you say is in Russian" in russian
    lines = LINES["ru"]
    assert lines.greeting.format(store="Test Store").startswith("Здравствуйте! Добро пожаловать")
    for line in (lines.greeting, lines.hiccup, lines.refused, lines.silent, *lines.fillers):
        assert speakable(line, "ru") == line
        assert language_of(line, "uz") == "ru"
    for line in (*LINES["uz"].fillers, LINES["uz"].greeting, LINES["uz"].hiccup):
        assert language_of(line, "en") == "uz"
