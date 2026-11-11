"""Importing the staff bot under pytest leaves no Sentry client that can send (owner rule 2026-11-10).

`staff_bot/bot.py` calls `sentry_sdk.init` at IMPORT time whenever `SENTRY_DSN` is set, tracing every
transaction by default, and the docker test runners pass the real `.env`, which holds the production DSN. So
every run of the staff-bot dispatcher tests reported its errors and performance traces to the real Sentry
project ("Sentry is attempting to send 2 pending events"). tests/conftest.py now blanks the DSN in the
environment before any bot module is imported, and strips the DSN from every Sentry client the test process
builds, so neither the module-level init nor any later one can send.
"""

import sentry_sdk

import staff_bot.bot  # noqa: F401 - the import is the act under test: it runs the bot's module-level Sentry init
from staff_bot.config import config


def test_importing_the_staff_bot_leaves_no_sentry_client_that_can_send():
    client = sentry_sdk.get_client()

    assert (client.dsn, client.transport) == (None, None)
    assert config.sentry.dsn in ("", None)
