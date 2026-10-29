"""The reorder-reminder email (spec §7.4, §7.5, §15.9): copy, footer, headers, language.

Drives the REAL path `send_reminder` takes for an email recipient: `messaging._send_email` ->
`email_template_data` (the SAME seeded rows the bot renders, through
shared/reorder_reminder_copy.py) -> the (episode, step) email claim -> `NotificationService.send_notification`
-> `_send_email_notification` -> the file template -> Brevo. Only `requests.post` is faked, through
tests/notification_send_fakes.py::live_send_path (the `live_send` technique of
tests/unit/test_delivery_rescheduled_notification.py), and the claim store, by the builders'
`email_claims` FakeRedis. The adapter answers with a `ChannelResult` (master amendment 11) that is
never retryable: spec §12 sends an email once.
"""

import json
import re
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
import requests
from bs4 import BeautifulSoup
from itsdangerous import URLSafeSerializer

from business_app.models.notification import Notification
from business_app.services.email_template_service import get_email_template_service
from business_app.services.notification_service import _PLACEHOLDER_STRICT_TYPES, NotificationService
from business_app.services.reorder_reminders import messaging
from business_app.services.reorder_reminders.config import current_config
from business_app.services.reorder_reminders.eligibility import evaluate
from business_app.services.reorder_reminders.facts import load_customer_for_user
from business_app.services.reorder_reminders.messaging import ChannelResult
from business_app.services.reorder_reminders.tokens import make_unsubscribe_token, read_unsubscribe_token
from business_app.utils.constants import NotificationChannel, NotificationType
from shared.i18n_rendering import humanise_key
from shared.redis_keyspace import KEYSPACE_TIERS, REORDER_REMINDER_CLAIM_TTL_SECONDS, RedisKeyspace, RedisUsageTier
# `email_claims_fixture` is the `email_claims` FakeRedis behind `messaging.redis_client`.
from tests.integration.reorder_reminder_builders import (  # noqa: F401
    add_order_lines,
    delivered_order,
    email_claims_fixture,
    make_episode,
    name_product,
    reminder_copy_keys,
    seed_translations,
)
from tests.notification_send_fakes import BREVO_URL, live_send_path
# The copy guard's own "asks the customer to call" pattern, so the email is held to the same one.
from tests.unit.test_reorder_reminder_copy_is_seeded import _PHONE_PROMPT

pytestmark = pytest.mark.integration

TODAY = date(2026, 9, 13)
NOW = datetime(2026, 9, 13, 5, 0, tzinfo=timezone.utc)
LAST_DELIVERY = datetime(2026, 8, 9, 7, 0, tzinfo=timezone.utc)  # 35 local days before TODAY
BOT_URL = "https://t.me/aqua_reminder_test_bot"
# Set so that a footer which printed the shop's phone (the base layout's tel: link) would show it.
COMPANY_PHONE = "+998712000000"
_PLACEHOLDER = re.compile(r"\{[a-z_][a-z0-9_]*\}", re.IGNORECASE)
# The water's name in each language (master amendment 13). Which one the email prints shows which
# language it resolved; it must be the language of the sentence around it.
WATER_NAMES = {"uz": "Toza suv 19 l", "ru": "Чистая вода 19 л", "en": "Pure Water 19L"}
# Alice's email, word for word, in the languages the language tests resolve to:
# (subject, statement, CTA line). The statement holds the plural day phrase ("35 days", "35 дней").
ALICE_EMAIL = {
    "en": (
        "Hi Alice! Time for fresh water?",
        "Hi Alice! Your last water delivery was 35 days ago. Running low?",
        "Order on our website or in our Telegram bot and we'll bring your usual 2 × Pure Water 19L.",
    ),
    "ru": (
        "Здравствуйте, Alice! Пора заказать свежую воду?",
        "Здравствуйте, Alice! Последняя доставка воды была 35 дней назад. Вода заканчивается?",
        "Закажите на сайте или в нашем Telegram-боте — привезём как обычно: 2 × Чистая вода 19 л.",
    ),
    "uz": (
        "Assalomu alaykum, Alice! Suv buyurtma qilish vaqti keldimi?",
        "Assalomu alaykum, Alice! Oxirgi suv yetkazib berilganiga 35 kun boʻldi. Suvingiz tugayaptimi?",
        "Saytimizda yoki Telegram-botimizda buyurtma bering — odatdagidek 2 ta Toza suv 19 l olib kelamiz.",
    ),
}


@pytest.fixture
def seeded_copy(db):
    seed_translations(db, reminder_copy_keys())


@pytest.fixture
def live_send(app, monkeypatch, email_claims):
    """The production send path with only `requests.post` faked (and the claim store, so no test
    claims in the Redis DBs test runs share). Yields the fake."""
    with live_send_path(app, monkeypatch, bot_url=BOT_URL, extra_config={"COMPANY_PHONE": COMPANY_PHONE}) as post:
        yield post


@pytest.fixture
def email_customer(db, sample_user, sample_product):
    """Alice: one delivery 35 local days ago (preset 30-day threshold), no Telegram, a real email.
    Her water has a name in each language."""
    sample_user.first_name = "Alice"
    sample_user.preferred_language = "en"
    db.session.commit()
    name_product(db, sample_product, WATER_NAMES)
    order = add_order_lines(db, delivered_order(db, sample_user, LAST_DELIVERY), [(sample_product, 2)])
    episode = make_episode(db, sample_user, anchor=order, step=1, next_action_on=TODAY + timedelta(days=7))
    episode.usual_gap_days = None
    db.session.commit()
    return SimpleNamespace(user=sample_user, episode=episode)


def _facts(user_id):
    cfg = current_config()
    customer = load_customer_for_user(user_id, today=TODAY)
    return customer, evaluate(customer, today=TODAY, cfg=cfg), cfg


def _data(email_customer):
    customer, evaluation, cfg = _facts(email_customer.user.id)
    return messaging.email_template_data(email_customer.episode, customer, evaluation, 1, cfg, today=TODAY)


def _send(email_customer, step=1):
    customer, evaluation, cfg = _facts(email_customer.user.id)
    return messaging._send_email(email_customer.episode, customer, evaluation, step, cfg, now=NOW)


def test_the_type_is_registered_as_the_spec_says(db, sample_user):
    service = NotificationService()

    assert NotificationType.REORDER_REMINDER.value == "reorder_reminder"
    assert service._default_channels_for_type("reorder_reminder") == [NotificationChannel.EMAIL]
    assert service._get_user_preferred_channels(sample_user.id, NotificationType.REORDER_REMINDER) == [
        NotificationChannel.EMAIL
    ]
    assert "reorder_reminder" in _PLACEHOLDER_STRICT_TYPES
    # No group: a group toggle would be a second opt-out that decides nothing.
    assert all("reorder_reminder" not in group for group in NotificationService.NOTIFICATION_TYPE_GROUPS.values())


def test_every_subject_language_carries_only_the_greeting():
    subjects = get_email_template_service().EMAIL_SUBJECTS["reorder_reminder"]

    assert set(subjects) == {"uz", "ru", "en"}
    for text in subjects.values():
        assert _PLACEHOLDER.findall(text) == ["{greeting}"]
        assert not _PHONE_PROMPT.search(text), text


@pytest.mark.parametrize(
    "stored, resolved",
    [
        ("uz", "uz"),
        ("ru", "ru"),
        ("en", "en"),
        ("ru-RU", "ru"),
        ("uz_UZ", "uz"),
        ("EN", "en"),
        (None, "en"),
        ("", "en"),
        ("de", "en"),
    ],
)
def test_an_email_is_written_in_one_supported_language(stored, resolved):
    """The email service's own rule for every OTHER email (D15): a regional or upper-case code reads
    as its base language (`shared.i18n_rendering.base_language`); anything still unsupported reads as
    its DEFAULT_LANGUAGE. The reorder reminder does not use it: its language is THE reminder language
    (`reminder_language`, tests/unit/test_reminder_language.py), handed to the send explicitly."""
    assert get_email_template_service().resolve_language(stored) == resolved


def test_the_template_data_is_the_seeded_copy(app, db, seeded_copy, email_customer):
    data = _data(email_customer)
    url = data.pop("unsubscribe_url")
    shop_url = data.pop("shop_url")

    assert data == {
        "greeting": "Hi Alice!",
        "statement": "Hi Alice! Your last water delivery was 35 days ago. Running low?",
        "cta_line": "Order on our website or in our Telegram bot and we'll bring your usual 2 × Pure Water 19L.",
        "btn_website": "Order on the website",
        "btn_telegram": "Order in Telegram",
        "unsubscribe_line": "Don't want these reminders? Unsubscribe",
    }
    parts = urlsplit(url)
    assert shop_url == f"{app.config['COMPANY_WEBSITE'].rstrip('/')}/shop"
    assert url.startswith(f"{app.config['COMPANY_WEBSITE'].rstrip('/')}/reminders/unsubscribe/")
    assert parts.query == "lang=en"
    assert read_unsubscribe_token(parts.path.rsplit("/", 1)[1]) == email_customer.user.id


def test_a_website_configured_with_a_trailing_slash_never_doubles_it(app, db, seeded_copy, email_customer,
                                                                     live_send, monkeypatch):
    """Both links share ONE spelling of the website base (`customer_links.website_url`)."""
    monkeypatch.setitem(app.config, "COMPANY_WEBSITE", "https://aqua-element.example/")

    assert _send(email_customer) == ChannelResult(ok=True)

    sent = live_send.call_args.kwargs["json"]
    hrefs = {a["href"] for a in BeautifulSoup(sent["htmlContent"], "html.parser").find_all("a")}
    header_url = sent["headers"]["List-Unsubscribe"][1:-1]
    assert "https://aqua-element.example/shop" in hrefs
    assert header_url.startswith("https://aqua-element.example/reminders/unsubscribe/")
    assert header_url in hrefs
    assert not any("//" in href.split("://", 1)[1] for href in hrefs if "aqua-element.example" in href)


def test_the_token_carries_no_contact_details_and_refuses_tampering(app, sample_user):
    token = make_unsubscribe_token(sample_user.id)
    payload, signature = token.rsplit(".", 1)
    # The signature's FIRST character: every one of its bits is signature data (the last
    # character's low bits are base64 padding, so changing it may decode to the same bytes).
    altered = f"{payload}.{'A' if signature[0] != 'A' else 'B'}{signature[1:]}"
    # Another user's payload under this user's signature.
    spliced = f"{make_unsubscribe_token(sample_user.id + 1).rsplit('.', 1)[0]}.{signature}"
    # The same id signed for something else in the app (another salt) never unsubscribes anybody.
    foreign = URLSafeSerializer(app.config["SECRET_KEY"], salt="some-other-purpose").dumps(sample_user.id)

    assert sample_user.email not in token and sample_user.phone not in token
    assert read_unsubscribe_token(token) == sample_user.id
    assert read_unsubscribe_token(altered) is None
    assert read_unsubscribe_token(spliced) is None
    assert read_unsubscribe_token(foreign) is None
    assert read_unsubscribe_token("not-a-token") is None


@pytest.mark.parametrize("token", [None, 123, b"MQ.abc", ["MQ.abc"]], ids=["none", "int", "bytes", "list"])
def test_a_token_that_is_not_a_string_reads_as_no_user_and_never_raises(app, token):
    assert read_unsubscribe_token(token) is None


@pytest.mark.parametrize("first_name, subject", [("Alice", "Hi Alice! Time for fresh water?"),
                                                 (None, "Hello! Time for fresh water?")])
def test_the_subject_greets_with_and_without_a_name(db, seeded_copy, email_customer, first_name, subject):
    email_customer.user.first_name = first_name
    email_customer.user.last_name = None
    db.session.commit()

    rendered = get_email_template_service().render_notification_email("reorder_reminder", "en", _data(email_customer))

    assert rendered["subject"] == subject


def test_the_sent_email_has_no_phone_and_exactly_one_unsubscribe_link(app, db, seeded_copy, email_customer, live_send):
    assert _send(email_customer) == ChannelResult(ok=True)

    assert live_send.call_args.args == (BREVO_URL,)
    sent = live_send.call_args.kwargs["json"]
    header_url = sent["headers"]["List-Unsubscribe"][1:-1]
    assert sent["headers"] == {
        "List-Unsubscribe": f"<{header_url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }
    assert "/reminders/unsubscribe/" in header_url
    assert sent["to"] == [{"email": "test@example.com", "name": "Alice User"}]
    assert sent["subject"] == "Hi Alice! Time for fresh water?"
    html = sent["htmlContent"]
    assert "tel:" not in html
    assert COMPANY_PHONE not in html
    soup = BeautifulSoup(html, "html.parser")
    # ONE unsubscribe URL in the whole body, as a link, and it is the header's.
    assert html.count("/reminders/unsubscribe/") == 1
    assert [a["href"] for a in soup.select('a[href*="/reminders/unsubscribe/"]')] == [header_url]
    hrefs = {a["href"] for a in soup.find_all("a")}
    assert f"{app.config['COMPANY_WEBSITE'].rstrip('/')}/shop" in hrefs
    assert BOT_URL in hrefs
    for style in soup.find_all("style"):
        style.decompose()
    text = soup.get_text(" ", strip=True)
    assert not _PHONE_PROMPT.search(text), _PHONE_PROMPT.search(text)
    assert "Hi Alice! Your last water delivery was 35 days ago. Running low?" in text
    assert "Order on our website or in our Telegram bot and we'll bring your usual 2 × Pure Water 19L." in text
    assert Notification.query.filter_by(notification_type="reorder_reminder").count() == 1


def test_a_leftover_placeholder_skips_the_send(db, seeded_copy, email_customer, live_send, monkeypatch):
    real = messaging.email_template_data

    def _without_greeting(*args, **kwargs):
        data = real(*args, **kwargs)
        del data["greeting"]
        return data

    monkeypatch.setattr(messaging, "email_template_data", _without_greeting)

    assert _send(email_customer) == ChannelResult(ok=False, detail="email_unrendered_placeholders", retryable=False)
    live_send.assert_not_called()


@pytest.mark.parametrize("status", [400, 402, 429, 500, 503])
def test_a_brevo_refusal_or_outage_is_final_and_names_the_status(db, seeded_copy, email_customer, live_send, status):
    """Spec §12 "Email: one attempt": no Brevo status is retried. A 429 or a 5xx may come after
    Brevo accepted the message, so a retry could mail the customer twice; a 4xx is a refusal a
    retry repeats. The detail names the status, so the worklist can tell an outage from a refusal.

    Brevo's answer is a REAL `requests.Response`, falsy for a 4xx/5xx as production's is, so its
    own `raise_for_status` raises and the error body still reaches the failure record."""
    refused = requests.Response()
    refused.status_code = status
    refused._content = json.dumps({"code": "failure", "message": f"Brevo refused with {status}"}).encode()
    live_send.return_value = refused
    assert not refused  # what a 4xx/5xx Response is

    assert _send(email_customer) == ChannelResult(ok=False, detail=f"email_{status}", retryable=False)
    live_send.assert_called_once()
    (row,) = Notification.query.filter_by(notification_type="reorder_reminder").all()
    assert row.is_sent is False
    assert f"Brevo refused with {status}" in row.failure_reason  # Brevo's own words, not str(error)


def test_an_unreachable_brevo_is_final_too(db, seeded_copy, email_customer, live_send):
    live_send.side_effect = requests.exceptions.ConnectionError("brevo unreachable")

    assert _send(email_customer) == ChannelResult(ok=False, detail="email_error", retryable=False)


@pytest.mark.parametrize(
    "stored, language",
    [(None, "uz"), ("", "uz"), ("de", "uz"), ("tr", "uz"), ("ru-RU", "ru"), ("en", "en")],
)
def test_an_unreadable_stored_language_still_mails_one_real_language(
    db, monkeypatch, seeded_copy, email_customer, live_send, stored, language
):
    """Review Focus #3, final review B1/E1. The email is written in THE reminder language,
    `shared.i18n_rendering.reminder_language`, the one the bot renders the Telegram reminder in
    and the worklist names: a regional code reads as its base language ("ru-RU" -> ru), anything
    unsupported as the system DEFAULT_LANGUAGE (uz here). The subject, the body, the copy inside
    it, the plural day phrase, the order line's product name (master amendment 13) and the
    unsubscribe link's `lang=` all use that ONE value, with no raw key, no `{placeholder}` and no
    "None"."""
    monkeypatch.setenv("DEFAULT_LANGUAGE", "uz")
    email_customer.user.preferred_language = stored
    db.session.commit()
    subject, statement, cta_line = ALICE_EMAIL[language]

    assert _send(email_customer) == ChannelResult(ok=True)

    sent = live_send.call_args.kwargs["json"]
    assert sent["subject"] == subject
    html = sent["htmlContent"]
    assert f'<html lang="{language}">' in html
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    assert statement in text
    assert cta_line in text
    for code, name in WATER_NAMES.items():
        assert (name in text) is (code == language), (code, name)
    assert not _PLACEHOLDER.search(sent["subject"] + html)
    assert "None" not in text
    assert "telegram.reorder_reminder" not in html
    assert urlsplit(sent["headers"]["List-Unsubscribe"][1:-1]).query == f"lang={language}"


def test_a_customer_with_no_name_is_greeted_without_one(db, seeded_copy, email_customer, live_send):
    email_customer.user.first_name = None
    email_customer.user.last_name = None
    db.session.commit()

    assert _send(email_customer) == ChannelResult(ok=True)

    sent = live_send.call_args.kwargs["json"]
    assert sent["subject"] == "Hello! Time for fresh water?"
    # The To line names the address, never "None None".
    assert sent["to"] == [{"email": "test@example.com", "name": "test@example.com"}]
    assert "None" not in BeautifulSoup(sent["htmlContent"], "html.parser").get_text(" ", strip=True)


def test_a_name_with_braces_still_mails_with_the_braces_neutralised(
    db, seeded_copy, email_customer, sample_product, live_send
):
    """Ruling T22a: the reminder type is placeholder-strict, so a literal `{evil}` in the greeting
    (the subject carries it too) or in a product's name would make the guard skip the send, and the
    customer would never be reminded. Both reach the email with full-width braces instead."""
    email_customer.user.first_name = "{evil}"
    name_product(db, sample_product, {"uz": "{Aqua} 19 l", "ru": "{Aqua} 19 л", "en": "{Aqua} 19L"})
    db.session.commit()

    assert _send(email_customer) == ChannelResult(ok=True)

    live_send.assert_called_once()
    sent = live_send.call_args.kwargs["json"]
    assert sent["subject"] == "Hi ｛evil｝! Time for fresh water?"
    soup = BeautifulSoup(sent["htmlContent"], "html.parser")
    for style in soup.find_all("style"):
        style.decompose()
    text = soup.get_text(" ", strip=True)
    assert "Hi ｛evil｝! Your last water delivery was 35 days ago. Running low?" in text
    assert "your usual 2 × ｛Aqua｝ 19L." in text
    assert "{" not in sent["subject"] + text and "}" not in sent["subject"] + text


def test_a_russian_customer_reads_russian_day_phrases_and_product_names(db, seeded_copy, email_customer):
    email_customer.user.preferred_language = "ru"
    db.session.commit()

    data = _data(email_customer)

    _subject, statement, cta_line = ALICE_EMAIL["ru"]
    assert data["statement"] == statement  # "35 дней": the Russian plural, not the English one
    assert data["greeting"] == "Здравствуйте, Alice!"
    assert data["cta_line"] == cta_line  # "Чистая вода 19 л": the product's Russian name


def test_missing_copy_degrades_to_a_label_never_a_raw_key(db, email_customer):
    """No rows at all: the email degrades exactly as the bot does (spec §7.4 "Body")."""
    data = _data(email_customer)

    assert data["statement"] == humanise_key("telegram.reorder_reminder.r1_preset")
    assert not any(_PLACEHOLDER.search(value) for value in data.values())


def test_the_second_reminder_is_the_short_one_under_the_same_subject(db, seeded_copy, email_customer, live_send):
    """R2 (spec §7.5): the statement is the short reminder with no day count; the subject is the one
    every reminder email carries, the greeting and the question."""
    email_customer.episode.step = 2
    db.session.commit()

    assert _send(email_customer, step=2) == ChannelResult(ok=True)

    sent = live_send.call_args.kwargs["json"]
    assert sent["subject"] == "Hi Alice! Time for fresh water?"
    paragraphs = [p.get_text(" ", strip=True) for p in BeautifulSoup(sent["htmlContent"], "html.parser").select("p")]
    assert paragraphs[:2] == [
        "Hi Alice! Just a quick reminder.",
        "Order on our website or in our Telegram bot and we'll bring your usual 2 × Pure Water 19L.",
    ]
    assert "35 days" not in sent["htmlContent"]


def test_the_email_claim_is_a_registered_reliability_key():
    assert RedisKeyspace.reorder_reminders_email_claim(41, 2) == "bs:reorder_reminders:email_claim:41:2"
    assert KEYSPACE_TIERS["reorder_reminders_email_claim"] is RedisUsageTier.TIER_RELIABILITY


def test_the_email_claim_and_the_bot_claims_are_held_by_one_shared_ttl():
    """Deferred 338: the 31 days are ONE constant in shared/redis_keyspace.py, read by this module
    and by the customer bot's route (tests/telegram_bot/test_webhook_reorder_reminder.py pins that
    side), never two copies of the number."""
    assert REORDER_REMINDER_CLAIM_TTL_SECONDS == 31 * 24 * 60 * 60
    assert messaging.REORDER_REMINDER_CLAIM_TTL_SECONDS is REORDER_REMINDER_CLAIM_TTL_SECONDS
    assert not hasattr(messaging, "_EMAIL_CLAIM_TTL_SECONDS")


def test_a_second_send_of_the_same_step_finds_the_claim_and_mails_nothing(
    db, seeded_copy, email_customer, live_send, email_claims
):
    """A re-published copy (or one after a worker died past Brevo's 2xx) must not mail the customer
    again: the claim is taken before Brevo is called, held 31 days and never released."""
    key = RedisKeyspace.reorder_reminders_email_claim(email_customer.episode.id, 1)

    assert _send(email_customer) == ChannelResult(ok=True)
    assert _send(email_customer) == ChannelResult(ok=False, detail="already_claimed", retryable=False)

    live_send.assert_called_once()
    assert (email_claims.store, email_claims.expiries) == ({key: b"1"}, {key: 31 * 24 * 60 * 60})


def test_a_claim_on_one_step_never_blocks_another(db, seeded_copy, email_customer, live_send, email_claims):
    assert _send(email_customer, step=1) == ChannelResult(ok=True)
    email_customer.episode.step = 2
    db.session.commit()

    assert _send(email_customer, step=2) == ChannelResult(ok=True)

    assert live_send.call_count == 2
    assert set(email_claims.store) == {
        RedisKeyspace.reorder_reminders_email_claim(email_customer.episode.id, step) for step in (1, 2)
    }


def test_without_redis_the_email_still_goes_out(db, seeded_copy, email_customer, live_send, email_claims, monkeypatch):
    """Fail OPEN: an outage must not silence every email; the event log's once-per-step guard
    still holds (the customer bot's reminder route falls back the same way)."""

    def redis_down(*args, **kwargs):
        raise ConnectionError("redis unreachable")

    monkeypatch.setattr(email_claims, "set", redis_down)

    assert _send(email_customer) == ChannelResult(ok=True)
    live_send.assert_called_once()
