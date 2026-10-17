"""
Unit tests for SessionCleanupService.

Regression coverage for the nightly cleanup failing with
sqlalchemy.exc.ArgumentError ("expected ORM mapped attribute for loader
strategy argument") caused by string arguments to load_only(). The service
swallows the exception and reports it via stats["errors"], so assertions
are made on the returned stats rather than on raised exceptions.
"""

from datetime import datetime, timedelta, timezone

import pytest

from business_app.models.user import UserSession
from business_app.services.session_cleanup_service import SessionCleanupService
from business_app.utils.timezone_utils import ensure_utc


@pytest.fixture
def cleanup_service():
    return SessionCleanupService()


@pytest.mark.unit
class TestSessionCleanupService:
    def test_cleanup_expired_sessions_purges_old_inactive_sessions(self, db, sample_user, cleanup_service):
        now = datetime.now(timezone.utc)
        expired_session = UserSession(
            user_id=sample_user.id,
            session_token="expired-session-token",
            expires_at=now - timedelta(days=45),  # past the 30-day removal threshold
            is_active=False,
            ended_at=now - timedelta(days=45),
        )
        db.session.add(expired_session)
        db.session.commit()

        stats = cleanup_service.cleanup_expired_sessions()

        assert stats["errors"] == 0
        assert stats["expired_sessions_removed"] >= 1
        assert UserSession.query.filter_by(session_token="expired-session-token").first() is None

    def test_cleanup_inactive_users_reports_no_errors(self, db, sample_user, cleanup_service):
        # Make the sample user eligible for inactivity cleanup so the
        # load_only()-backed query actually returns rows.
        stale = datetime.now(timezone.utc) - timedelta(days=400)
        sample_user.last_login = stale
        sample_user.created_at = stale
        sample_user.status = "active"
        db.session.commit()

        stats = cleanup_service.cleanup_inactive_users()

        assert stats["errors"] == 0
        assert stats["users_marked_inactive"] >= 1

    def test_the_bot_state_sweep_never_touches_reachability(self, db, sample_user, cleanup_service):
        """§7.2 d: the 180-day sweep keeps clearing a stale bot_state, and writes nothing else.
        (A behaviour pin: the guard test is what fails before the change.)"""
        now = datetime.now(timezone.utc)
        blocked_at = now - timedelta(days=190)
        sample_user.bot_state = '{"awaiting_input": "support_message"}'
        sample_user.is_bot_active = False
        sample_user.telegram_blocked_at = blocked_at
        sample_user.last_bot_interaction = now - timedelta(days=200)
        sample_user.last_login = now
        db.session.commit()

        stats = cleanup_service.cleanup_inactive_users()

        db.session.expire_all()
        assert stats["errors"] == 0
        assert stats["bot_states_cleared"] == 1
        assert sample_user.bot_state is None
        assert sample_user.is_bot_active is False
        assert ensure_utc(sample_user.telegram_blocked_at) == blocked_at
