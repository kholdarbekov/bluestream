"""Test bootstrap for telegram_bot modules with local-style imports."""

import sys
from pathlib import Path


BOT_DIR = Path(__file__).resolve().parents[2] / "telegram_bot"

if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))
