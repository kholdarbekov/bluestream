"""Importing the customer bot under pytest leaves no Sentry client that can send (owner rule 2026-11-10).

`telegram_bot/bot.py` calls `sentry_sdk.init` at IMPORT time whenever `SENTRY_DSN` is set, and the docker test
runners pass the real `.env`, which holds the production DSN. tests/conftest.py blanks the DSN in the environment
before any bot module is imported, and strips the DSN from every Sentry client the test process builds.
"""

import importlib

import sentry_sdk

# The customer bot runs with telegram_bot/ as its workdir, so it is imported by its bare name; tests/telegram_bot/
# conftest.py ranks that directory first. The import is the act under test: it runs the module-level Sentry init.
importlib.import_module("bot")
from config import config  # noqa: E402 - the customer bot's config, resolvable only after the bot path is ranked


def test_importing_the_customer_bot_leaves_no_sentry_client_that_can_send():
    client = sentry_sdk.get_client()

    assert (client.dsn, client.transport) == (None, None)
    assert config.sentry.dsn in ("", None)
