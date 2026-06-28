"""Benign 'recipient unreachable' Telegram errors (bot blocked, user
deactivated, chat not found) must be classified so the webhook notifier logs
them at WARNING rather than ERROR.

A driver who blocks the staff bot otherwise floods the error logs on every
new-order broadcast — prod saw 11 ERROR lines in a single day for one blocked
driver (staff_bot webhook new_order_handler).
"""

import pytest
from telegram.error import Forbidden, BadRequest

from staff_bot.webhook_server import _is_recipient_unreachable


@pytest.mark.unit
def test_bot_blocked_by_user_is_unreachable():
    assert _is_recipient_unreachable(Forbidden("Forbidden: bot was blocked by the user")) is True


@pytest.mark.unit
def test_user_deactivated_is_unreachable():
    assert _is_recipient_unreachable(Forbidden("Forbidden: user is deactivated")) is True


@pytest.mark.unit
def test_chat_not_found_is_unreachable():
    assert _is_recipient_unreachable(BadRequest("Chat not found")) is True


@pytest.mark.unit
def test_transient_network_error_is_not_unreachable():
    # A genuine fault must still surface as ERROR.
    assert _is_recipient_unreachable(RuntimeError("connection reset by peer")) is False
