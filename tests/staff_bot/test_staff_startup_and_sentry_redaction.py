"""The staff bot's startup lines and Sentry events carry no secret (D2 fix round 2).

- `log_bot_startup_info()` runs on every start. It logged the Postgres password (round 1) and 10 characters of the
  bot token's secret half; now it shows the redacted database URL and only the bot id, which is public.
- `sentry_sdk.init` runs at IMPORT time and only when `SENTRY_DSN` is set, which the test process blanks, so its
  `before_send` is pinned on the syntax tree.
"""

import ast
import logging
from pathlib import Path

import pytest

from staff_bot.config import config as staff_config
from staff_bot.logging_config import log_bot_startup_info

DB_PASSWORD = "pg-fake-Q7"
BOT_ID = "7012345678"
TOKEN_SECRET = "AAQzX9kV7wPq2Lm4RtYbN8cJ3hG6dF5sUeW"  # fake, shaped like a token's secret half
REPO = Path(__file__).resolve().parents[2]


def _parts_of(secret, size=4):
    """Every 4-character window of `secret`: a log line holding any part of it (a tail, a head) contains one."""
    return {secret[i : i + size] for i in range(len(secret) - size + 1)}


@pytest.fixture
def startup_messages(caplog, monkeypatch):
    def run(token):
        monkeypatch.setattr(staff_config.telegram, "bot_token", token)
        monkeypatch.setattr(staff_config.database, "url", f"postgresql://staff:{DB_PASSWORD}@db-host:5432/bluestream")
        bot_logger = logging.getLogger("staff_bot")
        # importing staff_bot.bot sets propagate=False on this logger, and caplog captures through propagation
        monkeypatch.setattr(bot_logger, "propagate", True)
        monkeypatch.setattr(bot_logger, "disabled", False)
        caplog.set_level(logging.INFO, logger="staff_bot")
        log_bot_startup_info()
        return [record.getMessage() for record in caplog.records]

    return run


def test_startup_lines_show_the_bot_id_and_a_redacted_database_url(startup_messages):
    messages = startup_messages(f"{BOT_ID}:{TOKEN_SECRET}")

    assert f"Bot Token: {BOT_ID}:***" in messages
    assert "Database URL: postgresql://staff:***@db-host:5432/bluestream" in messages
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


def test_staff_bot_sentry_init_redacts_url_credentials_in_every_event():
    tree = ast.parse((REPO / "staff_bot" / "bot.py").read_text(encoding="utf-8"))
    inits = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "init"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "sentry_sdk"
    ]
    assert len(inits) == 1
    before_send = {keyword.arg: keyword.value for keyword in inits[0].keywords}.get("before_send")
    assert isinstance(before_send, ast.Name) and before_send.id == "redact_event_urls"

    # bound at module level BEFORE the init: a later import would raise NameError only where SENTRY_DSN is set
    imports = [
        node
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        and node.module == "shared.url_redaction"
        and any(alias.name == "redact_event_urls" and alias.asname is None for alias in node.names)
    ]
    assert imports and imports[0].lineno < inits[0].lineno
