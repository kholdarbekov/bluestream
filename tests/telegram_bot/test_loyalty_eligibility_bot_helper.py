"""Unit tests for BotUserRepository.get_user_loyalty_eligible.

Tests that:
- True db value → True returned
- False db value → False returned
- None db value (no row) → True returned (default-open for unknown users)
- LOYALTY_ELIGIBLE_SQL appears in the query
- telegram_id is passed as the string "123"
"""
import pytest

from database import BotUserRepository
from shared.loyalty_eligibility import LOYALTY_ELIGIBLE_SQL


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
async def test_ensure_loyalty_eligible_blocks_and_toasts(monkeypatch):
    import eligibility
    from handlers.base import BaseHandler

    async def _ineligible(_tid):
        return False
    monkeypatch.setattr(eligibility, "is_loyalty_eligible", _ineligible)

    answered = {}
    class _Query:
        async def answer(self, text=None, show_alert=False):
            answered["text"] = text
        async def edit_message_text(self, **kw):
            answered["edited"] = True
    class _Update:
        callback_query = _Query()

    ok = await BaseHandler()._ensure_loyalty_eligible(_Update(), {}, 123, "en")
    assert ok is False
    assert "edited" in answered  # returned to a menu
