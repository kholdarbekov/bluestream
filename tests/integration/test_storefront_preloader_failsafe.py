"""The storefront preloader may not outlive the page by more than a few seconds.

Prod 2026-10-23: aqua-element.uz "stopped opening" for customers in Uzbekistan. The HTML arrived
in under a second, but script.js lifts the full-screen `.loader-wrap` only on `window.load`, which
waits for every image (~2 MB, nearly none lazy), font and analytics script. On the ~70 KB/s per
connection that Uzbek links were getting to Cloudflare for this host, `load` took over a minute,
so the spinner covered an already rendered page.

base.html therefore gives `.loader-wrap` a CSS-only fade that ends hidden whatever is still
downloading. It must be inline in the page: static CSS is served unversioned with a 4 h max-age,
the HTML is no-cache, so only an inline rule reaches every visitor on the next request.
"""
import re

import pytest
from bs4 import BeautifulSoup

pytestmark = pytest.mark.integration

# The spinner may cover the page for at most this long after first paint.
MAX_COVER_SECONDS = 5


@pytest.fixture
def client(app):
    # Function-scoped: the session-scoped client leaks session['language'] and '/' then 302s.
    return app.test_client()


def _seconds(value):
    return float(value[:-2]) / 1000 if value.endswith("ms") else float(value[:-1])


def _failsafe(html):
    """Return (delay + duration in seconds, keyframes body) of the inline .loader-wrap fade."""
    soup = BeautifulSoup(html, "html.parser")
    assert soup.select_one("body .loader-wrap .handle-preloader"), "preloader markup missing"
    css = " ".join(s.get_text() for s in soup.head.find_all("style"))
    rule = re.search(r"\.loader-wrap\s*\{[^}]*?animation\s*:\s*([^;}]+)", css)
    assert rule, "no inline .loader-wrap animation: the spinner waits for window.load again"
    parts = rule.group(1).split()
    assert "forwards" in parts, "the fade must hold its end state"
    times = [p for p in parts if re.fullmatch(r"\d*\.?\d+m?s", p)]
    assert len(times) == 2, f"expected duration and delay, got {parts}"
    frames = re.search(r"@keyframes\s+" + re.escape(parts[0]) + r"\s*\{\s*to\s*\{([^}]*)\}", css)
    assert frames, f"@keyframes {parts[0]} missing"
    return sum(_seconds(t) for t in times), frames.group(1)


@pytest.mark.parametrize("path", ["/", "/login", "/contact", "/coverage"])
def test_storefront_preloader_hides_itself_without_window_load(client, db, path):
    r = client.get(path)
    assert r.status_code == 200

    total, frames = _failsafe(r.get_data(as_text=True))

    assert total <= MAX_COVER_SECONDS
    assert re.search(r"opacity\s*:\s*0\b", frames)
    # Hidden, not just transparent: an invisible overlay at z-index 9999999 would still eat taps.
    assert re.search(r"visibility\s*:\s*hidden", frames)
