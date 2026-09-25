"""The last-resort error handler, driven through the real dispatcher.

``staff_bot.bot.error_handler`` is all a driver gets when a handler raises
something nobody caught. Most handlers answer the tap bare on entry, and
Telegram shows only the FIRST answer to a tap, so by the time an exception
escapes the notice has to be a chat message. It is registered in
``StaffBot.initialize``, not ``_setup_handlers``, so the harness never ran it;
every test here registers the production function itself.

The seam is the smallest one that makes a REAL registered handler raise:
``ActiveDeliveryHandler._get_language``, looked up on every call.
``navigate_to_address`` answers the tap and THEN asks for the language;
``decline_suggestion`` asks first and answers after; the "Active deliveries"
menu text reaches ``show_active_deliveries``, whose first line asks too.
"""
from __future__ import annotations

import logging

import pytest

from staff_bot.bot import error_handler
from staff_bot.handlers.base import BaseHandler
from staff_bot.handlers.delivery.active_delivery import ActiveDeliveryHandler

from tests.staff_bot.test_staff_delivery_journey_dispatcher import (
    build_driver,
    capture_errors,
    copy_for,
    menu_label,
    sign_in,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


class HandlerCrash(RuntimeError):
    """An exception no handler catches."""


@pytest.fixture
async def bot(monkeypatch):
    harness = await build_driver(monkeypatch)
    harness.application.add_error_handler(error_handler)
    return harness


def crash_the_active_delivery_handler(monkeypatch):
    async def _crash(self, update, context):
        raise HandlerCrash("nobody caught this")

    monkeypatch.setattr(ActiveDeliveryHandler, "_get_language", _crash)


async def test_a_crash_after_the_tap_was_answered_reaches_the_driver_as_a_message(bot, monkeypatch):
    driver, _labels = await sign_in(bot)
    errors = capture_errors(bot)
    crash_the_active_delivery_handler(monkeypatch)

    await bot.send(driver.tap("staff_navigate_501"))

    assert [type(error) for error in errors] == [HandlerCrash], "the seam did not make the handler raise"
    answers = bot.telegram.of("answerCallbackQuery")
    assert len(answers) == 1 and not answers[0].params.get("text"), (
        "the only answer must be the handler's own bare one; a second answer "
        f"carrying the error is accepted by Telegram and never shown: {answers}"
    )
    assert [call.text for call in bot.telegram.of("sendMessage")] == [copy_for("staff.error_occurred")]


async def test_a_crash_before_the_tap_was_answered_is_a_popup_and_nothing_else(bot, monkeypatch):
    driver, _labels = await sign_in(bot)
    errors = capture_errors(bot)
    crash_the_active_delivery_handler(monkeypatch)

    await bot.send(driver.tap("staff_decline_suggestion_501"))

    assert [type(error) for error in errors] == [HandlerCrash], "the seam did not make the handler raise"
    answers = bot.telegram.of("answerCallbackQuery")
    assert [(call.params.get("text"), call.params.get("show_alert")) for call in answers] == [
        (copy_for("staff.error_occurred"), True)
    ]
    assert bot.telegram.of("sendMessage") == [], "a popup was shown, so no message is owed"


async def test_a_crash_on_a_menu_text_is_answered_with_a_reply(bot, monkeypatch):
    driver, labels = await sign_in(bot)
    errors = capture_errors(bot)
    crash_the_active_delivery_handler(monkeypatch)

    await bot.send(driver.text(menu_label(labels, "staff.menu.active_deliveries")))

    assert [type(error) for error in errors] == [HandlerCrash], "the seam did not make the handler raise"
    assert [call.text for call in bot.telegram.of("sendMessage")] == [copy_for("staff.error_occurred")]
    assert bot.telegram.of("answerCallbackQuery") == []


async def test_a_failure_inside_the_backstop_is_logged_not_swallowed(bot, monkeypatch, caplog):
    """If notifying the user ever breaks (``_notify_user`` runs on an instance
    built without ``__init__``), the driver sees nothing — the log must say so."""
    driver, labels = await sign_in(bot)
    crash_the_active_delivery_handler(monkeypatch)

    async def _broken_notify(self, *_args, **_kwargs):
        raise AttributeError("'BaseHandler' object has no attribute 'user_repo'")

    monkeypatch.setattr(BaseHandler, "_notify_user", _broken_notify)
    # `setup_logging()` points the staff_bot logger away from pytest's root
    # handler; without this the assertion below could never see the record.
    staff_logger = logging.getLogger("staff_bot")
    monkeypatch.setattr(staff_logger, "disabled", False)
    monkeypatch.setattr(staff_logger, "propagate", True)

    with caplog.at_level(logging.ERROR, logger="staff_bot"):
        await bot.send(driver.text(menu_label(labels, "staff.menu.active_deliveries")))

    failures = [
        record for record in caplog.records
        if record.getMessage() == "Global error handler failed to notify the user"
    ]
    assert len(failures) == 1, "the backstop's own failure left no trace in the logs"
    assert failures[0].exc_info and failures[0].exc_info[0] is AttributeError
    assert bot.telegram.of("sendMessage", "answerCallbackQuery") == []
