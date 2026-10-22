"""Unit tests for BotUserRepository.get_user_loyalty_eligible.

Tests that:
- True db value → True returned
- False db value → False returned
- None db value (no row) → True returned (default-open for unknown users)
- LOYALTY_ELIGIBLE_SQL appears in the query
- telegram_id is passed as the string "123"
"""
from unittest.mock import create_autospec

import pytest

from database import BotUserRepository
from shared.loyalty_eligibility import LOYALTY_ELIGIBLE_SQL
from tests.telegram_bot.helpers import DummyCallbackQuery, DummyUpdate, make_context


class _FakeDB:
    """Minimal async DB stub that records the last query/args and returns a fixed value."""

    def __init__(self, return_value):
        self._return_value = return_value
        self.last_query = None
        self.last_args = None

    async def fetchval(self, query, *args):
        self.last_query = query
        self.last_args = args
        return self._return_value


@pytest.mark.unit
@pytest.mark.anyio
class TestGetUserLoyaltyEligible:
    async def test_returns_true_when_db_returns_true(self):
        db = _FakeDB(True)
        repo = BotUserRepository(db)
        result = await repo.get_user_loyalty_eligible(123)
        assert result is True

    async def test_returns_false_when_db_returns_false(self):
        db = _FakeDB(False)
        repo = BotUserRepository(db)
        result = await repo.get_user_loyalty_eligible(123)
        assert result is False

    async def test_returns_true_when_db_returns_none(self):
        """No row found (unknown user) → default-open → True."""
        db = _FakeDB(None)
        repo = BotUserRepository(db)
        result = await repo.get_user_loyalty_eligible(123)
        assert result is True

    async def test_loyalty_eligible_sql_in_query(self):
        db = _FakeDB(True)
        repo = BotUserRepository(db)
        await repo.get_user_loyalty_eligible(123)
        assert LOYALTY_ELIGIBLE_SQL in db.last_query

    async def test_telegram_id_passed_as_string(self):
        db = _FakeDB(True)
        repo = BotUserRepository(db)
        await repo.get_user_loyalty_eligible(123)
        assert db.last_args == ("123",)


@pytest.mark.unit
@pytest.mark.anyio
async def test_ensure_loyalty_eligible_blocks_and_sends_the_notice_with_the_menu(monkeypatch):
    import eligibility
    import menu_keyboard
    from handlers import base as base_module
    from handlers.base import BaseHandler
    from i18n import i18n

    async def _ineligible(_tid):
        return False
    monkeypatch.setattr(eligibility, "is_loyalty_eligible", _ineligible)
    send = create_autospec(menu_keyboard.send_menu_keyboard)
    monkeypatch.setattr(base_module, "send_menu_keyboard", send)

    update = DummyUpdate()
    update.callback_query = DummyCallbackQuery(data="menu_loyalty")
    context = make_context()

    ok = await BaseHandler()._ensure_loyalty_eligible(update, context, 123, "en")

    assert ok is False
    update.callback_query.answer.assert_awaited_once_with()
    send.assert_awaited_once_with(
        context.bot, 1001, 123, i18n.get("telegram.loyalty.not_available", "en"), "en"
    )
    update.callback_query.edit_message_text.assert_not_awaited()
