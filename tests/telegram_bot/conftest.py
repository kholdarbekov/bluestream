"""Test bootstrap for telegram_bot modules with local-style imports."""

import logging
import sys
from pathlib import Path

import pytest


BOT_DIR = Path(__file__).resolve().parents[2] / "telegram_bot"

# PRESENCE IS NOT ENOUGH — the bot directory must rank FIRST. When this suite
# shares a pytest process with any other package, pytest inserts its rootdir
# (`/app`) at position 0, and an "insert only if absent" check leaves BOT_DIR
# ranked behind it. The bot's workdir-relative `from config import config` then
# resolves to the repo-root `config.py`, which exposes `Config` (a class) and not
# `config` (an instance), and every module here fails at COLLECTION:
#     ImportError: cannot import name 'config' from 'config' (/app/config.py)
# `sys.path` is only consulted on a cache MISS, so a stale `sys.modules` entry
# must be evicted too. Mirrors `tests/integration/_bot_import.py`, which solves
# the same collision from the other side.
_BOT_PATH = str(BOT_DIR)
while _BOT_PATH in sys.path:
    sys.path.remove(_BOT_PATH)
sys.path.insert(0, _BOT_PATH)

for _shadowed in ("config", "i18n", "api_client", "handlers", "keyboards", "utils"):
    _existing = sys.modules.get(_shadowed)
    if _existing is not None:
        _origin = getattr(_existing, "__file__", None) or ""
        if not _origin.startswith(_BOT_PATH):
            sys.modules.pop(_shadowed, None)


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
