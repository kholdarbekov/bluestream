"""The website's Telegram link flow is the backend's answer plus a navigation, never a built link (spec 2026-10-19 D6)."""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

STATIC = Path(__file__).resolve().parents[2] / "business_app"
MY_ACCOUNT_JS = STATIC / "static" / "js" / "pages" / "my-account.js"
REGISTER_JS = STATIC / "static" / "js" / "pages" / "register.js"
REGISTER_HTML = STATIC / "templates" / "frontend" / "register.html"


def _text(path: Path) -> str:
    """The raw file: the bans below also cover comments (controller ruling C5)."""
    return path.read_text(encoding="utf-8")


def test_the_connect_button_asks_the_backend_and_navigates_without_a_link():
    code = _text(MY_ACCOUNT_JS)
    assert "apiRequest('/auth/telegram-link?lang=' + encodeURIComponent(PAGE_DATA.language), { method: 'POST' })" in code
    assert "window.location.href = result.data.telegram_link" in code
    assert "document.querySelectorAll('[data-action=\"connect-telegram\"]')" in code
    # `\bt\.me`: a bare substring ban would hit `result.message`, which the flow must read.
    assert re.search(r"\bt\.me\b", code) is None
    assert all(s not in code for s in ("window.open", "telegram_coming_soon"))


def test_the_register_page_has_no_fake_telegram_link_flow():
    for path in (REGISTER_JS, REGISTER_HTML):
        text = _text(path)
        assert re.search(r"/link\b", text) is None and "link_telegram" not in text and "telegram_linking" not in text


def test_the_account_page_loads_the_script_with_a_version_query():
    html = (STATIC / "templates" / "frontend" / "my_account.html").read_text(encoding="utf-8")
    # A cached older script would show an empty toast for the new error (controller ruling C13).
    assert "filename='js/pages/my-account.js', v=" in html
