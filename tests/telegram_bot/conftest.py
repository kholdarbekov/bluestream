"""Test bootstrap for telegram_bot modules with local-style imports."""

import logging
import sys
from pathlib import Path

import pytest


BOT_DIR = Path(__file__).resolve().parents[2] / "telegram_bot"

if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))


# Importing `telegram_bot.bot` (e.g. in test_support_capture.py) runs
# `logging_config.setup_logging()` at module import, which globally sets these
# application loggers to `propagate=False`. That breaks pytest's `caplog`, which
# captures records via propagation to the root logger — so any test asserting on
# one of these loggers (e.g. a `handlers.base` WARNING) sees an empty
# `caplog.records` once `bot` has been imported in the worker. Force propagation
# back on before every telegram_bot test so caplog works regardless of import
# order or xdist worker scheduling. This only affects the test process; the bot's
# production logging (intentional `propagate=False`) is unchanged.
_BOT_LOGGERS = ("bot", "handlers", "api_client", "utils", "database", "config")


@pytest.fixture(autouse=True)
def _restore_log_propagation():
    for name in _BOT_LOGGERS:
        logging.getLogger(name).propagate = True
    yield
