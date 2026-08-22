"""Test bootstrap for staff_bot modules with local-style imports."""

import importlib
import sys
from pathlib import Path


BOT_DIR = Path(__file__).resolve().parents[2]

# APPENDED, never inserted at position 0. `tests/telegram_bot/conftest.py`
# ranks `telegram_bot/` FIRST so the customer bot's workdir-relative bare
# imports (`from config import config`) resolve to `telegram_bot/config.py`.
# Conftest files are imported once, in collection order, so a `sys.path.insert(
# 0, repo_root)` here would outrank it for the rest of the worker process and
# every telegram_bot module would then fail to import with
#     ImportError: cannot import name 'config' from 'config' (/app/config.py)
# — which is exactly what running the two bot suites in one pytest invocation
# used to do. The repo root is already importable as pytest's rootdir; this
# append only guarantees it.
if str(BOT_DIR) not in sys.path:
    sys.path.append(str(BOT_DIR))


# `staff_bot/bot.py` runs with WORKDIR=/app/staff_bot in production, so it uses
# two staff-dir-relative bare imports: `from logging_config import ...` and
# `from webhook_server import ...`. Importing `staff_bot.bot` in tests would
# otherwise ModuleNotFoundError. Rather than putting the staff_bot dir on
# sys.path (which would shadow common module names like `config`/`database`),
# alias those two modules from their package-qualified counterparts. Surgical
# and side-effect-free for tests that never touch bot.py.
for _bare, _pkg in (("logging_config", "staff_bot.logging_config"),
                    ("webhook_server", "staff_bot.webhook_server")):
    if _bare not in sys.modules:
        try:
            sys.modules[_bare] = importlib.import_module(_pkg)
        except Exception:
            # Leave unresolved; tests that don't import bot.py are unaffected.
            pass
