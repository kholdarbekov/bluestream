"""Import a `telegram_bot` / `staff_bot` module from an integration test, safely.

WHY THIS EXISTS
---------------
The bots use workdir-relative BARE imports (`from config import config`,
`from i18n import i18n`, `from utils.x import y`), so their package directory
must be on `sys.path` and the module is imported by its bare name.

That works in `tests/telegram_bot/`, whose conftest puts the bot directory on
`sys.path` first. It breaks in `tests/integration/`, because by then the
REPO-ROOT `config.py` is usually already sitting in `sys.modules['config']` and
shadows `telegram_bot/config.py` for the rest of the process. The bot's
`from config import config` then finds the root module, which exposes `Config`
(a class) and not `config` (an instance)::

    ImportError: cannot import name 'config' from 'config' (/app/config.py).
                 Did you mean: 'Config'?

Inserting the bot directory at `sys.path[0]` does NOT fix it: `sys.path` is only
consulted on a cache MISS, and the stale entry is already cached.

Reproduced with zero project files of our own::

    bash scripts/precommit-backend-tests.sh \
        tests/telegram_bot/test_api_client.py \
        tests/integration/<any file importing a bot handler>
    -> 2 errors at COLLECTION

Each directory passes alone, which is why this stayed invisible: it only appears
when a bot-driving integration test shares a pytest process with the bot's own
suite, and it fails at COLLECTION, taking the whole file's tests with it rather
than failing one assertion.

WHY IT IS SAFE TO EVICT AND NOT RESTORE
---------------------------------------
Verified: NOTHING in `business_app/` or `shared/` imports the root `config`
module (`grep -rn "^from config import\\|^import config$" business_app/ shared/`
-> no hits). The root `config.py` occupying that name is accidental, not
depended upon. The bot, by contrast, needs its own `config` for the whole
lifetime of the test — including LAZY imports executed inside handler calls long
after import time.

That last point is why this helper does not restore `sys.path` afterwards. An
earlier version of this file did, and handler calls then failed with
`ModuleNotFoundError: No module named 'utils'` when they hit a lazy import.

USAGE
-----
    from tests.integration._bot_import import REPO_ROOT, import_bot_module

    products_module = import_bot_module("telegram_bot", "handlers.products")

⚠️ Importing BOTH bots into one pytest process would make them fight over these
same bare names. No test does that today. If one ever needs to, this helper is
the place to solve it — do not paper over it at the call site.
"""
from __future__ import annotations

import importlib
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# Bare names a bot package owns that can also be resolved from the repo root.
# `config` is the one that actually collides today; the rest are listed because
# they are bare-importable from inside the bot packages and would collide the
# same way if a root module of that name were ever added. If a NEW ImportError
# of this shape appears, extending this tuple is the fix.
_SHADOWED = ("config", "i18n", "api_client", "handlers", "keyboards", "utils")


def import_bot_module(bot_package: str, dotted_name: str):
    """Import ``dotted_name`` from ``bot_package`` regardless of import order.

    ``bot_package`` is the directory name (``"telegram_bot"`` / ``"staff_bot"``);
    ``dotted_name`` is the module path as the bot itself writes it
    (``"handlers.products"``).

    The bot directory is left on ``sys.path`` deliberately — the bot's lazy
    imports need it for the rest of the process.
    """
    bot_dir = REPO_ROOT / bot_package
    if not bot_dir.is_dir():  # pragma: no cover - misuse
        raise ValueError(f"not a bot package directory: {bot_dir}")

    # PRESENCE IS NOT ENOUGH — it must be FIRST. `tests/telegram_bot/conftest.py`
    # already appends the bot directory, and pytest then inserts its rootdir
    # (`/app`) at position 0 when it collects a second package. A "insert only if
    # absent" check therefore silently leaves the bot directory ranked BEHIND the
    # repo root, and `from config import config` keeps resolving to `/app/config.py`.
    # Move it to the front unconditionally.
    bot_path = str(bot_dir)
    while bot_path in sys.path:
        sys.path.remove(bot_path)
    sys.path.insert(0, bot_path)

    # Drop any root-resolved occupants so the loader consults sys.path again,
    # now with the bot directory in front. Modules already resolved FROM the bot
    # directory are left alone — re-importing them would hand callers a second,
    # non-identical module object.
    for name in _SHADOWED:
        existing = sys.modules.get(name)
        if existing is None:
            continue
        origin = getattr(existing, "__file__", None) or ""
        if not origin.startswith(str(bot_dir)):
            sys.modules.pop(name, None)

    return importlib.import_module(dotted_name)
