"""The customer bot's startup lines and Sentry events carry no secret (D2 fix round 2).

- `log_bot_startup_info()` runs on every start. It logged the Postgres password (round 1) and 10 characters of the
  bot token's secret half; now it shows the redacted database URL and only the bot id, which is public.
- `sentry_scrub.before_send` scrubbed by key name only, so a credential URL in a breadcrumb's message (an INFO line
  the LoggingIntegration recorded) or in an exception value reached Sentry.
"""

import logging

import pytest

# Bare names, as the bot imports them: the conftest ranks telegram_bot/ first while this module is collected, so
# `config` here is the module `log_bot_startup_info` finds in sys.modules when it runs `from config import config`.
from config import config as bot_config
from sentry_scrub import before_send

# Package-qualified: when both bot suites share a process, the staff conftest owns the bare `logging_config` name.
from telegram_bot.logging_config import log_bot_startup_info

DB_PASSWORD = "pg-fake-Q7"
BOT_ID = "7012345678"
TOKEN_SECRET = "AAQzX9kV7wPq2Lm4RtYbN8cJ3hG6dF5sUeW"  # fake, shaped like a token's secret half
DATABASE_URL = f"postgresql://bot:{DB_PASSWORD}@db-host:5432/bluestream"
REDACTED_DATABASE_URL = "postgresql://bot:***@db-host:5432/bluestream"


def _parts_of(secret, size=4):
    """Every 4-character window of `secret`: a log line holding any part of it (a tail, a head) contains one."""
    return {secret[i : i + size] for i in range(len(secret) - size + 1)}


@pytest.fixture
def startup_messages(caplog, monkeypatch):
    def run(token):
        monkeypatch.setattr(bot_config.telegram, "bot_token", token)
        monkeypatch.setattr(bot_config.database, "url", DATABASE_URL)
        monkeypatch.setattr(logging.getLogger("bot"), "disabled", False)
        caplog.set_level(logging.INFO, logger="bot")  # the conftest turns the logger's propagation back on
        log_bot_startup_info()
        return [record.getMessage() for record in caplog.records]

    return run


def test_startup_lines_show_the_bot_id_and_a_redacted_database_url(startup_messages):
    messages = startup_messages(f"{BOT_ID}:{TOKEN_SECRET}")

    assert f"Bot Token: {BOT_ID}:***" in messages
    assert f"Database URL: {REDACTED_DATABASE_URL}" in messages
    assert not any(DB_PASSWORD in message for message in messages)
    assert not any(part in message for message in messages for part in _parts_of(TOKEN_SECRET))


@pytest.mark.parametrize(
    "token, shown",
    [
        pytest.param("", "NOT SET", id="no-token"),
        pytest.param(None, "NOT SET", id="token-is-none"),
        pytest.param(TOKEN_SECRET, "***", id="token-without-a-colon"),
        # fix round 3 (G4): only the text before the FIRST `:` is the public bot id; rpartition showed the rest
        pytest.param(f"{BOT_ID}:{TOKEN_SECRET[:17]}:{TOKEN_SECRET[17:]}", f"{BOT_ID}:***", id="secret-holds-a-colon"),
        pytest.param(f"{BOT_ID}:{TOKEN_SECRET}:", f"{BOT_ID}:***", id="trailing-colon"),
        pytest.param(f"{BOT_ID}::{TOKEN_SECRET}", f"{BOT_ID}:***", id="double-colon"),
    ],
)
def test_startup_token_line_shows_no_secret_for_any_token_shape(startup_messages, token, shown):
    messages = startup_messages(token)

    assert f"Bot Token: {shown}" in messages
    assert not any(part in message for message in messages for part in _parts_of(TOKEN_SECRET))


def test_scrubber_redacts_credential_urls_in_breadcrumb_messages_and_exception_values():
    event = {
        "exception": {"values": [{"type": "OperationalError", "value": f"could not connect to {DATABASE_URL}"}]},
        "breadcrumbs": {
            "values": [
                {"category": "bot", "message": f"Database URL: {DATABASE_URL}", "data": {"phone": "+998901234567"}},
            ]
        },
    }

    out = before_send(event)

    assert DB_PASSWORD not in repr(out)
    assert out["exception"]["values"][0]["value"] == f"could not connect to {REDACTED_DATABASE_URL}"
    assert out["breadcrumbs"]["values"][0]["message"] == f"Database URL: {REDACTED_DATABASE_URL}"
    assert out["breadcrumbs"]["values"][0]["data"]["phone"] == "[REDACTED]"  # the key scrubbing still holds
