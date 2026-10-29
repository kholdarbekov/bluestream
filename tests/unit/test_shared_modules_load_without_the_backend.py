"""The shared/ modules the customer bot imports load without business_app (step 4 Task 9, fix round 1).

The telegram_bot image ships shared/ and telegram_bot/ and no business_app/ (Dockerfile `telegram_bot` stage,
docker-compose.yml's two mounts). A module in shared/ that imports business_app, even indirectly, raises
ModuleNotFoundError when the bot starts, while every other test stays green because the test run has the whole
repository on its path. So each module loads here in a fresh interpreter that cannot import business_app.

Task 16a adds `shared.telegram_login` (the Telegram sign-in allowlist, built on `safe_next_path`) to the list.
"""

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]

# The shared/ modules the customer bot imports.
BOT_SHARED_MODULES = ["shared.safe_paths"]

_LOAD_WITHOUT_THE_BACKEND = """
import importlib.abc
import sys


class NoBackend(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "business_app" or name.startswith("business_app."):
            raise ModuleNotFoundError(f"No module named {name!r} (the bot image has no business_app)", name=name)
        return None


sys.meta_path.insert(0, NoBackend())
module = __import__(sys.argv[1], fromlist=["_"])
print(module.__name__)
"""


def _load_without_the_backend(module):
    return subprocess.run(
        [sys.executable, "-c", _LOAD_WITHOUT_THE_BACKEND, module],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize("module", BOT_SHARED_MODULES)
def test_a_shared_module_the_bot_imports_loads_without_business_app(module):
    loaded = _load_without_the_backend(module)

    assert loaded.returncode == 0, loaded.stderr
    assert loaded.stdout.strip() == module


def test_the_check_would_catch_a_module_that_imports_business_app():
    """The blocker itself: a module that reaches business_app fails to load, as it would in the bot image."""
    loaded = _load_without_the_backend("business_app.utils.page_auth")

    assert loaded.returncode != 0
    assert "No module named 'business_app'" in loaded.stderr
