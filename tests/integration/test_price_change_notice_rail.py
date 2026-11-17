"""The announcement and the reminder on the customer-notice rail (price-change plan Task 41).

`customer_notices.send_price_change` is what the `price_change.send_notice` task calls: Telegram through the bot's
`/internal/customer-notice`, a blocked bot falls back to one email in the customer's language, no channel sends
nothing. The bot's HTTP hop and Brevo's POST are the only fakes.
"""

from decimal import Decimal

import pytest

from business_app.services import customer_notices
from business_app.utils.constants import NotificationType
from tests.integration.customer_notice_builders import (  # noqa: F401  (fixtures by their *_fixture name)
    BOT_BLOCKED,
    BOT_URL,
    CHAT_ID,
    CONTRACT_KEYS,
    ROUTE,
    bot_fixture,
    on_email_fixture,
    on_telegram_fixture,
)
from tests.integration.price_change_builders import CHANGE_ON, bottles, make_admin, make_price_change
from tests.integration.reorder_reminder_builders import name_product
from tests.integration.public_page_builders import seed_copy
from tests.notification_send_fakes import BREVO_URL, live_send_path

pytestmark = pytest.mark.integration

ANNOUNCEMENT = customer_notices.PRICE_CHANGE_ANNOUNCEMENT
REMINDER = customer_notices.PRICE_CHANGE_REMINDER
NOTICE_ID = "price_change:1:7:announcement"


@pytest.fixture(name="change")
def change_fixture(db):
    """The world's change: the 18.9 L to 20,000 and the 10 L to 12,000, each product named in three languages."""
    world = bottles(db)
    name_product(db, world.p19, {"en": "Pure Water 19L", "uz": "Toza suv 19L", "ru": "Чистая вода 19Л"})
    name_product(db, world.p10, {"en": "Pure Water 10L", "uz": "Toza suv 10L", "ru": "Чистая вода 10Л"})
    price_change = make_price_change(
        db,
        [(world.p19, Decimal("20000"), 2, None), (world.p10, Decimal("12000"), 3, None)],
        created_by=make_admin(db),
    )
    db.session.commit()
    return price_change


def test_the_telegram_payload_is_exact_and_in_the_resolved_language(change, on_telegram, bot, db):
    on_telegram.preferred_language = "ru-RU"
    db.session.commit()

    outcome = customer_notices.send_price_change(change, on_telegram, ANNOUNCEMENT, is_subscriber=False, notice_id=NOTICE_ID)

    assert outcome == "telegram"
    assert bot.calls == [
        {
            "endpoint": ROUTE,
            "request_id": NOTICE_ID,
            "payload": {
                "notice_id": NOTICE_ID,
                "kind": "price_change_announcement",
                "telegram_id": CHAT_ID,
                "language": "ru",
                "price_change_id": change.id,
                "effective_date": CHANGE_ON.isoformat(),
                "prices": [
                    {"name": "Чистая вода 19Л", "new_price": 20000.0},
                    {"name": "Чистая вода 10Л", "new_price": 12000.0},
                ],
                "is_subscriber": False,
            },
        }
    ]
    assert set(bot.calls[0]["payload"]) == CONTRACT_KEYS["price_change_announcement"]


def test_the_reminder_carries_its_kind_and_the_subscriber_answer(change, on_telegram, bot):
    outcome = customer_notices.send_price_change(change, on_telegram, REMINDER, is_subscriber=True, notice_id="r-1")

    assert outcome == "telegram"
    [call] = bot.calls
    assert call["payload"]["kind"] == "price_change_reminder"
    assert call["payload"]["is_subscriber"] is True
    assert call["payload"]["prices"][0] == {"name": "Pure Water 19L", "new_price": 20000.0}
    assert set(call["payload"]) == CONTRACT_KEYS["price_change_reminder"]
    assert customer_notices.PRICE_CHANGE_KINDS == frozenset({ANNOUNCEMENT, REMINDER})
    assert customer_notices.PRICE_CHANGE_KINDS <= customer_notices.NOTICE_KINDS


_LANGUAGES = [
    ("en", "so'm", "rices change on", "For your subscription, orders created from"),
    ("uz", "so'm", "dan narxlarimiz o'zgaradi", "Obunangiz bo'yicha"),
    ("ru", "сум", "меняются наши цены", "Заказы по вашей подписке, созданные с"),
]


@pytest.mark.parametrize("kind", [ANNOUNCEMENT, REMINDER])
@pytest.mark.parametrize("language,unit,subject_words,subscriber_words", _LANGUAGES)
def test_a_blocked_bot_falls_back_to_one_email_in_the_customers_language(
    app, monkeypatch, change, on_telegram, bot, db, language, unit, subject_words, subscriber_words, kind
):
    on_telegram.preferred_language = language
    db.session.commit()
    bot.answer = BOT_BLOCKED

    with live_send_path(app, monkeypatch, bot_url=BOT_URL) as post:
        outcome = customer_notices.send_price_change(change, on_telegram, kind, is_subscriber=True, notice_id="b-1")

    assert outcome == "email"
    assert len(bot.calls) == 1
    [call] = post.call_args_list
    assert call.args[0] == BREVO_URL
    body = call.kwargs["json"]
    assert subject_words in body["subject"] and "Aqua Element" in body["subject"]
    html = body["htmlContent"]
    assert f"20,000 {unit}" in html and f"12,000 {unit}" in html
    assert "UZS" not in html
    assert subscriber_words in html


# The change day (CHANGE_ON, 1 February 2027) as the email prints it: words with the year, never 02/01/2027, which
# an English reader takes for 2 January.
_DATE_WORDS = {"en": "1 February 2027", "uz": "1-fevral 2027", "ru": "1 февраля 2027"}


@pytest.mark.parametrize("kind", [ANNOUNCEMENT, REMINDER])
@pytest.mark.parametrize("language", ["en", "uz", "ru"])
def test_the_email_states_the_change_date_in_words_in_every_language(
    app, db, monkeypatch, change, on_telegram, bot, language, kind
):
    seed_copy(db, ("web.display_labels",))
    on_telegram.preferred_language = language
    db.session.commit()
    bot.answer = BOT_BLOCKED

    with live_send_path(app, monkeypatch, bot_url=BOT_URL) as post:
        outcome = customer_notices.send_price_change(change, on_telegram, kind, is_subscriber=False, notice_id="d-1")

    assert outcome == "email"
    body = post.call_args_list[0].kwargs["json"]
    assert _DATE_WORDS[language] in body["subject"]
    assert _DATE_WORDS[language] in body["htmlContent"]
    assert "02/01/2027" not in body["subject"] + body["htmlContent"]


def test_a_non_subscriber_email_has_no_subscription_sentence(app, monkeypatch, change, on_email):
    with live_send_path(app, monkeypatch, bot_url=BOT_URL) as post:
        outcome = customer_notices.send_price_change(change, on_email, ANNOUNCEMENT, is_subscriber=False, notice_id="e-1")

    assert outcome == "email"
    html = post.call_args_list[0].kwargs["json"]["htmlContent"]
    assert "20,000 so'm" in html
    assert "For your subscription" not in html


def test_no_channel_sends_nothing(change, sample_user, db, bot):
    sample_user.telegram_id = None
    sample_user.is_bot_active = False
    sample_user.email = f"telegram_{sample_user.id}@bluestream.local"
    db.session.commit()

    outcome = customer_notices.send_price_change(change, sample_user, ANNOUNCEMENT, is_subscriber=False, notice_id="n-1")

    assert outcome == "no_channel"
    assert bot.calls == []


def test_an_unknown_kind_is_refused(change, on_telegram, bot):
    with pytest.raises(ValueError):
        customer_notices.send_price_change(change, on_telegram, "prepayment_returned", is_subscriber=False, notice_id="x")
    assert bot.calls == []
    assert NotificationType.PRICE_CHANGE_REMINDER.value == "price_change_reminder"
