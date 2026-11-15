"""No full-screen preloader covers a storefront page that is already rendered.

Prod 2026-10-23: aqua-element.uz "stopped opening" for customers in Uzbekistan. The HTML arrived
in under a second, but script.js lifted the full-screen `.loader-wrap` only on `window.load`, which
waits for every image, font and analytics script; on slow links that was over a minute of spinner over a
page that was already painted. A 3.5 s inline cap followed, but every server-rendered navigation (each
cabinet tab, a language switch, a step back) still blanked content that was already there. Site review
SITE-06 (2026-11-14): the preloader is removed, markup, inline cap, stylesheet rules and script alike.
"""
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def client(app):
    # Function-scoped: the session-scoped client leaks session['language'] and '/' then 302s.
    return app.test_client()


@pytest.mark.parametrize("path", ["/", "/login", "/contact", "/coverage"])
def test_no_storefront_page_carries_a_preloader(client, db, path):
    r = client.get(path)
    assert r.status_code == 200

    soup = BeautifulSoup(r.get_data(as_text=True), "html.parser")
    assert soup.select(".loader-wrap, .preloader, .handle-preloader, .ae-preloader") == []
    css = " ".join(style.get_text() for style in soup.find_all("style"))
    assert "loader-wrap" not in css


def test_no_script_or_stylesheet_still_drives_a_preloader():
    script = (REPO / "business_app/static/js/script.js").read_text(encoding="utf-8")
    shell_css = (REPO / "business_app/static/css/ae-ui.css").read_text(encoding="utf-8")
    assert "loader-wrap" not in script and "handlePreloader" not in script
    assert ".loader-wrap" not in shell_css and ".ae-preloader" not in shell_css
