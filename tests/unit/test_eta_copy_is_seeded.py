"""Every arrival line is seeded, trilingual and assembled in ONE place (honest-ETA spec 2026-11-03 §4.8).

`shared/eta_copy.py` picks which line tells the customer about the arrival: `promised_time_lines` for the promised
time (the in-transit message renders through it) and `track_lines` for the customer bot's Track screen. A line that
cannot be rendered is not said, and the lines are all or nothing. So misspell a key or rename a placeholder and the
customer silently loses the line. Drop one language's row and that language's customers silently read the line in
another language, because the lookup falls back across languages. Following
tests/unit/test_reorder_reminder_copy_is_seeded.py, this pins:

* every key the module renders is seeded in en, uz and ru, each language with exactly the placeholders it passes;
* the module, driven through every branch with a recording renderer, passes exactly those placeholders;
* `shared/eta_copy.py` is the ONLY product module that spells an arrival key;
* no line names the driver (the delivery message's sanitiser deletes such a line) or asks the customer to call;
* no line carries a legacy-Markdown character: the Track screen is sent as Markdown, and a stray `_` or `*` in one of
  these lines makes Telegram refuse the whole screen.
"""

from __future__ import annotations

import re
from pathlib import Path
from string import Formatter

import pytest

from business_app.services.notification_service import NotificationService
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS
from shared import eta_copy
from shared.constants import ETA_ATTRIBUTION_GOOGLE, ETA_STAGE_ARRIVED, ETA_STAGE_ON_THE_WAY
from shared.i18n_rendering import render_translation
from tests.unit.test_reorder_reminder_copy_is_seeded import _PHONE_PROMPT

pytestmark = pytest.mark.unit

LANGUAGES = ("en", "uz", "ru")
PREFIX = "telegram.orders.eta_"
REPO = Path(__file__).resolve().parents[2]
MODULE = REPO / "shared" / "eta_copy.py"

# key -> the placeholders the module passes. Written by hand, so a rename has to be made in three places on purpose;
# test_the_module_passes_exactly_these_placeholders ties it to what the code really passes.
KEYS = {
    "telegram.orders.eta_on_the_way": {"minutes", "time"},
    "telegram.orders.eta_arriving_now": set(),
    "telegram.orders.eta_google_attribution": set(),
    "telegram.orders.eta_on_the_way_no_time": set(),
    "telegram.orders.eta_arrived": set(),
}

# Values as `customer_eta` publishes them: the minutes left, and the "by" time as HH:MM.
VALUES = {"minutes": 29, "time": "14:29"}

_CYRILLIC = re.compile(r"[А-Яа-яЁё]")
_KEY_LITERAL = re.compile(r"telegram\.orders\.eta_[a-z0-9_]+")
_PRODUCT_TREES = ("business_app", "telegram_bot", "staff_bot", "shared")


def _placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(text) if name}


def _seeded(language: str):
    """`render(key, /, **values)` over the seed rows in `language`, through production's own renderer. Also the
    renderer of tests/unit/test_eta_copy.py."""

    def render(key, /, **values):
        return render_translation(key, BACKEND_TRANSLATIONS[key][language], (), values)

    return render


def _exercise(render) -> None:
    """Every branch the module's arguments select."""
    for minutes_left in (None, 0, 1, 29):
        for attribution in (None, ETA_ATTRIBUTION_GOOGLE):
            eta_copy.promised_time_lines(render, minutes_left=minutes_left, by_time="14:29", attribution=attribution)
            for stage in (ETA_STAGE_ON_THE_WAY, ETA_STAGE_ARRIVED, None):
                eta_copy.track_lines(
                    render, stage=stage, minutes_left=minutes_left, by_time="14:29", attribution=attribution
                )


@pytest.mark.parametrize("key", sorted(KEYS))
def test_every_arrival_key_is_seeded_in_all_three_languages(key):
    assert key in BACKEND_TRANSLATIONS, f"{key} is rendered but never seeded"
    for language in LANGUAGES:
        assert BACKEND_TRANSLATIONS[key].get(language), f"{key} has no {language} copy"


def test_the_seed_holds_no_arrival_key_this_file_does_not_check():
    seeded = {key for key in BACKEND_TRANSLATIONS if key.startswith(PREFIX)}
    assert sorted(seeded) == sorted(KEYS)


@pytest.mark.parametrize("key", sorted(KEYS))
def test_every_language_carries_exactly_the_same_placeholders(key):
    for language in LANGUAGES:
        assert _placeholders(BACKEND_TRANSLATIONS[key][language]) == KEYS[key], (key, language)


def test_the_module_passes_exactly_these_placeholders():
    passed: dict[str, list[set[str]]] = {}

    def recording(language):
        seeded = _seeded(language)

        def render(key, /, **values):
            passed.setdefault(key, []).append(set(values))
            return seeded(key, **values)

        return render

    for language in LANGUAGES:
        _exercise(recording(language))

    assert set(passed) == set(KEYS), f"never rendered: {sorted(set(KEYS) - set(passed))}"
    for key, calls in passed.items():
        for values in calls:
            assert values == KEYS[key], (key, values)


@pytest.mark.parametrize("key", sorted(KEYS))
def test_the_russian_copy_is_actually_russian(key):
    assert _CYRILLIC.search(BACKEND_TRANSLATIONS[key]["ru"]), BACKEND_TRANSLATIONS[key]["ru"]


@pytest.mark.parametrize("key", sorted(KEYS))
def test_no_arrival_line_names_the_driver_or_asks_the_customer_to_call(key):
    """The in-transit message passes every line through production's driver-info sanitiser, which deletes a line that
    names the driver or starts like a phone number. Every arrival line comes through it whole."""
    for language in LANGUAGES:
        line = _seeded(language)(key, **{name: VALUES[name] for name in KEYS[key]})
        assert NotificationService._strip_driver_info_from_delivery_message(line) == line, (key, language)
        assert not _PHONE_PROMPT.search(BACKEND_TRANSLATIONS[key][language]), (key, language)


@pytest.mark.parametrize("key", sorted(KEYS))
def test_no_arrival_line_carries_a_legacy_markdown_character(key):
    """The Track screen is sent with parse_mode Markdown (legacy), where `_`, `*`, a backtick and `[` open an entity.
    One of them in a line leaves the entity unclosed and Telegram refuses the whole screen."""
    for language in LANGUAGES:
        assert not set(BACKEND_TRANSLATIONS[key][language]) & set("_*`["), (key, language)


def test_the_module_spells_exactly_the_seeded_keys():
    assert set(_KEY_LITERAL.findall(MODULE.read_text(encoding="utf-8"))) == set(KEYS)


def test_no_other_product_module_spells_an_arrival_key():
    offenders = []
    for tree in _PRODUCT_TREES:
        for path in (REPO / tree).rglob("*"):
            if path == MODULE or path.suffix not in {".py", ".html", ".js"} or "node_modules" in path.parts:
                continue
            if _KEY_LITERAL.search(path.read_text(encoding="utf-8", errors="ignore")):
                offenders.append(str(path.relative_to(REPO)))
    assert offenders == [], (
        "only shared/eta_copy.py may name an arrival key; render through it instead: " f"{offenders}"
    )
