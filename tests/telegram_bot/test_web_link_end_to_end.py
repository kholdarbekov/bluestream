"""A web-only customer connects Telegram from /my-account, end to end (spec 2026-10-19 R3–R6, D11).

Every hop is the real one: the storefront's POST /api/v1/auth/telegram-link (access cookie plus
`X-CSRF-TOKEN`, the page language as `?lang=`), then the real customer bot through the PTB
dispatcher, whose link calls (and, after the link, the phone-link calls) are bridged to the REAL
signed routes on the Flask app. The other suites pin each side against the other's published
shape; this one proves the shapes meet: the real link payload reaches the bot's parser, the real
preview's label reaches the confirm card, and the real envelope of a refusal (its `error_code` at
the top level) reaches the bot's copy.

Only Telegram and the bot's own SQL stay faked (tests/telegram_bot/ptb_harness.py), and the SMS
task where a phone-link code is sent.
"""

from datetime import UTC, datetime

import pytest

from business_app.models.loyalty import LoyaltyTransaction
from business_app.models.user import User

from tests.integration.test_web_password_reset_and_change import WEB_HEADERS, _logged_in_browser
from tests.telegram_bot.ptb_harness import DEFAULT_USER_ID, backend_failure
from tests.telegram_bot.test_registration_journey_dispatcher import (
    CHECK_PHONE,
    LINK_SEND_OTP,
    LINK_VERIFY,
    MY_PHONE_AS_TELEGRAM_SENDS_IT,
    MY_PHONE_E164,
    texts,
)
from tests.telegram_bot.test_web_link_journey_dispatcher import (  # noqa: F401  (link_bot and make_bot are fixtures)
    CONFIRM,
    PREVIEW,
    card_id,
    copy,
    created_no_account,
    link_bot,
    make_bot,
    no_language_card,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


ISSUE = "/api/v1/auth/telegram-link"
OTHER_TELEGRAM = 700100777


def _bridge(bot, signed_bot_post, endpoint, seen):
    """Answer the bot's call to `endpoint` with the real signed route, and keep each body in `seen`.

    A refusal goes back the way `_make_request` surfaces a 4xx/5xx: the body's `message` as the
    error and the whole body as the data, so the bot reads the real envelope's `error_code`.
    """

    def _respond(call):
        assert call.sign is True, f"{endpoint} is reachable only with the bot's signature"
        resp = signed_bot_post(endpoint, call.data)
        body = resp.get_json()
        seen.append(body)
        if resp.status_code == 200:
            return body
        return backend_failure(body["message"], resp.status_code, data=body)

    bot.backend.route("POST", endpoint, _respond)


@pytest.fixture
def verified_number(db, sample_user):
    """The web account's number is verified, so Yes ends at the greeting, not at D12's SMS step."""
    sample_user.phone_verified_at = datetime.now(UTC)
    db.session.commit()


def _confirm_card(bot):
    [card] = [shown for shown in bot.telegram.shown if shown.callback_data() == ["weblink_yes", "weblink_no"]]
    return card


async def _connect(app, auth_token, signed_bot_post, link_bot):
    """The whole journey: the /my-account tap, then `/start web_<token>` and Yes in the bot.

    Returns the bot, the deep link's start payload and every body the real bot routes answered.
    """
    browser, csrf = _logged_in_browser(app, auth_token)
    issued = browser.post(f"{ISSUE}?lang=ru", headers={**WEB_HEADERS, "X-CSRF-TOKEN": csrf})
    assert issued.status_code == 200
    payload = issued.get_json()["data"]["telegram_link"].split("?start=", 1)[1]

    bot, seen = await link_bot(), []
    for endpoint in (PREVIEW, CONFIRM):
        _bridge(bot, signed_bot_post, endpoint, seen)
    user = bot.updates()
    await bot.send(user.command(f"/start {payload}"))
    assert created_no_account(bot), "no telegram-register or telegram-login before Yes (C6)"
    await bot.send(user.tap("weblink_yes", card_id(bot)))
    return bot, payload, seen


# `db` before `link_bot`: anyio's runner task copies the context when the first async fixture is set
# up, so `db`'s app context (the one that scopes `db.session`) must already be pushed by then (C14).
async def test_a_web_only_customer_connects_telegram_with_one_tap(
    app, db, sample_user, verified_number, auth_token, signed_bot_post, link_bot
):
    before = (sample_user.registration_source, sample_user.email_verified_at, sample_user.status)

    bot, _payload, seen = await _connect(app, auth_token, signed_bot_post, link_bot)

    assert _confirm_card(bot).text == copy("telegram.web_link.confirm_prompt", account="T*** U*** · +998***4567")
    db.session.expire_all()
    row = db.session.get(User, sample_user.id)
    assert (row.telegram_id, row.is_bot_active, row.preferred_language) == (str(DEFAULT_USER_ID), True, "ru")
    assert (row.registration_source, row.email_verified_at, row.status) == before
    assert LoyaltyTransaction.query.filter_by(user_id=row.id).count() == 0
    tokens = seen[-1]["data"]["tokens"]
    assert bot.tokens.stored == [{"user_id": DEFAULT_USER_ID, "access_token": tokens["access_token"],
                                  "refresh_token": tokens["refresh_token"], "expires_in": tokens["expires_in"]}]
    shown = bot.telegram.last_shown()
    assert shown.text == copy("telegram.web_link.connected", name="Test") and "menu_support" in shown.callback_data()
    assert created_no_account(bot)


# `db` before `link_bot`, as above (C14).
async def test_the_same_link_from_another_telegram_is_dead_through_the_real_envelope(
    app, db, sample_user, verified_number, auth_token, signed_bot_post, link_bot
):
    bot, payload, seen = await _connect(app, auth_token, signed_bot_post, link_bot)
    bot.telegram.reset()

    other = bot.updates(user_id=OTHER_TELEGRAM, chat_id=OTHER_TELEGRAM)
    await bot.send(other.command(f"/start {payload}"))

    _preview, _confirm, refusal = seen
    assert (refusal["status_code"], refusal["error_code"]) == (400, "WEB_LINK_EXPIRED")
    # In the other Telegram's own locale: a dead link carries no page language, and it has no row.
    [shown] = bot.telegram.shown
    assert (shown.text, shown.params["chat_id"]) == (copy("telegram.web_link.expired", "uz"), OTHER_TELEGRAM)
    assert no_language_card(bot) and created_no_account(bot)
    db.session.expire_all()
    assert db.session.get(User, sample_user.id).telegram_id == str(DEFAULT_USER_ID)


# `db` before `link_bot`, as above (C14).
async def test_a_number_held_by_another_account_merges_into_it_through_the_real_routes(
    app, db, monkeypatch, sample_user, second_sample_user, auth_token, signed_bot_post, link_bot
):  # D12 'missing', R8, R8a, R8b
    sample_user.phone = None  # registered by email only, so Yes asks for the contact
    # ACTIVE, no Telegram, source 'web': the account holding the number the customer then shares.
    second_sample_user.phone = MY_PHONE_E164
    db.session.commit()
    web_id, holder_id = sample_user.id, second_sample_user.id
    codes = []

    def _sms_task(user_id, otp_code, phone_number=None):  # the SMS gateway: the one external hop replaced
        codes.append(otp_code)

    monkeypatch.setattr("business_app.services.auth_service.send_verification_sms_task", _sms_task)

    bot, _payload, seen = await _connect(app, auth_token, signed_bot_post, link_bot)
    assert seen[-1]["data"]["phone_status"] == "missing"
    for endpoint in (CHECK_PHONE, LINK_SEND_OTP, LINK_VERIFY):
        _bridge(bot, signed_bot_post, endpoint, seen)
    user = bot.updates()
    await bot.send(user.contact(MY_PHONE_AS_TELEGRAM_SENDS_IT))
    assert seen[-1]["data"]["can_link"] is True
    assert bot.telegram.last_shown().callback_data() == ["link_yes", "link_no"]
    await bot.send(user.tap("link_yes"))
    [code] = codes
    await bot.send(user.text(code))

    merged = seen[-1]["data"]
    assert merged["user"]["id"] == holder_id
    assert texts(bot)[-1] == "Welcome back, Co! Accounts linked"
    assert copy("telegram.web_link.connected", name="Test") not in texts(bot)
    assert bot.tokens.stored[-1]["access_token"] == merged["tokens"]["access_token"]
    db.session.expire_all()
    assert db.session.get(User, web_id) is None
    holder = db.session.get(User, holder_id)
    assert (holder.telegram_id, holder.phone, holder.email) == (str(DEFAULT_USER_ID), MY_PHONE_E164, "test@example.com")
    assert created_no_account(bot)
