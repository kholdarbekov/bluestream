"""The storefront's Reorder reminders switch (spec 2026-10-09 §8): drawn on the profile page,
disabled until the published value arrives, and saved through the same preferences API the bot
uses (the X-Platform: web half of the route detection is pinned in
tests/integration/test_reorder_reminder_preferences_api.py).

The requests here are the ones profile-settings.js makes, as base.js `apiRequest` makes them: the
customer's own token, `X-Platform: web` (read from base.js, so a rename there breaks this file), a
GET for the published value, and a PUT whose whole body is `{"reorder_reminders_enabled": <the
switch>}`. The JS reads `data.preferences.reorder_reminders` and redraws from it.
"""

import json
import re
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from scripts.seed_backend_translations import BACKEND_TRANSLATIONS
from business_app.services.reorder_reminders.settings import ReorderReminderSettingsService as Settings
from tests.integration.reorder_reminder_builders import customer_headers, make_customer, seed_translations

# The ONE definition of "copy that asks a customer to call" (Task 14's guard).
from tests.unit.test_reorder_reminder_copy_is_seeded import _PHONE_PROMPT

pytestmark = pytest.mark.integration

PREFERENCES = "/api/v1/notifications/preferences"
_BASE_JS = Path(__file__).resolve().parents[2] / "business_app" / "static" / "js" / "pages" / "base.js"
# base.js `apiRequest` stamps every request with this header. Read from the source, so a change there
# changes the requests below; the round-trip test proves the server records it as web_settings.
WEB = {"X-Platform": re.search(r"headers\['X-Platform'\] = '([^']+)'", _BASE_JS.read_text(encoding="utf-8")).group(1)}
PUBLISHED_KEYS = {"enabled", "changed_by_kind", "changed_via", "changed_at"}

LABEL_KEYS = ("Reorder reminders", "Remind me when it is usually time to reorder water")
TOAST_KEYS = {
    "reorder_reminders_saved": "Reorder reminder setting saved",
    "reorder_reminders_failed": "Failed to save the reorder reminder setting",
}
ADMIN_OFF_REASON = "Asked not to be contacted"


@pytest.fixture
def ali(db):
    return make_customer(db, telegram=True)


@pytest.fixture
def web_copy(app, db):
    """The switch's rows, as the seeder ships them, with templates compiled afresh: Jinja folds
    `{{ 'key'|t }}` into a constant, and `app` is session-scoped (precedent:
    test_reorder_reminder_unsubscribe_pages.py). Every other `|t` text renders as its English key,
    because the test DB holds no other rows."""
    seed_translations(db, (*LABEL_KEYS, *TOAST_KEYS.values()))
    app.jinja_env.cache.clear()
    yield
    app.jinja_env.cache.clear()


def _page(client, app, user, language):
    """The page in `language`. Uzbek is the site default: `?lang=uz` is redirected (301) to the
    clean URL, so it is asked for the way a browser lands on it, without the parameter."""
    url = "/profile-settings" if language == "uz" else f"/profile-settings?lang={language}"
    response = client.get(url, headers=customer_headers(app, user))
    assert response.status_code == 200, response.get_data(as_text=True)[:400]
    soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    return soup, json.loads(soup.select_one("script#page-data").string)


def _get(client, app, user):
    return client.get(PREFERENCES, headers={**customer_headers(app, user), **WEB})


def _put(client, app, user, wanted):
    """What the change handler sends: nothing but the new value."""
    return client.put(
        PREFERENCES, headers={**customer_headers(app, user), **WEB}, json={"reorder_reminders_enabled": wanted}
    )


def _published(response):
    """What the JS draws from: `result.data.preferences.reorder_reminders`."""
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]["preferences"]["reorder_reminders"]


# ------------------------------------------------------------------------------------------ the page


@pytest.mark.parametrize("language", ["uz", "ru", "en"])
def test_the_profile_page_draws_the_switch_disabled_until_the_api_answers(
    client, app, db, ali, web_copy, language
):
    soup, page_data = _page(client, app, ali, language)

    switch = soup.select_one("#reorderRemindersSwitch")
    assert switch is not None
    assert switch.get("type") == "checkbox"
    assert switch.get("name") == "reorder_reminders_enabled"
    assert switch.has_attr("disabled"), "before the published value arrives the switch must not offer a guess"
    assert not switch.has_attr("checked")
    # Firefox restores a stale checked/disabled state on a soft reload unless told not to.
    assert switch.get("autocomplete") == "off"
    # The existing inert preferences form is untouched (spec §18), and no form can submit the switch.
    assert soup.select_one("#preferencesForm #reorderRemindersSwitch") is None
    assert switch.find_parent("form") is None
    assert [field.get("name") for field in soup.select("#preferencesForm input")] == [
        "email_notifications",
        "sms_notifications",
        "marketing_emails",
    ]
    # A disabled switch offers nothing to press: no pointer cursor, no hover highlight.
    styles = " ".join(style.get_text() for style in soup.select("style"))
    assert re.search(r"\.check-box input\.check:disabled\s*\+\s*label\s*\{[^}]*cursor:\s*default", styles)
    assert re.search(r"\.check-box:has\(input\.check:disabled\):hover\s*\{[^}]*background:\s*#f8f9fa", styles)
    # profile-settings.js is on the page and reads the data island the switch's toasts come from.
    assert soup.select_one('script[src$="js/pages/profile-settings.js"]') is not None
    assert set(TOAST_KEYS) <= set(page_data["i18n"])


@pytest.mark.parametrize("language", ["uz", "ru"])
def test_an_uzbek_or_russian_page_carries_no_english_for_the_switch(client, app, db, ali, web_copy, language):
    soup, page_data = _page(client, app, ali, language)

    switch = soup.select_one("#reorderRemindersSwitch")
    card = switch.find_parent(class_="card")
    assert card.select_one(".card-header").get_text(" ", strip=True) == (
        BACKEND_TRANSLATIONS["Reorder reminders"][language]
    )
    assert soup.select_one('label[for="reorderRemindersSwitch"]').get_text(strip=True) == (
        BACKEND_TRANSLATIONS["Remind me when it is usually time to reorder water"][language]
    )
    for field, english in TOAST_KEYS.items():
        assert page_data["i18n"][field] == BACKEND_TRANSLATIONS[english][language], field
        assert page_data["i18n"][field] != english
    # Nothing of the switch's English source survives on the page.
    shown = card.get_text(" ", strip=True) + " " + json.dumps(page_data["i18n"], ensure_ascii=False)
    for english in (*LABEL_KEYS, *TOAST_KEYS.values()):
        assert english not in shown, english


def test_the_english_page_reads_the_english_source(client, app, db, ali, web_copy):
    soup, page_data = _page(client, app, ali, "en")

    assert soup.select_one('label[for="reorderRemindersSwitch"]').get_text(strip=True) == LABEL_KEYS[1]
    assert {field: page_data["i18n"][field] for field in TOAST_KEYS} == TOAST_KEYS


def test_the_switch_copy_is_trilingual_and_never_asks_the_customer_to_call():
    from scripts.seed_backend_translations import REORDER_REMINDER_WEB_TRANSLATIONS

    assert set(REORDER_REMINDER_WEB_TRANSLATIONS) == {*LABEL_KEYS, *TOAST_KEYS.values()}
    for key, texts in REORDER_REMINDER_WEB_TRANSLATIONS.items():
        assert set(texts) == {"en", "uz", "ru"}, key
        assert BACKEND_TRANSLATIONS[key] is texts, f"{key} is not in the seed the app reads"
        for language, text in texts.items():
            assert text.strip() and not _PHONE_PROMPT.search(text), (key, language)
    for key in REORDER_REMINDER_WEB_TRANSLATIONS:
        assert re.search(r"[А-Яа-яЁё]", REORDER_REMINDER_WEB_TRANSLATIONS[key]["ru"]), key


# --------------------------------------------------------------------------- the round trip


def test_the_get_publishes_the_default_for_the_switch_to_draw(client, app, db, ali):
    published = _published(_get(client, app, ali))

    assert published == {"enabled": True, "changed_by_kind": None, "changed_via": None, "changed_at": None}


@pytest.mark.parametrize("wanted", [False, True])
def test_the_web_switch_round_trips_through_the_preferences_api(client, app, db, ali, wanted):
    """Both options a customer can pick. The write is recorded as the customer's own, from the web
    settings, though the page never sends either fact."""
    if wanted:  # Turning ON needs something to turn on from.
        assert _published(_put(client, app, ali, False))["enabled"] is False

    answer = _put(client, app, ali, wanted)

    assert WEB == {"X-Platform": "web"}, "base.js no longer sends the header this request stands for"
    redrawn = _published(answer)
    assert set(redrawn) == PUBLISHED_KEYS, "the JS reads this object; a staff-only `reason` must not be in it"
    assert redrawn["enabled"] is wanted
    assert (redrawn["changed_by_kind"], redrawn["changed_via"]) == ("customer", "web_settings")
    assert redrawn["changed_at"] is not None
    # The next page load draws the same thing the save redrew.
    assert _published(_get(client, app, ali)) == redrawn


def test_a_customers_off_over_an_admins_off_is_published_as_the_admins_off(client, app, db, ali, admin_user):
    """D20: the page was loaded while reminders were on; an admin switched them off; the customer
    then taps the switch off. The server changes nothing, and the PUT's own answer is what the
    switch must redraw from: still off, still the admin's, with no reason."""
    Settings.set(
        ali.id, False, actor_kind="admin", via="admin_ui", reason=ADMIN_OFF_REASON, actor_user_id=admin_user.id
    )

    answer = _put(client, app, ali, False)

    redrawn = _published(answer)
    assert set(redrawn) == PUBLISHED_KEYS
    assert (redrawn["enabled"], redrawn["changed_by_kind"], redrawn["changed_via"]) == (False, "admin", "admin_ui")
    assert ADMIN_OFF_REASON not in answer.get_data(as_text=True)


def test_a_refused_save_leaves_the_published_value_the_switch_reverts_to(client, app, db, ali):
    before = _published(_put(client, app, ali, False))

    refused = client.put(
        PREFERENCES, headers={**customer_headers(app, ali), **WEB}, json={"reorder_reminders_enabled": "yes"}
    )

    assert refused.status_code == 400
    body = refused.get_json()
    assert body["success"] is False and body["message"]  # English: the page never shows it
    assert _published(_get(client, app, ali)) == before
