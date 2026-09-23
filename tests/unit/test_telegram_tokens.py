"""`get_staff_bot_token`: one lookup order for the staff bot's secret (D27).

Notification sends and the visit-photo proxy both need it, and a `file_id` fetched with the
wrong bot's token is a dead id -- so the two must never disagree about which bot they mean.
"""
import pytest

from business_app.utils.telegram_tokens import get_staff_bot_token

NAMES = ("STAFF_BOT_TOKEN", "STAFF_TELEGRAM_BOT_TOKEN")


@pytest.fixture
def bare(app, monkeypatch):
    # `.env` reaches the test container, so the real secret is in os.environ until removed here.
    for name in NAMES:
        monkeypatch.delitem(app.config, name, raising=False)
        monkeypatch.delenv(name, raising=False)
    return app


def test_config_beats_environment_and_the_short_name_beats_the_long_one(bare, monkeypatch):
    monkeypatch.setenv("STAFF_BOT_TOKEN", "env-short")
    monkeypatch.setitem(bare.config, "STAFF_TELEGRAM_BOT_TOKEN", "config-long")
    with bare.app_context():
        assert get_staff_bot_token() == "config-long"
    monkeypatch.setitem(bare.config, "STAFF_BOT_TOKEN", "config-short")
    with bare.app_context():
        assert get_staff_bot_token() == "config-short"


def test_the_environment_is_the_fallback(bare, monkeypatch):
    monkeypatch.setenv("STAFF_TELEGRAM_BOT_TOKEN", "env-long")
    with bare.app_context():
        assert get_staff_bot_token() == "env-long"
    monkeypatch.setenv("STAFF_BOT_TOKEN", "env-short")
    with bare.app_context():
        assert get_staff_bot_token() == "env-short"


def test_no_token_anywhere_is_none(bare):
    with bare.app_context():
        assert get_staff_bot_token() is None
