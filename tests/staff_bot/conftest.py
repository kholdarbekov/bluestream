"""Test bootstrap for staff_bot modules with local-style imports."""

import importlib
import sys
from pathlib import Path


BOT_DIR = Path(__file__).resolve().parents[2]

if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))


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
