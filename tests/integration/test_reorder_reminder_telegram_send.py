"""The Telegram half of a reorder reminder, backend side (spec §7.3, §7.2 c, §12).

`telegram_payload` is the body the customer bot renders from. Its keys are pinned by the
master contract here and by tests/telegram_bot/test_webhook_reorder_reminder.py from the other
side. `_send_telegram` is the adapter `send_reminder` (Task 13) calls once the send-time
re-check has passed. It answers with a `ChannelResult` (master amendment 11).

Driven over real rows: the recipient comes from the real fact loader and evaluation, the order
lines from the recipient's real newest delivered order, the block from the real
TelegramReachabilityService. Only `trigger_bot_webhook` (the HTTP hop to the bot) is replaced,
by the `bot_webhook` recorder (tests/integration/reorder_reminder_builders.py), which keeps its
exact signature and records what would have been POSTed.
"""

import dataclasses
import logging
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from business_app.models.reorder_reminder import ReorderReminderEvent
from business_app.models.user import User
from business_app.services.reorder_reminders import messaging
from business_app.services.reorder_reminders.config import current_config
from business_app.services.reorder_reminders.eligibility import evaluate
from business_app.services.reorder_reminders.facts import load_customer_for_user
from business_app.services.reorder_reminders.messaging import ChannelResult
from business_app.utils.timezone_utils import ensure_utc
from shared import reorder_reminder_copy
# `bot_webhook_fixture` is the `bot_webhook` recorder of the HTTP hop to the customer bot.
from tests.integration.reorder_reminder_builders import (  # noqa: F401
    BOT_BLOCKED,
    add_order_lines,
    bot_webhook_fixture,
    captured_logs,
    delivered_order,
    event_rows,
    link,
    make_episode,
    make_product,
    name_product,
)

pytestmark = pytest.mark.integration

TODAY = date(2026, 9, 13)
NOW = datetime(2026, 9, 13, 5, 0, tzinfo=timezone.utc)  # 10:00 in Tashkent
LAST_DELIVERY = datetime(2026, 9, 1, 7, 0, tzinfo=timezone.utc)  # local noon, 12 local days before TODAY
CHAT_ID = "810000001"
# tests/telegram_bot/test_webhook_reorder_reminder.py::CONTRACT_KEYS pins the bot's side.
CONTRACT_KEYS = {
    "telegram_id", "episode_id", "step", "silent", "show_later", "name",
    "days", "gap", "snooze_days", "reorder_order_id", "items", "language",
}
WATER_NAMES = {"uz": "Toza suv 19 l", "ru": "Чистая вода 19 л", "en": "Pure Water 19L"}


def _names(name):
    """A product with no per-language rows reads its column in every language."""
    return {"uz": name, "ru": name, "en": name}


@pytest.fixture
def telegram_customer(db, sample_user, sample_category, sample_product):
    """Alice orders every 7 days; her last delivery was 12 local days ago, so R1 is due."""
    sample_user.first_name = "Alice"
    sample_user.telegram_id = CHAT_ID
    sample_user.is_bot_active = True
    sample_user.preferred_language = "en"
    db.session.commit()
    cups = make_product(db, sample_category, "Cooler cups")
    pump = make_product(db, sample_category, "Hand pump")
    for weeks_back in (2, 1):
        earlier = delivered_order(db, sample_user, LAST_DELIVERY - timedelta(weeks=weeks_back))
        add_order_lines(db, earlier, [(sample_product, 1)])
    last = add_order_lines(
        db,
        delivered_order(db, sample_user, LAST_DELIVERY),
        [(sample_product, 3), (pump, 1), (cups, 2)],
        reward_lines=[(cups, 5)],
    )
    episode = make_episode(db, sample_user, anchor=last, step=1, next_action_on=TODAY + timedelta(days=7))
    return SimpleNamespace(user=sample_user, last=last, episode=episode, water=sample_product, cups=cups, pump=pump)


def _facts(user_id):
    cfg = current_config()
    customer = load_customer_for_user(user_id, today=TODAY)
    return customer, evaluate(customer, today=TODAY, cfg=cfg), cfg


def _r1_payload(c):
    return {
        "telegram_id": CHAT_ID,
        "episode_id": c.episode.id,
        "step": 1,
        "silent": False,
        "show_later": True,
        "name": "Alice",
        "days": 12,
        "gap": 7,
        "snooze_days": 14,
        "reorder_order_id": c.last.id,
        # The two largest real lines; the free reward line (5 cups) is never "your usual".
        "items": [{"qty": 3, "names": _names("Pure Water 19L")}, {"qty": 2, "names": _names("Cooler cups")}],
        # `shared.i18n_rendering.reminder_language` of her stored "en": the bot renders in it.
        "language": "en",
    }


def test_r1_carries_exactly_the_contract_fields(db, telegram_customer):
    customer, evaluation, cfg = _facts(telegram_customer.user.id)

    payload = messaging.telegram_payload(telegram_customer.episode, customer, evaluation, 1, cfg, today=TODAY)

    assert set(payload) == CONTRACT_KEYS
    assert payload == _r1_payload(telegram_customer)


def test_r2_is_silent_and_offers_no_later(db, telegram_customer):
    customer, evaluation, cfg = _facts(telegram_customer.user.id)

    payload = messaging.telegram_payload(
        telegram_customer.episode, customer, evaluation, 2, cfg, today=TODAY + timedelta(days=7)
    )

    assert (payload["step"], payload["silent"], payload["show_later"], payload["days"]) == (2, True, False, 19)


def test_a_one_step_ladder_offers_no_later_even_on_r1(db, telegram_customer):
    customer, evaluation, cfg = _facts(telegram_customer.user.id)

    payload = messaging.telegram_payload(
        telegram_customer.episode, customer, evaluation, 1, dataclasses.replace(cfg, max_per_episode=1), today=TODAY
    )

    assert (payload["silent"], payload["show_later"]) == (False, False)


def test_the_preset_sends_no_gap(db, telegram_customer):
    telegram_customer.episode.usual_gap_days = None
    db.session.commit()
    customer, evaluation, cfg = _facts(telegram_customer.user.id)

    payload = messaging.telegram_payload(telegram_customer.episode, customer, evaluation, 1, cfg, today=TODAY)

    assert payload["gap"] is None


def test_each_order_line_carries_its_name_in_all_three_languages(db, telegram_customer):
    """Master amendment 13: every line names its product in all three languages; the bot prints the
    one of the payload's `language` (tests/telegram_bot/test_webhook_reorder_reminder.py pins that half)."""
    name_product(db, telegram_customer.water, WATER_NAMES)
    telegram_customer.user.preferred_language = "ru-RU"
    db.session.commit()
    customer, evaluation, cfg = _facts(telegram_customer.user.id)

    payload = messaging.telegram_payload(telegram_customer.episode, customer, evaluation, 1, cfg, today=TODAY)

    assert payload["items"] == [{"qty": 3, "names": WATER_NAMES}, {"qty": 2, "names": _names("Cooler cups")}]
    assert payload["language"] == "ru"


@pytest.mark.parametrize(
    "stored, language",
    [("en", "en"), ("ru-RU", "ru"), ("UZ_uz", "uz"), (None, "uz"), ("", "uz"), ("   ", "uz"), ("tr", "uz"), ("kk", "uz")],
)
def test_the_payload_carries_the_one_reminder_language(db, monkeypatch, telegram_customer, stored, language):
    """Final review B1/E1: the backend decides the language ONCE (`reminder_language`) and the bot
    renders in it. A NULL, blank or unsupported stored value (a raw Telegram language_code the bot
    does not support) reads as the system DEFAULT_LANGUAGE, which this deployment sets to uz: the
    language the email and the worklist name for the same customer, never a second answer."""
    monkeypatch.setenv("DEFAULT_LANGUAGE", "uz")
    telegram_customer.user.preferred_language = stored
    db.session.commit()
    customer, evaluation, cfg = _facts(telegram_customer.user.id)

    payload = messaging.telegram_payload(telegram_customer.episode, customer, evaluation, 1, cfg, today=TODAY)

    assert payload["language"] == language


def test_a_line_the_bot_could_not_print_never_costs_the_customer_a_line(db, telegram_customer, sample_category):
    """The payload drops every line `reorder_reminder_copy.printable` refuses BEFORE it takes its
    MAX_ITEMS. Alice's newest delivery has three lines: the largest names a product whose name is
    blank in every language, the smallest has a quantity of 0. Only the water is a line she could
    read, so it is the only one sent: the blank one never takes the first slot, and
    "0 × Hand pump" never takes the second."""
    nameless = make_product(db, sample_category, "   ")
    newest = add_order_lines(
        db,
        delivered_order(db, telegram_customer.user, LAST_DELIVERY + timedelta(hours=1)),
        [(nameless, 9), (telegram_customer.water, 3), (telegram_customer.pump, 0)],
    )
    customer, evaluation, cfg = _facts(telegram_customer.user.id)

    payload = messaging.telegram_payload(telegram_customer.episode, customer, evaluation, 1, cfg, today=TODAY)

    assert (payload["reorder_order_id"], payload["items"]) == (
        newest.id,
        [{"qty": 3, "names": _names("Pure Water 19L")}],
    )


def _show_lines(key, **values):
    """A renderer that shows what the composer passes for each order line."""
    return f"{values['qty']} × {values['name']}" if "qty" in values else "water"


def test_the_payload_and_the_composer_drop_lines_by_the_one_printable_rule(
    db, telegram_customer, sample_category, monkeypatch
):
    """`reorder_reminder_copy.printable` is the one "can this line be printed" rule: the payload
    drops what it refuses, and the bot's `items_text` skips the same lines. Switched off (always
    True), both let the blank-named AND the zero-quantity lines through: they ask that one
    function, not two copies of it. MAX_ITEMS is raised to 3 so the slice cannot be what drops the
    zero line (it sorts last): only the rule may."""
    monkeypatch.setattr(reorder_reminder_copy, "MAX_ITEMS", 3)
    nameless = make_product(db, sample_category, "   ")
    lines = [(nameless, 9), (telegram_customer.water, 3), (telegram_customer.pump, 0)]
    add_order_lines(db, delivered_order(db, telegram_customer.user, LAST_DELIVERY + timedelta(hours=1)), lines)
    customer, evaluation, cfg = _facts(telegram_customer.user.id)
    every_line = [{"qty": qty, "names": _names(product.name)} for product, qty in lines]

    def payload_items():
        episode = telegram_customer.episode
        return messaging.telegram_payload(episode, customer, evaluation, 1, cfg, today=TODAY)["items"]

    assert payload_items() == [every_line[1]]
    assert reorder_reminder_copy.items_text(_show_lines, "en", every_line) == "3 × Pure Water 19L"

    monkeypatch.setattr(reorder_reminder_copy, "printable", lambda item: True)

    assert payload_items() == every_line
    # Both refused lines now reach the composer too (the blank name prints as nothing).
    assert reorder_reminder_copy.items_text(_show_lines, "en", every_line) == (
        "9 × , 3 × Pure Water 19L, 0 × Hand pump"
    )


def test_a_linked_sibling_on_telegram_gets_the_catalogue_button(db, sample_user, second_sample_user, sample_product):
    """§4/D19: the connected Telegram member is the recipient. `reorder_<id>` checks ownership,
    so only the RECIPIENT'S own order may ride on the button. Bob has none, so it opens the catalogue."""
    link(db, sample_user, second_sample_user)
    second_sample_user.first_name = "Bob"
    second_sample_user.telegram_id = "810000002"
    second_sample_user.is_bot_active = True
    db.session.commit()
    last = add_order_lines(db, delivered_order(db, sample_user, LAST_DELIVERY), [(sample_product, 2)])
    episode = make_episode(db, second_sample_user, anchor=last, step=1, next_action_on=TODAY + timedelta(days=7))
    customer, evaluation, cfg = _facts(sample_user.id)
    assert evaluation.recipient.user_id == second_sample_user.id

    payload = messaging.telegram_payload(episode, customer, evaluation, 1, cfg, today=TODAY)

    assert (payload["telegram_id"], payload["name"], payload["reorder_order_id"], payload["items"]) == (
        "810000002", "Bob", None, [],
    )


def test_the_payload_names_the_recipient_not_the_episodes_account(db, telegram_customer, second_sample_user):
    """§4/D19: the lapse is Alice's (her episode, her orders), but her linked sibling Bob talked to
    the bot more recently, so Bob is the run-time recipient. Everything that addresses a person is
    Bob's: his chat, his name, his OWN newest delivered order on the button (`reorder_<id>` checks
    ownership) and that order's lines. The lapse numbers stay the episode's."""
    alice = telegram_customer.user
    link(db, alice, second_sample_user)
    second_sample_user.first_name = "Bob"
    second_sample_user.telegram_id = "810000002"
    second_sample_user.is_bot_active = True
    second_sample_user.last_bot_interaction = NOW - timedelta(hours=1)
    db.session.commit()
    bobs = add_order_lines(
        db,
        delivered_order(db, second_sample_user, LAST_DELIVERY - timedelta(days=3)),
        [(telegram_customer.pump, 4), (telegram_customer.cups, 1)],
    )
    customer, evaluation, cfg = _facts(alice.id)
    assert (telegram_customer.episode.user_id, evaluation.recipient.user_id) == (alice.id, second_sample_user.id)

    payload = messaging.telegram_payload(telegram_customer.episode, customer, evaluation, 1, cfg, today=TODAY)

    assert payload == {
        **_r1_payload(telegram_customer),
        "telegram_id": "810000002",
        "name": "Bob",
        "reorder_order_id": bobs.id,
        "items": [{"qty": 4, "names": _names("Hand pump")}, {"qty": 1, "names": _names("Cooler cups")}],
    }


def test_send_reminder_posts_r1_to_the_bot_under_its_stable_request_id(
    db, app, monkeypatch, telegram_customer, bot_webhook
):
    """The real path: Task 13's send-time re-check, then this adapter."""
    monkeypatch.setitem(app.config, "REORDER_REMINDERS_MODE", "on")

    assert messaging.send_reminder(telegram_customer.episode.id, 1, now=NOW) == "sent"

    (call,) = bot_webhook.calls
    assert call["endpoint"] == "/internal/reorder-reminder"
    assert call["request_id"] == f"reorder-reminder:{telegram_customer.episode.id}:1"
    assert call["payload"] == _r1_payload(telegram_customer)


def test_a_delivered_push_is_ok(db, telegram_customer, bot_webhook):
    customer, evaluation, cfg = _facts(telegram_customer.user.id)

    assert messaging._send_telegram(telegram_customer.episode, customer, evaluation, 1, cfg, now=NOW) == ChannelResult(
        ok=True
    )


def test_a_410_marks_only_this_account_blocked_and_is_final(db, telegram_customer, bot_webhook):
    bot_webhook.answer = BOT_BLOCKED
    customer, evaluation, cfg = _facts(telegram_customer.user.id)

    result = messaging._send_telegram(telegram_customer.episode, customer, evaluation, 1, cfg, now=NOW)

    assert result == ChannelResult(ok=False, detail="bot_blocked", retryable=False)
    user = db.session.get(User, telegram_customer.user.id)
    assert user.is_bot_active is False
    assert ensure_utc(user.telegram_blocked_at) == NOW
    assert [e.kind for e in ReorderReminderEvent.query.filter_by(user_id=user.id).all()] == ["blocked"]


def test_a_410_blocks_the_recipient_never_its_linked_sibling(
    db, app, monkeypatch, telegram_customer, second_sample_user, bot_webhook
):
    """D19: a block is one account's. Alice and her linked sibling Bob are both on Telegram;
    Alice (the newer bot interaction) is the recipient. The bot route answers 410 for her chat:
    the send is recorded `bot_blocked` and Alice is marked blocked, while Bob stays reachable
    on his own chat, with no `blocked` event, for a later lapse to reach."""
    monkeypatch.setitem(app.config, "REORDER_REMINDERS_MODE", "on")
    alice = telegram_customer.user
    link(db, alice, second_sample_user)
    alice.last_bot_interaction = NOW - timedelta(days=1)
    second_sample_user.first_name = "Bob"
    second_sample_user.telegram_id = "810000002"
    second_sample_user.is_bot_active = True
    db.session.commit()
    bot_webhook.answer = BOT_BLOCKED

    assert messaging.send_reminder(telegram_customer.episode.id, 1, now=NOW) == "failed:bot_blocked"

    assert [call["payload"]["telegram_id"] for call in bot_webhook.calls] == [CHAT_ID]
    db.session.expire_all()
    alice, bob = db.session.get(User, alice.id), db.session.get(User, second_sample_user.id)
    assert (alice.is_bot_active, ensure_utc(alice.telegram_blocked_at)) == (False, NOW)
    assert (bob.is_bot_active, bob.telegram_blocked_at) == (True, None)
    assert event_rows(user=alice) == [("blocked", "send_410", None, None), ("reminder_failed", "bot_blocked", 1, None)]
    assert event_rows(user=bob) == []


def test_a_410_for_a_recipient_whose_account_is_gone_is_logged_and_still_final(
    db, telegram_customer, bot_webhook, caplog
):
    """The recipient's user row vanished between the re-check and the bot's answer: there is no
    account to mark blocked, so it is logged, and the send is still final as `bot_blocked`."""
    bot_webhook.answer = BOT_BLOCKED
    customer, evaluation, cfg = _facts(telegram_customer.user.id)
    gone = dataclasses.replace(evaluation, recipient=dataclasses.replace(evaluation.recipient, user_id=987654))

    with captured_logs(caplog, messaging.logger, logging.WARNING):
        result = messaging._send_telegram(telegram_customer.episode, customer, gone, 1, cfg, now=NOW)

    assert result == ChannelResult(ok=False, detail="bot_blocked", retryable=False)
    assert "recipient user 987654 no longer exists" in caplog.text
    assert event_rows(user=telegram_customer.user) == []


@pytest.mark.parametrize(
    "answer, detail",
    [
        (
            {"success": False, "message": "Bot webhook failed: Internal server error", "status_code": 500},
            "telegram_500",
        ),
        ({"success": False, "message": "Bot webhook failed: Bad Gateway", "status_code": 502}, "telegram_502"),
        ({"success": False, "message": "Bot webhook timeout"}, "telegram_error"),
    ],
)
def test_any_other_failure_is_retryable_and_blocks_nobody(db, telegram_customer, bot_webhook, answer, detail):
    bot_webhook.answer = answer
    customer, evaluation, cfg = _facts(telegram_customer.user.id)

    result = messaging._send_telegram(telegram_customer.episode, customer, evaluation, 1, cfg, now=NOW)

    assert result == ChannelResult(ok=False, detail=detail, retryable=True)
    user = db.session.get(User, telegram_customer.user.id)
    assert (user.is_bot_active, user.telegram_blocked_at) == (True, None)
