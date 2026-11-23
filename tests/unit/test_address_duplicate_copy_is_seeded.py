"""Every duplicate-address guard string is seeded once, in en, uz and ru (spec 2026-11-18 §6.11).

Each value carries exactly the placeholders its caller fills, in its family's form (`{x}` for the backend, the bots
and the website, `{{x}}` for the admin panel), the Russian is Russian, and no value tells the customer to call
(Global Constraints: point to Support or the bot chat). Later tasks add their keys to KEYS.
"""

import re
from pathlib import Path

import pytest

from scripts import (
    seed_backend_translations,
    seed_place_group_ui_translations,
    seed_staff_translations,
    seed_web_ui_checkout_translations,
)

from tests.unit.test_place_group_translation_seeds import _JS_PAIR, _JS_STRING, _unquote_js

pytestmark = pytest.mark.unit

LANGUAGES = ("en", "uz", "ru")

# key -> the placeholder names its caller fills (EXPECTED_PLACEHOLDERS).
KEYS: dict[str, set[str]] = {
    # The guard's refusals (business_app/services/address_guard_service.py; move_needs_pin: the move route).
    "api.addresses.error.possible_duplicate": set(),
    "api.addresses.error.pin_with_driver": set(),
    "api.addresses.error.pin_shared_place": set(),
    "api.addresses.error.pin_outlet": set(),
    "api.addresses.error.move_too_far": set(),
    "api.addresses.error.move_needs_pin": set(),
    # The customer bot's duplicate prompt and its outcomes (telegram_bot/handlers/profile.py).
    "telegram.address.duplicate_prompt_header": set(),
    "telegram.address.duplicate_prompt_line": {"label", "distance"},
    "telegram.address.duplicate_prompt_footer": set(),
    "telegram.address.duplicate_use_button": {"label"},
    "telegram.address.duplicate_move_button": {"label"},
    "telegram.address.duplicate_new_button": set(),
    "telegram.address.duplicate_save_here_button": set(),
    "telegram.address.duplicate_kept": {"label"},
    "telegram.address.duplicate_moved": {"label"},
    # Its copy for each pin-move refusal (`PIN_REFUSAL_COPY`).
    "telegram.address.pin_with_driver": {"label"},
    "telegram.address.pin_shared_place": {"label"},
    "telegram.address.pin_outlet": {"label"},
    "telegram.address.move_too_far": set(),
    # Edit location: the durable prompt, its expiry, and the moved address (telegram_bot/handlers/profile.py).
    "telegram.address.location_edit_prompt": {"label"},
    "telegram.address.location_edit_send_pin": {"label"},
    "telegram.address.location_edit_expired": set(),
    "telegram.address.location_updated": {"label", "line"},
    # The website wizard's duplicate pane (business_app/static/js/address-wizard.js; {distance} is whole metres).
    "web.checkout.wizard.duplicate_title": set(),
    "web.checkout.wizard.duplicate_body": set(),
    "web.checkout.wizard.duplicate_distance": {"distance"},
    "web.checkout.wizard.duplicate_use": set(),
    "web.checkout.wizard.duplicate_move": set(),
    "web.checkout.wizard.duplicate_move_hint": set(),
    "web.checkout.wizard.duplicate_new": set(),
    "web.checkout.wizard.duplicate_save_anyway": set(),
    # The admin panel's possible-duplicate dialog (admin_ui/src/pages/Users.js).
    "ui.users.address_duplicate_title": set(),
    "ui.users.address_duplicate_body": set(),
    "ui.users.address_duplicate_create_anyway": set(),
    "ui.users.address_duplicate_save_anyway": set(),
    # The admin place-group suggestion rows (PlaceGroupPanel.jsx, GroupedAddressesPanel.jsx).
    "ui.users.place_groups.kind_same_customer": set(),
    "ui.users.place_groups.distance_apart": {"distance"},
    "ui.users.grouped_addresses.filter_all": set(),
    "ui.users.grouped_addresses.filter_shared_place": set(),
    "ui.users.grouped_addresses.filter_duplicates": set(),
    # The operator's possible-duplicate screen (staff_bot/handlers/operator/manage_address.py).
    "staff.operator.address_duplicate_prompt": set(),
    "staff.operator.address_duplicate_use": set(),
    "staff.operator.address_duplicate_create_anyway": set(),
    # The driver's alert for a pin move (`webhook_server.order_address_changed_handler`, when `pin_moved_m` is sent).
    "staff.notification.order_address_pin_moved": {"number", "distance", "new"},
}


def _prefixed(prefix: str, catalog: dict) -> dict:
    return {f"{prefix}{suffix}": values for suffix, values in catalog.items()}


def _by_key(translations: dict) -> dict:
    """{language: {key: value}} pivoted to {key: {language: value}}."""
    return {
        key: {language: rows[key] for language, rows in translations.items() if key in rows}
        for key in set().union(*translations.values())
    }


# Every catalog a key of this feature may be seeded in, as {key: {language: value}}. The staff seed's bare-suffix
# catalogs are listed one by one, so a key both there and in STAFF_TRANSLATIONS counts twice.
_STAFF = seed_staff_translations
CATALOGS = {
    "seed_backend_translations BACKEND_TRANSLATIONS": seed_backend_translations.BACKEND_TRANSLATIONS,
    "seed_web_ui_checkout_translations ROWS": _by_key(seed_web_ui_checkout_translations.TRANSLATIONS),
    "seed_place_group_ui_translations KEYS": seed_place_group_ui_translations.KEYS,
    "seed_staff_translations STAFF_TRANSLATIONS": _STAFF.STAFF_TRANSLATIONS,
    "seed_staff_translations EXTRA_TRANSLATIONS": _STAFF.EXTRA_TRANSLATIONS,
    "seed_staff_translations DELIVERY_TEXT": _prefixed("staff.delivery.", _STAFF.DELIVERY_TEXT_TRANSLATIONS),
    "seed_staff_translations OPERATOR_TEXT": _prefixed("staff.operator.", _STAFF.OPERATOR_TEXT_TRANSLATIONS),
    "seed_staff_translations SALES_TEXT": _prefixed("staff.sales.", _STAFF.SALES_TEXT_TRANSLATIONS),
}
# The seed sources: a key quoted twice in them is a second copy, which a later `BACKEND_TRANSLATIONS.update(...)`
# (or a repeated dict-literal key) would silently let win over the first.
SOURCES = "\n".join(
    Path(module.__file__).read_text(encoding="utf-8")
    for module in (
        seed_backend_translations,
        seed_web_ui_checkout_translations,
        seed_place_group_ui_translations,
        seed_staff_translations,
    )
)

_PLACEHOLDER = re.compile(r"\{\{\w+\}\}|\{\w+\}")
_CYRILLIC = re.compile(r"[\u0400-\u04FF]")
_CALL = re.compile(r"(?i)\bcall\b|позвон|qo.ng.iroq")


def _row(key: str) -> dict:
    rows = [catalog[key] for catalog in CATALOGS.values() if key in catalog]
    assert rows, f"{key} is not seeded"
    return rows[0]


def _expected_placeholders(key: str) -> set[str]:
    form = "{{%s}}" if key.startswith("ui.") else "{%s}"
    return {form % name for name in KEYS[key]}


@pytest.mark.parametrize("key", sorted(KEYS))
def test_each_key_is_seeded_once(key):
    owners = [name for name, catalog in CATALOGS.items() if key in catalog]
    assert len(owners) == 1, f"{key} is seeded in {owners}"
    assert SOURCES.count(f"'{key}'") + SOURCES.count(f'"{key}"') <= 1, f"{key} is written twice in the seeds"


@pytest.mark.parametrize("key", sorted(KEYS))
def test_each_key_is_seeded_in_three_languages_with_its_placeholders(key):
    row = _row(key)
    for language in LANGUAGES:
        value = row.get(language)
        assert isinstance(value, str) and value.strip(), f"{key} has no {language} value"
        assert set(_PLACEHOLDER.findall(value)) == _expected_placeholders(key), f"{key} ({language}): {value}"


@pytest.mark.parametrize("key", sorted(KEYS))
def test_the_russian_is_russian_and_no_copy_says_call(key):
    row = _row(key)
    assert _CYRILLIC.search(row["ru"]), f"{key}: {row['ru']}"
    for language in LANGUAGES:
        assert not _CALL.search(row[language]), f"{key} ({language}) tells the customer to call: {row[language]}"


# A label quoted in a sentence: «uz/ru» or “en”.
_QUOTED = re.compile(r"[«“]([^»”]+)[»”]")


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_expired_prompt_quotes_the_edit_location_button_as_seeded(language):
    """`location_edit_expired` tells the customer to tap Edit location again, so the one label it quotes is that
    button exactly as the bot labels it (spec §6.11, below the table), without the button's leading pin emoji, in
    every language the button is seeded in."""
    button = _row("telegram.address.edit_location_button")[language]
    assert button.startswith("📍 "), button
    assert _QUOTED.findall(_row("telegram.address.location_edit_expired")[language]) == [button.removeprefix("📍 ")]


_USERS_JS = Path(__file__).resolve().parents[2] / "admin_ui/src/pages/Users.js"


def _users_js_fallbacks() -> dict:
    text = _USERS_JS.read_text(encoding="utf-8")
    return {
        key: "".join(_unquote_js(literal) for literal in re.findall(_JS_STRING, expression, re.S))
        for _, key, expression in _JS_PAIR.findall(text)
    }


@pytest.mark.parametrize("key", sorted(key for key in KEYS if key.startswith("ui.users.address_duplicate_")))
def test_the_users_page_fallback_is_the_seeded_english(key):
    fallbacks = _users_js_fallbacks()
    assert key in fallbacks, f"Users.js does not render {key}"
    assert fallbacks[key] == _row(key)["en"]
