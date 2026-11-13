"""The admin accuracy page's copy: the seed and the page's inline defaults agree, in all three languages."""

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SEED = ROOT / "scripts" / "seed_ui_eta_accuracy_translations.py"
PAGE = ROOT / "admin_ui" / "src" / "pages" / "EtaAccuracy.js"
LANGUAGES = ("en", "uz", "ru")
TOKEN = re.compile(r"\{\{\s*(\w+)\s*\}\}")
FIRST_HALF_KEY = "eta_acc_first_half_same_day"


def _translations():
    spec = importlib.util.spec_from_file_location("_seed_ui_eta_accuracy", SEED)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.UI_ETA_ACCURACY_TRANSLATIONS


TRANSLATIONS = _translations()


def test_every_language_keeps_the_pages_tokens():
    assert set(TRANSLATIONS["uz"]) == set(TRANSLATIONS["en"]) == set(TRANSLATIONS["ru"])
    for key, english in TRANSLATIONS["en"].items():
        tokens = set(TOKEN.findall(english))
        for language in ("uz", "ru"):
            assert set(TOKEN.findall(TRANSLATIONS[language][key])) == tokens, (key, language)


def test_every_english_value_is_the_pages_default():
    page = PAGE.read_text(encoding="utf-8")
    for key, value in TRANSLATIONS["en"].items():
        assert f"'{value}'" in page, key


def test_the_first_half_line_states_no_hour():
    for language in LANGUAGES:
        assert not re.search(r"\d", TRANSLATIONS[language][FIRST_HALF_KEY]), language
