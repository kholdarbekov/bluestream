"""The copy plan 2026-11-12 seeds: every key it seeds (the five dicts below) has a value in all three languages with
matching placeholders; every offer key a frontend template renders is seeded; offer copy types no digit; and the two
fact sentences are composed only in the price service.

The page tests read their expectations through `shipped()` and `seed_copy()`, which fall back to English for a missing
language, so a lost uz or ru value (or a lost placeholder) would pass there: the checks below are the guard."""

import re
from pathlib import Path

from scripts.seed_backend_translations import (
    BACKEND_TRANSLATIONS,
    DELIVERY_COPY_CORRECTIONS,
    DELIVERY_PAGE_TRANSLATIONS,
    FAQ_DELIVERY_TRANSLATIONS,
    OFFER_BLOCK_TRANSLATIONS,
    ORDER_AND_TERMS_TRANSLATIONS,
)
from tests.unit.test_cabinet_template_keys_are_seeded import _KEY_RE

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "business_app" / "templates"
FRONTEND = TEMPLATES / "frontend"
SERVICE = ROOT / "business_app" / "services" / "public_offer_service.py"
OFFER = ("landing.offer.", "landing.pricing_policy.")
LANGS = ("en", "uz", "ru")


PLAN_DICTS = (
    OFFER_BLOCK_TRANSLATIONS,
    DELIVERY_PAGE_TRANSLATIONS,
    FAQ_DELIVERY_TRANSLATIONS,
    DELIVERY_COPY_CORRECTIONS,
    ORDER_AND_TERMS_TRANSLATIONS,
)


def _offer_rows():
    return {key: row for key, row in BACKEND_TRANSLATIONS.items() if key.startswith(OFFER)}


def _plan_rows():
    """Every key the plan seeds, by the five dicts that hold them."""
    return {key: row for rows in PLAN_DICTS for key, row in rows.items()}


def _rendered_offer_keys():
    keys = set()
    for path in [*FRONTEND.rglob("*.html"), *FRONTEND.rglob("*.txt")]:
        keys.update(m.group(2) for m in _KEY_RE.finditer(path.read_text(encoding="utf-8")) if m.group(2).startswith(OFFER))
    return keys


def test_every_offer_key_a_template_renders_is_seeded():
    keys = _rendered_offer_keys()
    assert keys, "the scan found no offer key: the test is blind"
    for key in sorted(keys):
        row = BACKEND_TRANSLATIONS.get(key)
        assert row is not None, f"{key} is rendered but not seeded"
        for language in LANGS:
            assert (row.get(language) or "").strip(), f"{key} has no {language} value"


def test_every_key_the_plan_seeds_has_a_value_in_all_three_languages():
    rows = _plan_rows()
    assert all(PLAN_DICTS), "a dict is empty: the test is blind"
    for key, row in rows.items():
        for language in LANGS:
            assert (row.get(language) or "").strip(), f"{key} has no {language} value"


def test_seeded_copy_placeholders_match_in_all_three_languages():
    for key, row in {**_offer_rows(), **_plan_rows()}.items():
        sets = {language: set(re.findall(r"{(\w+)}", row[language])) for language in LANGS}
        assert sets["en"] == sets["uz"] == sets["ru"], f"{key}: placeholders differ {sets}"
        for language in LANGS:
            value = row[language]
            assert value.count("{") == value.count("}") == len(re.findall(r"{\w+}", value)), f"{key} {language}"
        assert not sets["en"] & {"key", "language"}, key


def test_no_offer_copy_types_a_digit():
    for key, row in _offer_rows().items():
        for language in LANGS:
            assert not re.search(r"\d", row[language]), f"{key} ({language}) types a digit"


def test_two_fact_sentences_are_composed_only_in_the_service():
    for literal in ("landing.offer.price_line", "landing.offer.smallest_order", "landing.offer.upcoming_price"):
        owners = [
            path
            for path in [*TEMPLATES.rglob("*"), *(ROOT / "business_app").rglob("*.py")]
            if path.is_file() and path.suffix in {".html", ".txt", ".py"} and literal in path.read_text(encoding="utf-8")
        ]
        assert owners == [SERVICE], f"{literal} is composed in {owners}"


def test_the_upcoming_price_line_is_seeded_with_date_and_price():
    row = _offer_rows()["landing.offer.upcoming_price"]
    for language in LANGS:
        assert re.findall(r"{(\w+)}", row[language]) == ["date", "price"], language
