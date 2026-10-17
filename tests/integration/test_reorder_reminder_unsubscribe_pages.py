"""The email's unsubscribe link (spec §7.4 "Unsubscribe", §8, D20).

GET only asks: link scanners and mail previews fetch it, so it must never change anything. POST
(the page's button, or a mailbox's cookie-less RFC 8058 one-click) writes customer-off with
via=email_link. The Done page offers Undo, and Undo is NOT the email's token: the email token never
expires and must never be able to switch anybody ON, so Done mints a separate 24-hour undo token
and /unsubscribe/undo/<undo token> honours only that. A token whose user no longer exists, or a
forged one, gets a neutral expired page and writes nothing. Pages are requested in Russian
(`?lang=ru`, as the email builds the link) with the page rows seeded as the seeder ships them.

Undo reverts only what the unsubscribe changed: the Done page offers it only when the POST turned
reminders from on to off (nothing to undo over an admin-off or an earlier customer-off), and the
undo route acts only while the effective setting is still that email-link customer-off.

CSRF: the three views carry `@csrf_protection.csrf.exempt`: the signed token is their only
authority. The global check is off in production (`WTF_CSRF_CHECK_DEFAULT = False`) while the
testing config also disables the extension. `csrf_on` turns the extension on with the production
default, so the POST tests run the way production does, and fail if the premise changes;
`csrf_strict` turns the whole check on, which only the exemption survives.

Privacy: both tokens sit in the URL, so the page passes `suppress_analytics` and base.html drops the
Google Analytics and Yandex Metrica snippets (they would send the path to Google and Yandex).
"""

import re
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import itsdangerous.timed as itsdangerous_timed
import pytest
from bs4 import BeautifulSoup
from flask_jwt_extended import create_access_token
from flask_limiter.util import get_qualified_name
from itsdangerous import URLSafeSerializer, URLSafeTimedSerializer
from limits import parse as parse_rate_limit
from werkzeug.exceptions import MethodNotAllowed

from business_app import limiter
from business_app.frontend.routes import REMINDERS_UNSUBSCRIBE_RATE_LIMIT
from business_app.models.reorder_reminder import ReorderReminderEvent, ReorderReminderSetting
from business_app.services.reorder_reminders.actions import ReorderReminderActions
from business_app.services.reorder_reminders.settings import ReorderReminderSettingsService
from business_app.services.reorder_reminders.tokens import (
    UNDO_MAX_AGE_SECONDS,
    UNDO_SALT,
    make_undo_token,
    make_unsubscribe_token,
    read_undo_token,
    read_unsubscribe_token,
    unsubscribe_url,
)
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS
from tests.integration.reorder_reminder_builders import event_rows, seed_translations

# The ONE definition of "copy that asks a customer to call" (Task 14's guard).
from tests.unit.test_reorder_reminder_copy_is_seeded import _PHONE_PROMPT

pytestmark = pytest.mark.integration

LANGUAGES = ("en", "uz", "ru")
PAGE_KEYS = sorted(k for k in BACKEND_TRANSLATIONS if k.startswith("reorder_unsubscribe."))
HOUR = 3600


@pytest.fixture
def page_copy(app, db):
    """The page rows, with templates compiled afresh: Jinja folds `{{ 'key'|t }}` into a constant,
    and `app` is session-scoped (precedent: test_web_pages_show_awaiting_new_date.py). Returns
    `copy(key, language="ru")`, the seeded text."""
    seed_translations(db, PAGE_KEYS)
    app.jinja_env.cache.clear()

    def copy(key, language="ru"):
        return BACKEND_TRANSLATIONS[key][language]

    yield copy
    app.jinja_env.cache.clear()


@pytest.fixture
def csrf_on(app, monkeypatch):
    """Production's CSRF settings: the extension on, its global check off (config/base.py)."""
    monkeypatch.setitem(app.config, "WTF_CSRF_ENABLED", True)
    assert app.config["WTF_CSRF_CHECK_DEFAULT"] is False


@pytest.fixture
def csrf_strict(app, monkeypatch):
    """The CSRF extension fully on, as if `WTF_CSRF_CHECK_DEFAULT` were flipped: it then checks every
    POST view that is not exempt."""
    monkeypatch.setitem(app.config, "WTF_CSRF_ENABLED", True)
    monkeypatch.setitem(app.config, "WTF_CSRF_CHECK_DEFAULT", True)


@pytest.fixture
def token_clock(monkeypatch):
    """`token_clock(hours)`: move the clock the TIMED undo token reads, by `hours` from now. It is
    `time` as bound in `itsdangerous.timed`, the one place a timestamp signature asks the time, so
    nothing else in the process moves."""
    real_time = itsdangerous_timed.time.time

    def travel(hours):
        monkeypatch.setattr(
            itsdangerous_timed, "time", SimpleNamespace(time=lambda: real_time() + hours * HOUR)
        )

    return travel


def _page(response):
    """`(soup, text, state)` of a rendered page, which must be a 200."""
    assert response.status_code == 200, response.get_data(as_text=True)[:400]
    soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    return soup, soup.get_text(" ", strip=True), soup.select_one("[data-unsubscribe-state]")["data-unsubscribe-state"]


def _page_forms(soup):
    """The forms of the page's own card, not the site header's search form."""
    return soup.select("[data-unsubscribe-state] form")


def _undo_form(soup):
    return soup.select_one('[data-action="undo"]').find_parent("form")


def _submit(client, form, **kwargs):
    """What a browser does with a form: POST its action with its fields."""
    fields = {field["name"]: field.get("value", "") for field in form.select("input[name]")}
    return client.post(form["action"], data=fields, **kwargs)


def _unsubscribe(client, user):
    """The customer presses the button; returns the Done page's Undo form."""
    token = make_unsubscribe_token(user.id)
    soup, _text, state = _page(client.post(f"/reminders/unsubscribe/{token}?lang=ru"))
    assert state == "done"
    return _undo_form(soup)


def _setting(db, user):
    row = db.session.get(ReorderReminderSetting, user.id)
    return None if row is None else (row.enabled, row.changed_by_kind, row.changed_via)


def _row(db, user):
    """The whole setting row, to prove a click left it exactly as it was."""
    row = db.session.get(ReorderReminderSetting, user.id)
    return (row.enabled, row.changed_by_kind, row.changed_via, row.changed_at, row.reason, row.changed_by_user_id)


def _admin_off(user, admin):
    ReorderReminderSettingsService.set(
        user.id, False, actor_kind="admin", via="admin_ui", reason="Asked not to be contacted", actor_user_id=admin.id
    )


def _customer_sets(user, enabled, via):
    ReorderReminderSettingsService.set(user.id, enabled, actor_kind="customer", via=via, actor_user_id=user.id)


# ---------------------------------------------------------------------- GET only asks


def test_opening_the_link_only_asks(client, db, sample_user, page_copy):
    token = make_unsubscribe_token(sample_user.id)

    soup, _text, state = _page(client.get(f"/reminders/unsubscribe/{token}?lang=ru"))

    assert state == "confirm"
    button = soup.select_one('[data-action="unsubscribe"]')
    assert button.get_text(strip=True) == page_copy("reorder_unsubscribe.confirm_button")
    form = button.find_parent("form")
    assert form["method"].lower() == "post"
    assert urlsplit(form["action"]).path == f"/reminders/unsubscribe/{token}"
    assert parse_qs(urlsplit(form["action"]).query) == {"lang": ["ru"]}
    assert soup.select_one('meta[name="robots"]')["content"].startswith("noindex")
    assert soup.select_one('[data-action="undo"]') is None
    assert db.session.get(ReorderReminderSetting, sample_user.id) is None
    assert ReorderReminderEvent.query.count() == 0


# ---------------------------------------------------------------------- POST: the button and the mailbox


def test_the_button_unsubscribes_and_offers_undo(client, db, sample_user, page_copy):
    token = make_unsubscribe_token(sample_user.id)

    soup, text, state = _page(client.post(f"/reminders/unsubscribe/{token}?lang=ru"))

    assert state == "done"
    assert page_copy("reorder_unsubscribe.done_text") in text
    undo = _undo_form(soup)
    assert undo["method"].lower() == "post"
    parts = urlsplit(undo["action"])
    assert parse_qs(parts.query) == {"lang": ["ru"]}
    undo_token = parts.path.rsplit("/", 1)[1]
    assert parts.path == f"/reminders/unsubscribe/undo/{undo_token}"
    assert read_undo_token(undo_token) == sample_user.id
    # The Undo form does not carry the email's token anywhere.
    assert token not in str(undo)
    assert _setting(db, sample_user) == (False, "customer", "email_link")
    assert ReorderReminderEvent.query.filter_by(user_id=sample_user.id, kind="stop").count() == 1


def test_a_mailbox_one_click_post_needs_no_session_and_no_csrf_token(app, db, sample_user, csrf_on):
    token = make_unsubscribe_token(sample_user.id)
    mailbox = app.test_client(use_cookies=False)

    response = mailbox.post(
        f"/reminders/unsubscribe/{token}?lang=en",
        data="List-Unsubscribe=One-Click",
        content_type="application/x-www-form-urlencoded",
    )

    assert response.status_code == 200
    assert _setting(db, sample_user) == (False, "customer", "email_link")


def test_the_page_button_is_not_refused_for_want_of_a_csrf_token(client, db, sample_user, page_copy, csrf_on):
    """The customer opens the link and presses the button, exactly as the form is rendered."""
    token = make_unsubscribe_token(sample_user.id)
    soup, _text, _state = _page(client.get(f"/reminders/unsubscribe/{token}?lang=ru"))

    response = _submit(client, soup.select_one('[data-action="unsubscribe"]').find_parent("form"))

    assert _page(response)[2] == "done"
    assert _setting(db, sample_user) == (False, "customer", "email_link")


def test_a_logged_in_browser_can_press_the_button_without_a_jwt_csrf_header(
    app, client, db, sample_user, page_copy, csrf_on
):
    """The customer is signed in on the web (JWT cookie) when they click the email link. The link's
    signed token is the only authority, so the session's own CSRF header is not asked for."""
    client.set_cookie("access_token_cookie", create_access_token(identity=str(sample_user.id)))
    token = make_unsubscribe_token(sample_user.id)
    soup, _text, _state = _page(client.get(f"/reminders/unsubscribe/{token}?lang=ru"))

    response = _submit(client, soup.select_one('[data-action="unsubscribe"]').find_parent("form"))

    assert _page(response)[2] == "done"
    assert _setting(db, sample_user) == (False, "customer", "email_link")


def test_with_the_csrf_check_fully_on_the_signed_token_is_still_enough(app, db, sample_user, csrf_strict):
    """`WTF_CSRF_ENABLED` and `WTF_CSRF_CHECK_DEFAULT` both True: the extension refuses every POST
    view without a CSRF token unless it is exempt. The mailbox's cookie-less one-click POST and the
    Undo POST still succeed, because the three routes are exempt by name."""
    mailbox = app.test_client(use_cookies=False)
    control = mailbox.post("/api/v1/auth/login", json={})
    assert control.status_code == 400
    assert control.get_json()["error"] == "CSRF token validation failed"

    token = make_unsubscribe_token(sample_user.id)
    one_click = mailbox.post(
        f"/reminders/unsubscribe/{token}?lang=en",
        data="List-Unsubscribe=One-Click",
        content_type="application/x-www-form-urlencoded",
    )
    soup, _text, state = _page(one_click)
    assert state == "done"
    assert _setting(db, sample_user) == (False, "customer", "email_link")

    undone = mailbox.post(_undo_form(soup)["action"])

    assert _page(undone)[2] == "undone"
    assert _setting(db, sample_user) == (True, "customer", "email_link")


def test_an_unsubscribe_over_an_admin_off_changes_nothing_and_offers_no_undo(client, db, sample_user, admin_user):
    """D20: it never turns an admin-off into a customer-off. And the click made no off of its own, so
    the Done page has nothing to undo: Undo would switch on what an admin switched off."""
    _admin_off(sample_user, admin_user)
    before = _row(db, sample_user)
    token = make_unsubscribe_token(sample_user.id)

    soup, _text, state = _page(client.post(f"/reminders/unsubscribe/{token}?lang=en"))

    assert state == "done"
    assert soup.select_one('[data-action="undo"]') is None
    assert _page_forms(soup) == []
    assert _setting(db, sample_user) == (False, "admin", "admin_ui")
    assert _row(db, sample_user) == before
    assert ("setting_changed", "already_off", None, None) in event_rows(user=sample_user)


@pytest.mark.parametrize("earlier", ["telegram_button", "telegram_settings", "web_settings", "email_link"])
def test_an_unsubscribe_when_already_customer_off_changes_nothing_and_offers_no_undo(
    client, db, sample_user, earlier
):
    ReorderReminderActions.stop(sample_user.id, via=earlier)
    before = _row(db, sample_user)
    token = make_unsubscribe_token(sample_user.id)

    soup, _text, state = _page(client.post(f"/reminders/unsubscribe/{token}?lang=en"))

    assert state == "done"
    assert soup.select_one('[data-action="undo"]') is None
    assert _row(db, sample_user) == before
    assert before[:3] == (False, "customer", earlier)
    assert ("setting_changed", "already_off", None, None) in event_rows(user=sample_user)


def test_a_second_click_on_the_same_link_offers_no_undo(client, db, sample_user):
    """A mail scanner or a double-tap POSTs the link again: the first click's Undo is the only one."""
    _unsubscribe(client, sample_user)
    before = _row(db, sample_user)
    token = make_unsubscribe_token(sample_user.id)

    soup, _text, state = _page(client.post(f"/reminders/unsubscribe/{token}?lang=ru"))

    assert state == "done"
    assert soup.select_one('[data-action="undo"]') is None
    assert _row(db, sample_user) == before


# ---------------------------------------------------------------------- Undo


def test_undo_turns_reminders_back_on(client, db, sample_user, page_copy):
    undo = _unsubscribe(client, sample_user)

    soup, text, state = _page(_submit(client, undo))

    assert state == "undone"
    assert page_copy("reorder_unsubscribe.undone_text") in text
    assert _setting(db, sample_user) == (True, "customer", "email_link")
    assert [detail for kind, detail, _s, _c in event_rows(user=sample_user) if kind == "setting_changed"] == [
        "off",
        "on",
    ]


def test_undo_does_not_switch_on_what_an_admin_switched_off_later(client, db, sample_user, admin_user, page_copy):
    """A valid, unexpired undo token minted BEFORE an admin-off must not override that admin-off: Undo
    reverts the email-link off it was minted for, and only while that off is still in effect."""
    undo = _unsubscribe(client, sample_user)
    _admin_off(sample_user, admin_user)
    before, events_before = _row(db, sample_user), event_rows(user=sample_user)

    soup, text, state = _page(_submit(client, undo))

    assert state == "expired"
    assert page_copy("reorder_unsubscribe.expired_text") in text
    assert _row(db, sample_user) == before
    assert _setting(db, sample_user) == (False, "admin", "admin_ui")
    assert event_rows(user=sample_user) == events_before


def test_an_undo_token_for_an_admin_off_customer_writes_nothing(client, db, sample_user, admin_user, page_copy):
    """A genuine undo token (only `make_undo_token` mints one) for a customer who is admin-off."""
    _admin_off(sample_user, admin_user)
    before, events_before = _row(db, sample_user), event_rows(user=sample_user)

    soup, _text, state = _page(client.post(f"/reminders/unsubscribe/undo/{make_undo_token(sample_user.id)}?lang=ru"))

    assert state == "expired"
    assert _row(db, sample_user) == before
    assert event_rows(user=sample_user) == events_before


@pytest.mark.parametrize("later", ["back_on_in_the_bot", "back_on_then_off_in_the_bot"])
def test_undo_is_dead_once_the_customer_changed_the_setting_themselves(client, db, sample_user, page_copy, later):
    undo = _unsubscribe(client, sample_user)
    _customer_sets(sample_user, True, "telegram_settings")
    if later == "back_on_then_off_in_the_bot":
        _customer_sets(sample_user, False, "telegram_button")
    before, events_before = _row(db, sample_user), event_rows(user=sample_user)

    soup, _text, state = _page(_submit(client, undo))

    assert state == "expired"
    assert _row(db, sample_user) == before
    assert event_rows(user=sample_user) == events_before


def test_undo_twice_writes_once(client, db, sample_user, page_copy):
    undo = _unsubscribe(client, sample_user)
    assert _page(_submit(client, undo))[2] == "undone"
    events_after_first = event_rows(user=sample_user)

    assert _page(_submit(client, undo))[2] == "expired"
    assert _setting(db, sample_user) == (True, "customer", "email_link")
    assert event_rows(user=sample_user) == events_after_first


def test_undo_works_for_a_whole_day(client, db, sample_user, page_copy, token_clock):
    undo = _unsubscribe(client, sample_user)
    token_clock(UNDO_MAX_AGE_SECONDS / HOUR - 0.5)

    assert _page(_submit(client, undo))[2] == "undone"
    assert _setting(db, sample_user) == (True, "customer", "email_link")


def test_after_a_day_undo_says_expired_and_writes_nothing(client, db, sample_user, page_copy, token_clock):
    undo = _unsubscribe(client, sample_user)
    events_before = event_rows(user=sample_user)
    token_clock(UNDO_MAX_AGE_SECONDS / HOUR + 0.5)

    soup, text, state = _page(_submit(client, undo))

    assert state == "expired"
    assert page_copy("reorder_unsubscribe.expired_text") in text
    assert _setting(db, sample_user) == (False, "customer", "email_link")
    assert event_rows(user=sample_user) == events_before


def test_the_emails_own_token_posted_to_undo_writes_nothing(client, db, sample_user, page_copy):
    """The email token never expires and is in every mailbox: it must not be able to switch anybody ON."""
    _unsubscribe(client, sample_user)
    email_token = make_unsubscribe_token(sample_user.id)
    events_before = event_rows(user=sample_user)

    soup, text, state = _page(client.post(f"/reminders/unsubscribe/undo/{email_token}?lang=ru"))

    assert state == "expired"
    assert page_copy("reorder_unsubscribe.expired_text") in text
    assert _setting(db, sample_user) == (False, "customer", "email_link")
    assert event_rows(user=sample_user) == events_before


def test_undo_tokens_are_not_unsubscribe_tokens_either(app, sample_user):
    assert read_unsubscribe_token(make_undo_token(sample_user.id)) is None
    assert read_undo_token(make_unsubscribe_token(sample_user.id)) is None


def _forged_undo_tokens(app, user_id):
    """Every way to hand the undo route something that is not a fresh undo token of this user."""
    key = app.config["SECRET_KEY"]
    mine = make_undo_token(user_id)
    theirs = make_undo_token(user_id + 1)
    return {
        "garbage": "not-a-token",
        "altered": mine[:-2] + ("yy" if mine.endswith("xx") else "xx"),
        "email salt": URLSafeSerializer(key, salt="reorder-reminder-unsubscribe").dumps(user_id),
        "untimed, right salt": URLSafeSerializer(key, salt=UNDO_SALT).dumps(user_id),
        "timed, other salt": URLSafeTimedSerializer(key, salt="some-other-purpose").dumps(user_id),
        "other key": URLSafeTimedSerializer("another-secret", salt=UNDO_SALT).dumps(user_id),
        # Another user's payload and timestamp under this user's signature.
        "other user's payload": f"{theirs.rsplit('.', 1)[0]}.{mine.rsplit('.', 1)[1]}",
    }


@pytest.mark.parametrize(
    "how",
    [
        "garbage",
        "altered",
        "email salt",
        "untimed, right salt",
        "timed, other salt",
        "other key",
        "other user's payload",
    ],
)
def test_a_forged_undo_token_writes_nothing(app, client, db, sample_user, page_copy, how):
    _unsubscribe(client, sample_user)
    events_before = event_rows(user=sample_user)
    forged = _forged_undo_tokens(app, sample_user.id)[how]

    soup, text, state = _page(client.post(f"/reminders/unsubscribe/undo/{forged}?lang=ru"))

    assert state == "expired"
    assert page_copy("reorder_unsubscribe.expired_text") in text
    assert _setting(db, sample_user) == (False, "customer", "email_link")
    assert event_rows(user=sample_user) == events_before


def test_an_undo_for_an_account_merged_away_in_the_meantime_says_expired(client, db, sample_user, page_copy):
    soup, text, state = _page(client.post(f"/reminders/unsubscribe/undo/{make_undo_token(987654)}?lang=ru"))

    assert state == "expired"
    assert ReorderReminderSetting.query.count() == 0
    assert ReorderReminderEvent.query.count() == 0


def test_undo_has_no_get(client, sample_user):
    undo_token = make_undo_token(sample_user.id)

    assert client.get(f"/reminders/unsubscribe/undo/{undo_token}?lang=ru").status_code == 405


# ---------------------------------------------------------------------- a dead email link


@pytest.mark.parametrize("how", ["merged_away", "tampered"])
def test_a_dead_link_says_so_and_writes_nothing(client, db, sample_user, page_copy, how):
    if how == "merged_away":
        token = make_unsubscribe_token(987654)
    else:
        token = make_unsubscribe_token(sample_user.id)[:-2] + "xx"

    for response in (
        client.get(f"/reminders/unsubscribe/{token}?lang=ru"),
        client.post(f"/reminders/unsubscribe/{token}?lang=ru"),
    ):
        soup, text, state = _page(response)
        assert state == "expired"
        assert page_copy("reorder_unsubscribe.expired_text") in text
        assert _page_forms(soup) == []
    assert ReorderReminderSetting.query.count() == 0
    assert ReorderReminderEvent.query.count() == 0


# ---------------------------------------------------------------------- the undo token itself


def test_the_undo_token_carries_only_the_user_id_and_reads_back(app, sample_user):
    token = make_undo_token(sample_user.id)

    assert sample_user.email not in token and sample_user.phone not in token
    assert read_undo_token(token) == sample_user.id


def test_the_undo_token_is_timed_with_its_own_salt_and_a_day_to_live(app, sample_user):
    signed = URLSafeTimedSerializer(app.config["SECRET_KEY"], salt=UNDO_SALT)

    assert UNDO_SALT == "reorder-reminder-resubscribe"
    assert UNDO_MAX_AGE_SECONDS == 24 * HOUR
    assert signed.loads(make_undo_token(sample_user.id), max_age=UNDO_MAX_AGE_SECONDS) == sample_user.id


def test_the_undo_token_expires_after_a_day(app, sample_user, token_clock):
    token = make_undo_token(sample_user.id)

    token_clock(23.9)
    assert read_undo_token(token) == sample_user.id
    token_clock(24.1)
    assert read_undo_token(token) is None


@pytest.mark.parametrize("token", [None, 123, b"MQ.abc", ["MQ.abc"], "", "not-a-token"], ids=repr)
def test_garbage_reads_as_no_user_and_never_raises(app, token):
    assert read_undo_token(token) is None


# ---------------------------------------------------------------------- language


# The keys each state of the page renders: the F1 guard below checks every one of them, per state.
STATE_KEYS = {
    "confirm": ("title", "confirm_text", "confirm_button", "home_link"),
    "done": ("title", "done_text", "undo_button", "home_link"),
    "undone": ("title", "undone_text", "home_link"),
    "expired": ("title", "expired_text", "home_link"),
}


def _every_state(client, user, language):
    """`{state: (soup, text, state)}` for the four states of the page, requested in `language`. It ends
    with the customer's reminders on again, so the next round can run the same four requests."""
    live = make_unsubscribe_token(user.id)
    dead = make_unsubscribe_token(987654)
    pages = {
        "confirm": _page(client.get(f"/reminders/unsubscribe/{live}?lang={language}")),
        "expired": _page(client.get(f"/reminders/unsubscribe/{dead}?lang={language}")),
    }
    done = _page(client.post(f"/reminders/unsubscribe/{live}?lang={language}"))
    pages["done"] = done
    pages["undone"] = _page(_submit(client, _undo_form(done[0])))
    assert {state: page[2] for state, page in pages.items()} == {state: state for state in STATE_KEYS}
    return pages


def test_each_visitor_reads_every_state_of_the_page_in_their_own_language(client, sample_user, page_copy):
    """F1. The first request compiles the template; a later request in another language must not read
    the first one's text. The Jinja cache is NOT cleared between the rounds (ru, en, ru), and every
    key of every state is checked, not one: a single `|t` without the language would fold into the
    compiled template in the first visitor's language."""
    for language, other in (("ru", "en"), ("en", "ru"), ("ru", "en")):
        pages = _every_state(client, sample_user, language)

        for state, names in STATE_KEYS.items():
            text = pages[state][1]
            for name in names:
                key = f"reorder_unsubscribe.{name}"
                assert page_copy(key, language) in text, (language, state, key)
                assert page_copy(key, other) not in text, (language, state, key)


def test_every_t_filter_of_the_page_takes_the_page_language():
    """F1, structurally: Jinja folds `{{ 'key'|t }}` into the cached compiled template, so no `|t` may
    be written without `(page_language)`. Counts every literal key against its filter."""
    template = Path(__file__).resolve().parents[2] / "business_app/templates/frontend/reminders_unsubscribe.html"
    source = re.sub(r"\{#.*?#\}", "", template.read_text(encoding="utf-8"), flags=re.DOTALL)

    keys = re.findall(r"'reorder_unsubscribe\.[a-z_]+'", source)
    translated = re.findall(r"'reorder_unsubscribe\.[a-z_]+'\|t\(page_language\)", source)

    assert len(keys) == len(translated) > 0
    assert re.findall(r"\|\s*t\b(?!\(page_language\))", source) == []


@pytest.mark.parametrize("lang", ["ru-RU", "ru_RU", "RU", "Ru-ru"])
def test_a_regional_or_upper_case_lang_reads_as_its_base_language(client, sample_user, page_copy, lang):
    token = make_unsubscribe_token(sample_user.id)

    soup, text, _state = _page(client.get(f"/reminders/unsubscribe/{token}?lang={lang}"))

    assert page_copy("reorder_unsubscribe.confirm_text", "ru") in text
    assert parse_qs(urlsplit(_page_forms(soup)[0]["action"]).query) == {"lang": ["ru"]}


def test_an_uzbek_link_opens_in_uzbek_whatever_the_browser_prefers(client, sample_user, page_copy):
    """`uz` is the site default, which every other page strips from the URL (a 301) before
    choosing the language from the visitor's browser. This page's language is the email's."""
    token = make_unsubscribe_token(sample_user.id)

    response = client.get(f"/reminders/unsubscribe/{token}?lang=uz", headers={"Accept-Language": "en"})

    _soup, text, _state = _page(response)
    assert page_copy("reorder_unsubscribe.confirm_text", "uz") in text


def test_the_done_and_undone_pages_keep_the_language_of_the_link(client, db, sample_user, page_copy):
    token = make_unsubscribe_token(sample_user.id)

    soup, text, _state = _page(client.post(f"/reminders/unsubscribe/{token}?lang=en"))
    assert page_copy("reorder_unsubscribe.done_text", "en") in text
    assert parse_qs(urlsplit(_undo_form(soup)["action"]).query) == {"lang": ["en"]}

    _soup, text, state = _page(_submit(client, _undo_form(soup)))
    assert state == "undone"
    assert page_copy("reorder_unsubscribe.undone_text", "en") in text


# ---------------------------------------------------------------------- the link in the email, the limits


def test_the_link_in_the_email_is_this_page(app, sample_user):
    parts = urlsplit(unsubscribe_url(sample_user.id, "ru"))
    adapter = app.url_map.bind("localhost")

    assert adapter.match(parts.path, method="GET")[0] == "frontend.reminders_unsubscribe"
    assert adapter.match(parts.path, method="POST")[0] == "frontend.reminders_unsubscribe_post"
    assert parse_qs(parts.query) == {"lang": ["ru"]}


def test_the_undo_route_is_post_only_and_its_own_endpoint(app, sample_user):
    adapter = app.url_map.bind("localhost")
    path = f"/reminders/unsubscribe/undo/{make_undo_token(sample_user.id)}"

    assert adapter.match(path, method="POST")[0] == "frontend.reminders_unsubscribe_undo"
    with pytest.raises(MethodNotAllowed):
        adapter.match(path, method="GET")


@pytest.mark.parametrize(
    "endpoint",
    ["reminders_unsubscribe", "reminders_unsubscribe_post", "reminders_unsubscribe_undo"],
)
def test_every_unsubscribe_route_is_rate_limited_per_ip_and_generously(app, endpoint):
    """Spec §14. Each route carries exactly the named constant's limit, per IP (the app limiter's
    key). Mailboxes POST one-click requests from shared IPs, so the constant is a ceiling for abuse,
    not a trickle."""
    view = app.view_functions[f"frontend.{endpoint}"]
    limits = limiter.limit_manager.decorated_limits(get_qualified_name(view))

    assert [limit.limit for limit in limits] == [parse_rate_limit(REMINDERS_UNSUBSCRIBE_RATE_LIMIT)]
    assert parse_rate_limit(REMINDERS_UNSUBSCRIBE_RATE_LIMIT).GRANULARITY.name == "hour"


def test_opening_an_admin_off_customers_link_writes_nothing(client, db, sample_user, admin_user):
    """GET only asks, whatever the setting is: the confirm page for an admin-off customer."""
    _admin_off(sample_user, admin_user)
    before, events_before = _row(db, sample_user), event_rows(user=sample_user)
    token = make_unsubscribe_token(sample_user.id)

    _soup, _text, state = _page(client.get(f"/reminders/unsubscribe/{token}?lang=en"))

    assert state == "confirm"
    assert _row(db, sample_user) == before
    assert event_rows(user=sample_user) == events_before


def test_one_whole_uzbek_visit_stays_in_uzbek(client, db, sample_user, page_copy):
    """`uz` is the site default, the language the other pages strip from the URL. The link opens in
    Uzbek in an English browser, and the button, the Done page and Undo all stay in Uzbek."""
    token = make_unsubscribe_token(sample_user.id)
    english_browser = {"Accept-Language": "en"}

    confirm, text, state = _page(client.get(f"/reminders/unsubscribe/{token}?lang=uz", headers=english_browser))
    assert state == "confirm"
    assert page_copy("reorder_unsubscribe.confirm_text", "uz") in text
    button = confirm.select_one('[data-action="unsubscribe"]')
    assert button.get_text(strip=True) == page_copy("reorder_unsubscribe.confirm_button", "uz")

    done, text, state = _page(_submit(client, button.find_parent("form"), headers=english_browser))
    assert state == "done"
    assert page_copy("reorder_unsubscribe.done_text", "uz") in text
    undo = _undo_form(done)
    assert undo.select_one('[data-action="undo"]').get_text(strip=True) == page_copy(
        "reorder_unsubscribe.undo_button", "uz"
    )
    assert parse_qs(urlsplit(undo["action"]).query) == {"lang": ["uz"]}
    assert _setting(db, sample_user) == (False, "customer", "email_link")

    _undone, text, state = _page(_submit(client, undo, headers=english_browser))
    assert state == "undone"
    assert page_copy("reorder_unsubscribe.undone_text", "uz") in text
    assert _setting(db, sample_user) == (True, "customer", "email_link")


# ---------------------------------------------------------------------- the tokens never reach third parties
# The control (an ordinary page with the same IDs renders both snippets) lives with the other
# secret-in-URL pages in test_secret_url_pages_skip_analytics.py.


def test_no_state_of_the_page_loads_analytics_or_is_indexable(client, sample_user, page_copy, analytics_configured):
    """Both tokens sit in the URL, which GA4's `page_path` and Metrica's Webvisor would send to
    Google and Yandex. The page also says noindex (the one robots meta, `noindex,nofollow`)."""
    for state, (soup, _text, _state) in _every_state(client, sample_user, "ru").items():
        html = str(soup)
        for marker in analytics_configured:
            assert marker not in html, (state, marker)
        robots = soup.select('meta[name="robots"]')
        assert len(robots) == 1 and "noindex" in robots[0]["content"], state


# ---------------------------------------------------------------------- page copy


@pytest.mark.parametrize("key", PAGE_KEYS)
def test_the_page_copy_is_trilingual_and_never_asks_the_customer_to_call(key):
    for language in LANGUAGES:
        value = BACKEND_TRANSLATIONS[key][language]
        assert value
        assert not _PHONE_PROMPT.search(value), (key, language)


@pytest.mark.parametrize(
    "language, website_settings",
    [
        ("en", "your profile settings on our website"),
        ("uz", "saytdagi profil sozlamalarida"),
        ("ru", "в настройках профиля на нашем сайте"),
    ],
)
def test_the_expired_page_points_to_the_website_profile_settings_first(client, sample_user, page_copy, language,
                                                                     website_settings):
    """Final review E3: the email goes only to customers without usable Telegram (spec §7.4), so the
    expired page names the switch they DO have, the website profile settings (Task 19c), and the bot
    only as an extra. Rendered through the page a dead link really opens."""
    copy = page_copy("reorder_unsubscribe.expired_text", language)
    assert website_settings in copy
    assert copy.index(website_settings) < copy.lower().index("telegram")

    _soup, text, state = _page(client.get(f"/reminders/unsubscribe/{make_unsubscribe_token(987654)}?lang={language}"))

    assert state == "expired"
    assert copy in text


def test_the_page_has_exactly_the_keys_the_template_renders():
    template = Path(__file__).resolve().parents[2] / "business_app/templates/frontend/reminders_unsubscribe.html"

    used = set(re.findall(r"'(reorder_unsubscribe\.[a-z_]+)'", template.read_text(encoding="utf-8")))

    assert used == set(PAGE_KEYS)
